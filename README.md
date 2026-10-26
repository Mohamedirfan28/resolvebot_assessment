# ResolveBot — ShopSphere support assistant

A small FastAPI service that handles customer-support chats for the fictional
ShopSphere store. It can look up a customer's orders, answer policy questions
with citations, check return eligibility, and create return requests or support
tickets **only after the customer confirms**.

- Python 3.10+
- One command to start: `uvicorn main:app --port 8000`
- No UI, no Docker, no database migration
- Business decisions (dates, eligibility, warranty) are made in Python from
  `policy_parameters`, never by the language model

---

## 1. Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt

# The assessment data/ folder must sit next to main.py.
cp .env.example .env          # optional; the defaults already point at ./data

uvicorn main:app --port 8000
```

Health check: `GET http://127.0.0.1:8000/health`.

A request:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-Customer-Id: C1009" \
  -d '{"session_id":"s1","message":"Where is order SS-10512?"}'
```

`python main.py` also works and reads `HOST`/`PORT`.

---

## 2. Configuration

All settings come from environment variables; a local `.env` is loaded if
present. See `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `POLICIES_PATH` | `./data/policies.md` | Policy Markdown |
| `MANIFEST_PATH` | `./data/manifest.json` | Document metadata |
| `DB_PATH` | `./data/shopsphere.db` | SQLite database |
| `APP_TODAY` | `2026-07-01` | The date the rules treat as today |
| `LLM_PROVIDER` | `none` | `none`, `openai`, `anthropic`, `google` |
| `LLM_MODEL` | provider default | Model name for the agent loop / polish pass |
| `LLM_BASE_URL` | provider default | OpenAI-compatible base URL |
| `LLM_AGENT` | `0` | Set `1` for the real LLM tool loop (needs a provider key) |
| `LLM_POLISH` | `0` | Set `1` to let the LLM rephrase final answers when the agent loop is off |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | empty | Provider key |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Server bind address |
| `MAX_TOOL_CALLS` | `6` | Upper bound used by the planner flows |

`APP_TODAY` is the only source of "today" for business logic. The machine clock
is used only for audit timestamps on inserted rows.

---

## 3. API

### `POST /chat`

Headers: `X-Customer-Id`, `Content-Type: application/json`.

```json
{"session_id": "s1", "message": "Return the speaker from SS-10458, I changed my mind."}
```

Response:

```json
{
  "request_id": "5d1e...",
  "answer": "You can return Nimbra Smart Speaker from order SS-10458. ...",
  "route": "answer",
  "citations": [{"doc_id": "returns_policy", "section": "§2.1"}],
  "tool_calls": [
    {"name": "get_orders", "args": {"order_id": "SS-10458"}, "status": "success"},
    {"name": "check_return_eligibility", "args": {"order_id": "SS-10458", "item_ids": ["SS-10458-1"], "reason": "changed_mind"}, "status": "success"},
    {"name": "search_policy", "args": {"query": "return window days"}, "status": "success"},
    {"name": "create_return_request", "args": {"order_id": "SS-10458", "item_ids": ["SS-10458-1"], "reason": "changed_mind"}, "status": "pending_confirmation"}
  ],
  "pending_confirmation": true,
  "session_id": "s1"
}
```

Routes: `answer`, `clarify`, `no_answer`, `out_of_scope`.

HTTP errors:

- `400` — invalid JSON, or missing `session_id` / `message`
- `401` — missing or unknown `X-Customer-Id`
- `403` — the `session_id` belongs to another customer

Everything else, including "order not found", is a normal `200` reply.

---

## 4. How the code is organised

```
main.py                  FastAPI app, header/body validation, HTTP status codes
app/config.py            environment settings
app/repository.py        SQLite access; every query filters by customer_id
app/rules.py             return, warranty and cancellation rules (all numbers
                         come from policy_parameters)
app/policy_search.py     splits policies.md, filters by manifest, BM25 search
app/tools.py             the five tools + tool-call log
app/agent.py             optional LLM tool loop (LLM_AGENT=1)
app/nlu.py               deterministic intent / order / item / reason parsing
app/assistant.py         conversation planner, clarification, propose/confirm
app/assistant_types.py   shared reply dataclass
app/sessions.py          per-session memory (messages, one pending proposal)
app/llm.py               optional answer-polish pass
tests/                   rule tests and HTTP tests
```

### The five tools

`get_orders`, `search_policy`, `check_return_eligibility`,
`create_return_request`, `create_support_ticket` — with the exact inputs from
the brief. Every tool takes the customer ID from the request context, never from
the message. An order that is missing and an order that belongs to another
customer both return `ORDER_NOT_FOUND`, so existence cannot be probed.

`get_orders` adds `delivery_date`, `days_since_delivery`, and per item
`warranty_end` / `warranty_active`. Writes re-run the rule check immediately
before inserting.

### Return check order

1. order exists, belongs to the caller, items belong to the order → `ORDER_NOT_FOUND` / `INVALID_ITEM`
2. order delivered → `NOT_DELIVERED`
3. product is not `no` → `NON_RETURNABLE`
4. `days_since_delivery <= return_window_days` → `RETURN_WINDOW_EXPIRED`
5. opened + `changed_mind` + `unopened_only` → `OPENED_NOT_RETURNABLE`

Day 30 is allowed; day 31 is not. Warranty ends on the same calendar day N
months later (month-end handled with `dateutil.relativedelta`) and that last day
is still covered.

### Policy search

`policies.md` is split at each top-level `#` document, then into `### §x.y`
sections. A section is only searchable if the manifest says `status: current`
and `effective_date <= APP_TODAY`. Superseded documents (including the 2024
returns policy) are excluded at index time; future documents are kept in a
separate list so the assistant can mention a later change with its start date.
Ranking is a small BM25 index with title/heading boosts. Answers quote the
retrieved paragraph and cite only the documents used.

### Propose, then do

A write tool is never called in the same turn as the proposal. The proposal is
stored in the session with `pending_confirmation: true`, and the pending tool
call is shown with status `pending_confirmation`. Only a clear yes in the very
next message executes it. "No", a new topic, or changed details clear the
proposal. A changed detail ("just the power bank") starts a new proposal for the
same order. A session holds at most one waiting proposal.

### Optional LLM tool loop

The deterministic planner is the default. Setting `LLM_AGENT=1` together with
`LLM_PROVIDER` and a provider key switches normal turns to a real tool loop:
the message plus the five tool schemas go to the model, requested tools are
executed, their results are sent back, and this repeats until the model writes
a final reply or `MAX_TOOL_CALLS` is reached. OpenAI-compatible chat
completions, the Anthropic Messages API and the Google `generateContent` API
are supported, all at temperature 0.

Safety stays in Python, outside the prompt:

- `create_return_request` and `create_support_ticket` are **intercepted**. The
  arguments are validated against the same rule functions, and an eligible
  action becomes a waiting proposal. The loop never inserts a row.
- The customer's yes is still handled by the deterministic path, which re-runs
  the rule check and only then calls the write tool.
- A missing or foreign order short-circuits to the exact
  `I couldn't find that order on your account.` reply with `no_answer` and no
  citations.
- Citations returned by the model are filtered to sections that `search_policy`
  actually returned in that turn.
- A provider error or timeout falls back to the deterministic planner for that
  message, with no partial tool log.

With `LLM_AGENT=0` (the default), `app/agent.py` is never called.

---

## 5. Assumptions

- **Reason is required for a return.** `changed_mind` and `defective` change the
  outcome (opened earbuds), so if the customer does not say which, the assistant
  asks instead of guessing.
- **A question is not an action.** "Can I return it?" gets an answer plus an
  offer; "please return it" gets a proposal. A yes to an explicit offer executes
  the action, because the offer already described exactly what will happen.
- **Ambiguity is never guessed.** If the same product appears in several orders,
  or an order has several matching items, the assistant lists them and asks.
  The clarifying context is remembered, so a short follow-up ("SS-10471",
  "the charger", "changed my mind") continues the same flow.
- **Delivered** means `shipments.delivered_at` is set. No delivery timestamp is
  treated as `NOT_DELIVERED`.
- **Cancelling a delivered order** is refused. The tool returns
  `ORDER_ALREADY_SHIPPED` because the brief's error-code list has no separate
  "delivered" code.
- **Future policy documents** are not applied. The shipping-fee update dated
  2026-08-01 is mentioned only as a future change, and only for cost/fee
  questions.
- **Sessions are in memory**, keyed by `session_id`, and belong to the customer
  who created them. Restarting the service clears chats.
- **`data/` is not part of the submission.** The `.gitignore` excludes it; the
  service expects the assessment copy to be present at runtime.
- **No refund amount is calculated** and no exchanges are processed, per the
  brief. The exchange policy can still be explained.

## 6. What does not work / known limitations

- **The tool-selection step is deterministic by default.** `LLM_AGENT=1`
  enables the real tool loop described in section 4. That path is covered by
  scripted-provider tests, but was not run against a live provider key in this
  environment. The deterministic planner is what the example conversations and
  the HTTP tests exercise. If a true LLM planner is required with no
  configuration, this is the main deviation.
- Intent parsing is keyword/pattern based. Very unusual phrasing may end in a
  clarification instead of the intended flow. That is deliberate (ask rather
  than guess), but it is a limitation.
- Sessions are single-process and not persisted. Multiple uvicorn workers would
  not share pending proposals.
- There is no authentication beyond the `X-Customer-Id` header, which is what
  the brief specifies.
- The LLM agent and polish paths are optional. Both are covered by scripted
  provider tests, but neither was exercised against a live provider key in this
  environment; provider errors fall back to the deterministic text.
- Multi-intent support covers tracking plus one other action in the same message.
  Three or more separate requests in one message are not guaranteed.

## 7. Example conversations

All with `APP_TODAY=2026-07-01`.

**Track**

> C1009: Where is order SS-10512?
>
> Bot: Order SS-10512 is in transit with SwiftPost Logistics. The estimated
> delivery date is 3 July 2026. Tracking number: SWP311402977.

**Return, propose then confirm**

> C1009: I want to return the speaker from SS-10458, I changed my mind.
>
> Bot: You can return Nimbra Smart Speaker from order SS-10458. It was delivered
> on 21 June 2026 (10 days ago), which is within the 30-day return window. Shall
> I go ahead and create the return request?
>
> (`pending_confirmation: true`, `create_return_request` status
> `pending_confirmation`, nothing in the database)
>
> C1009: Yes, go ahead.
>
> Bot: Done — the return request has been created for order SS-10458. Your
> reference is ... (`create_return_request` status `success`)

**Foreign / missing order**

> C1009: Where is order SS-10530?
>
> Bot: I couldn't find that order on your account.
>
> (`route: no_answer`, `citations: []`, `get_orders` error `ORDER_NOT_FOUND`;
> SS-10530 belongs to another customer)

**Policy**

> How many days do I have to return something?
>
> Bot quotes `returns_policy §2.1` (30 days) and `§2.2` (how days are counted),
> with citations.

**Ambiguous item**

> C1024: I want to return my charger.
>
> Bot: I found that item in more than one order: SS-10503 (...); SS-10489 (...);
> SS-10471 (...). Which order do you mean? Also, is this a change of mind, or is
> the item defective?
>
> C1024: SS-10471 → Bot asks which item in that order
> C1024: the charger → Bot asks for the reason
> C1024: changed my mind → Bot proposes the return

**Opened earbuds**

> C1011: Return the earbuds from SS-10505, I changed my mind.
>
> Bot: ... cannot be returned for a change of mind because they were opened;
> quotes `returns_policy §4.2`. A defective return is still possible.

**Cancellation**

> C1039: Please cancel order SS-10530. → proposal for a cancellation ticket
> C1039: yes → ticket saved with status `open`

**Off-topic**

> Tell me a joke. → `out_of_scope`, empty citations and tool calls.

## 8. Tests

```bash
python -m unittest discover -s tests -v
```

- `tests/test_rules.py` covers the day-30/day-31 boundary, opened earbuds,
  gift cards, missing delivery, warranty month-end (31 Jan → 28/29 Feb), the
  inclusive warranty end date, and cancellation states.
- `tests/test_api.py` starts the app against a temporary copy of the database
  and checks the response shape, 400/401/403, the exact not-found reply,
  "nothing saved before yes", and the main flows.
- `tests/test_agent.py` drives the optional LLM loop with a scripted provider
  (no network) and checks that it cites only retrieved sections, never writes
  before a yes, refuses an ineligible return, hides foreign orders, falls back
  on a provider error, and stops at the tool limit.

## 9. Where AI coding assistants were used

This solution was built with an AI coding assistant (OpenCode with a DeepSeek
model) used for scaffolding, drafting the rule and retrieval modules, and
writing the tests. The design choices, the rule ordering, the confirmation
state machine and the verification against the supplied data were reviewed and
run locally; the README's limitations are listed honestly above.
