# ResolveBot

ResolveBot is a FastAPI customer-support assistant for **ShopSphere**, a fictional
electronics store. It exposes a single chat endpoint on port 8000. Customers can
track orders, ask policy questions, check return eligibility, and request returns,
warranty tickets or cancellation tickets. Every write is proposed first and saved
only after the customer says yes.

The service is deterministic by default, so it can be tested without an API key.
An optional LLM tool loop can be enabled with OpenAI, Anthropic or Google Gemini.

---

## Quick start

Requirements: Python 3.10 or newer (the implementation was developed against
Python 3.12), and the supplied `data/` folder in the project root. The folder is
listed in `.gitignore` because the assessment asks candidates not to commit it;
keep the supplied copy locally when running.

```powershell
# from the repository root (the folder containing main.py)
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate          # macOS / Linux

pip install -r requirements.txt
uvicorn main:app --port 8000
```

Open `http://127.0.0.1:8000/health` to confirm startup. Interactive API docs are
at `http://127.0.0.1:8000/docs`.

You can also run `python main.py`; it reads `HOST` and `PORT` from the
environment (defaults `0.0.0.0:8000`).

### Configuration

Copy `.env.example` to `.env` if you want to override anything. Real environment
variables take precedence over `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `POLICIES_PATH` | `./data/policies.md` | Policy Markdown file |
| `MANIFEST_PATH` | `./data/manifest.json` | Document status/effective-date manifest |
| `DB_PATH` | `./data/shopsphere.db` | SQLite database |
| `APP_TODAY` | `2026-07-01` | Business "today"; the machine clock is never used |
| `LLM_PROVIDER` | `none` | `none`, `openai`, `anthropic` or `google` |
| `LLM_AGENT` | `auto` | `auto`, `always` or `never` |
| `LLM_MODEL` | provider default | Model name override |
| `LLM_BASE_URL` | provider default | OpenAI-compatible base URL override |
| `<PROVIDER>_API_KEY` | empty | For example `OPENAI_API_KEY` |
| `LLM_API_KEY` | empty | Generic fallback key name |
| `MAX_TOOL_CALLS` | `6` | Per-message tool budget |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Server binding |
| `LOG_LEVEL` | `INFO` | Logging threshold |

With `LLM_PROVIDER=none` (the default), the deterministic planner handles every
message. No network access and no key are required.

---

## API

```http
POST /chat
X-Customer-Id: C1009
Content-Type: application/json

{"session_id": "s1", "message": "Where is order SS-10512?"}
```

```json
{
  "request_id": "e7191acb-fba1-4ec0-bf82-fceebd856372",
  "answer": "Order SS-10512 is in transit with SwiftPost Logistics. The estimated delivery date is 3 July 2026. Tracking number: SWP311402977.",
  "route": "answer",
  "citations": [],
  "tool_calls": [
    {"name": "get_orders", "args": {"order_id": "SS-10512"}, "status": "success"}
  ],
  "pending_confirmation": false,
  "session_id": "smoke-live-1"
}
```

Routes are `answer`, `clarify`, `no_answer` and `out_of_scope`. HTTP 400, 401 and
403 are reserved for malformed bodies, missing/unknown customers and sessions
owned by another customer. Business failures such as an unknown order are still
HTTP 200 with `route: "no_answer"`.

The full contract is in [`docs/api_specification.md`](docs/api_specification.md).

---

## How it works

### Request flow

1. `app/api/routes/chat.py` validates the header and JSON body.
2. A per-request SQLite connection is opened and the customer is verified.
3. The in-memory session store returns the session, rejecting cross-customer
   session IDs with HTTP 403.
4. The assistant handles the turn: it detects intents, resolves the order and
   items, calls the five tools, applies the business rules and writes the reply.
5. The route, citations, tool-call log and pending-confirmation flag are returned.

Each tool call appears in `tool_calls` in execution order. A proposed write is
logged with status `pending_confirmation`; after a confirmed write it is logged
with status `success`.

### The five tools

The tools live in `app/services/tools.py`. Their names match the brief exactly.

| Tool | Type | Behaviour |
|---|---|---|
| `get_orders` | read | Customer-scoped orders, items, delivery details, days since delivery and warranty end dates |
| `search_policy` | read | BM25 search over current, effective policy sections; returns `doc_id` and `section` |
| `check_return_eligibility` | read | Runs the ordered return checks and returns `eligible` or an error code |
| `create_return_request` | write | Re-checks eligibility, then inserts one `return_requests` row with status `requested` |
| `create_support_ticket` | write | Re-checks warranty or cancellation rules, then inserts one `support_tickets` row with status `open` |

The customer identity always comes from `X-Customer-Id`. No tool accepts a
customer ID argument, and every order lookup is filtered by the authenticated
customer. A missing order and another customer's order return the same
`ORDER_NOT_FOUND` result.

### Business rules

`app/services/rules.py` owns every decision. It reads all limits from the
`policy_parameters` table at request time, so a replacement database with
different numbers changes behaviour without code changes.

Return checks run in the required order:

1. Order exists, belongs to the customer, items belong to the order, order was
   delivered.
2. Product `returnable` is not `no`.
3. Days since delivery is within `return_window_days`, inclusive of the final day.
4. For `changed_mind` only: an opened product marked `unopened_only` fails.

Warranty ends the same calendar day N months after delivery, using
`dateutil.relativedelta` (month-end dates clamp correctly). The final day is
covered. Cancellation is allowed only while the order status is `processing`.
Writes re-run the same checks immediately before inserting, so a stale proposal
cannot bypass the rules.

### Policy search (RAG)

`app/services/policy_search.py` parses `policies.md` into documents, then into
numbered sections such as `§2.1`. The manifest is matched by `doc_id` and
controls `status`, `doc_type` and `effective_date`.

- Superseded documents are never returned.
- Documents effective after `APP_TODAY` are excluded from normal answers. A
  future-dated shipping-fee change can be mentioned as a clearly labelled future
  note with its start date when the question is about money.
- Retrieval is BM25 with light stemming, stop-word removal, phrase matching and a
  minimum query-term coverage threshold. A small preference keeps an
  authoritative policy above the FAQ when both answer the same question.
- Answers quote the selected section and cite only the documents actually used.

The policy index is built once at startup. It currently loads 66 sections from
the ten supplied documents.

### Propose, then confirm

Conversations are keyed by the caller-supplied `session_id` and are held in
memory. A session may have one waiting proposal.

1. An explicit action request produces a proposal (`pending_confirmation: true`).
   The write tool appears in `tool_calls` with status `pending_confirmation`, and
   no row is inserted.
2. Only the very next message can confirm. A clear yes runs the write and returns
   status `success`.
3. "No", a changed detail, or any unrelated message abandons the proposal. A
   changed detail starts a fresh proposal. Questions such as "where is my other
   order?" are answered normally instead of being mistaken for a confirmation.
4. A question such as "can I return this?" answers the question and offers the
   action; saying yes to the offer creates a proposal, never an immediate write.

### Optional LLM agent

When `LLM_PROVIDER` and an API key are configured (and `LLM_AGENT` is `auto` or
`always`), `app/services/agent.py` runs a provider-neutral tool loop:

- The model chooses among the same five tools.
- Read tools execute normally; citations are taken only from sections returned by
  `search_policy` in that turn.
- Write tools are intercepted. Python re-validates the request and converts it
  into a waiting proposal. The model never writes a row directly.
- The confirmation turn is handled by the deterministic planner, which re-checks
  the rules before writing.
- Provider errors, malformed tool calls and tool-budget exhaustion fall back
  safely ("nothing has been saved" or the deterministic planner).

Temperature is set to 0 for all providers.

### Testing a real provider

1. Add the key to `.env` (or set it in the shell). `.env` is git-ignored.

```dotenv
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
LLM_AGENT=always
# Optional; otherwise the provider default is used.
LLM_MODEL=gpt-4o-mini
LOG_LEVEL=INFO
```

For the other providers:

```dotenv
# Anthropic
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-3-5-haiku-latest

# Google Gemini
LLM_PROVIDER=google
GOOGLE_API_KEY=...
LLM_MODEL=gemini-1.5-flash
```

For an OpenAI-compatible local server, keep `LLM_PROVIDER=openai` and set
`LLM_BASE_URL`, for example `http://127.0.0.1:1234/v1`.

2. Stop the old server and restart it so the new environment is loaded.

```powershell
uvicorn main:app --port 8000
```

The startup log should include `LLM=openai` (or the provider you chose). A
successful model answer also logs:

```text
LLM reply via openai: route=answer, tool_calls=2
```

If the provider call fails, the log contains `LLM agent failed; using
deterministic planner` and the request still succeeds using the local planner.

3. Exercise a read, a policy question, and a write proposal.

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-Customer-Id: C1009" \
  -d '{"session_id":"llm-1","message":"Where is order SS-10512?"}'

curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-Customer-Id: C1009" \
  -d '{"session_id":"llm-2","message":"How many days do I have to return something?"}'

curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-Customer-Id: C1009" \
  -d '{"session_id":"llm-3","message":"Return the speaker from SS-10458, I changed my mind."}'
```

For the third request, confirm that:

- `pending_confirmation` is `true`;
- the last `tool_calls` entry is `create_return_request` with status
  `pending_confirmation`;
- no row exists yet in `return_requests`.

Only send `"yes"` as the next message; the deterministic confirmation path then
writes the row. This verifies that the model can choose tools without being
allowed to bypass the yes-step.

If you want to force the deterministic path for comparison, set
`LLM_AGENT=never` and restart.

---

## Assumptions

- `APP_TODAY` is the single source of "today". The wall clock is used only to
  timestamp saved rows.
- `session_id` is opaque and chosen by the caller. Separate processes do not share
  in-memory sessions; a restart starts new conversations.
- A missing header, unknown customer or malformed body is an HTTP error, not a
  chat reply.
- "Clear yes" means a short affirmative such as yes, yeah, sure, confirm, go
  ahead or please do. "Just do it" alone is not treated as confirmation.
- One proposal waits at a time. Any message other than a clear yes abandons it.
- A date without a year is interpreted as 2026 by the supplied data and policy
  text; `APP_TODAY` governs all rule calculations.
- Future-dated documents are mentioned only as future changes. They are never
  cited as current rules.
- `return_requests.item_ids` is stored as a JSON array because the supplied table
  defines it as text.
- The deterministic planner is the reference implementation. The optional LLM
  path is an add-on, not a dependency.

## What does not work / limitations

- **No live LLM key was available during development.** The optional provider
  loop is covered by a scripted fake provider (tool calls, write interception,
  confirmation and fallback), but the real OpenAI/Anthropic/Google HTTP calls were
  not exercised against a live endpoint.
- Sessions are in-memory and process-local. They are not persisted, shared across
  workers, or evicted except when the process exits. The store is protected by a
  lock but is not a distributed cache.
- Retrieval is lexical BM25, not embeddings. It handles the supplied policy
  vocabulary well, but unusual paraphrases can return no result rather than a
  best guess. No result deliberately becomes `no_answer`.
- Conversation understanding is rule-based in deterministic mode. Very unusual
  phrasing may need a clarification turn, especially item descriptions.
- There is no authentication beyond trusting `X-Customer-Id` (the brief assumes
  an already logged-in customer), no rate limiting and no UI.
- Refund amounts, emails, human hand-off and exchanges are explicitly out of
  scope.

## Tests

The suite uses pytest and FastAPI's test client. Every test copies
`data/shopsphere.db` to a pytest temporary directory, so the supplied database is
never modified.

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

Current result: **83 passed** across business rules, policy retrieval, tools,
HTTP contract, conversation safety and the optional agent path.

| Test file | Focus |
|---|---|
| `tests/test_business_rules.py` | Return order of checks, inclusive day 30, warranty month-end, cancellation |
| `tests/test_policy_search.py` | Manifest filtering, superseded/future documents, citations, coverage |
| `tests/test_tools.py` | Owner scoping, error codes, writes and re-checks |
| `tests/test_api_contract.py` | Response shape and 400/401/403 behaviour |
| `tests/test_conversation.py` | Clarification, propose/confirm, no-write safety, combined intents |
| `tests/test_agent.py` | Scripted LLM loop, write interception, fallback |

Manual smoke test performed with a live uvicorn process:

```text
GET  /health -> {"status":"ok","policy_sections":66,"app_today":"2026-07-01"}
POST /chat   -> tracking answer for SS-10512, route "answer", one get_orders call
```

## Example conversations

```text
C1009: Where is order SS-10512?
Bot:   Order SS-10512 is in transit with SwiftPost Logistics. The estimated
       delivery date is 3 July 2026. Tracking number: SWP311402977.

C1009: Return the speaker from SS-10458, I changed my mind.
Bot:   You can return Nimbra Smart Speaker from order SS-10458. It was delivered
       on 21 June 2026 (10 days ago), which is within the 30-day return window.
       Shall I go ahead and create the return request?
                                      [pending_confirmation: true]

C1009: Yes
Bot:   Done - the return request has been created for order SS-10458. Your
       reference is <uuid>. [create_return_request: success]
```

```text
C1024: I want to return my charger.
Bot:   Which order do you mean? SS-10471, SS-10489 and SS-10503 all contain
       chargers. [route: clarify]

C1024: The 65W one, I changed my mind.
Bot:   It was delivered ... within the return window. Shall I create the return
       request?
```

```text
C1011: Can I return the opened earbuds? I changed my mind.
Bot:   Auralis Buds Air has been opened. In-ear earphones and earbuds can be
       returned only if the hygiene seal is unbroken ... [no proposal]
```

```text
C1009: What's the weather in Mumbai?
Bot:   I can help with ShopSphere orders and policies ... [route: out_of_scope]
```

## Project layout

```text
main.py                       Uvicorn entry point
app/
  main.py                     FastAPI app factory and lifespan
  api/routes/chat.py          POST /chat and HTTP error contract
  api/routes/health.py        GET /health
  core/config.py              Environment settings and .env loader
  core/logging.py             Logging setup
  db/repository.py            SQLite queries and inserts
  schemas/chat.py             Pydantic request/response models
  services/
    nlu.py                    Intent, item and message helpers
    rules.py                  Return, warranty and cancellation rules
    policy_search.py          Markdown parsing, manifest filtering, BM25
    tools.py                  The five tools with owner scoping
    sessions.py               Session store and proposal state
    assistant.py              Deterministic planner and reply writer
    agent.py                  Optional LLM tool loop
tests/                        Pytest suite with temporary database copies
data/                         Supplied inputs (do not edit)
docs/                         Requirements, architecture, API and report
```

## Where AI coding assistants were used

AI coding assistants were used to help scaffold the implementation, generate the
test suite and draft documentation. All business rules, safety behaviour and
tests were reviewed and exercised locally. The deterministic planner was chosen
so the safety-critical path can be explained and verified without a model.

---

## Documentation

- [`docs/requirements_analysis.md`](docs/requirements_analysis.md) - requirement
  breakdown, assumptions and traceability
- [`docs/architecture.md`](docs/architecture.md) - components, flows, data model
  and safety invariants
- [`docs/api_specification.md`](docs/api_specification.md) - request/response
  schema, routes, errors and examples
- [`docs/implementation_report.md`](docs/implementation_report.md) - delivery
  summary, verification evidence and remaining limitations
