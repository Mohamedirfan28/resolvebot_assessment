# ResolveBot implementation report

## 1. Task understanding

Build the backend of ResolveBot, a customer-support assistant for the fictional
ShopSphere store. A customer chats through `POST /chat`; the service identifies
the customer from `X-Customer-Id`, answers questions using the supplied orders,
shipments and policy documents, enforces the business rules in Python, and
performs the two write operations (return request and support ticket) only after
an explicit customer confirmation.

The PDF is the source of truth. The main non-obvious constraints are:

- Exactly the five named tools, with owner-scoped data access.
- Exactly the four routes: `answer`, `clarify`, `no_answer`, `out_of_scope`.
- Missing and foreign orders must be indistinguishable, using the exact reply
  `I couldn't find that order on your account.`
- Every number used in a decision must come from `policy_parameters`; `APP_TODAY`
  is the only source of "today" for business logic.
- Policy retrieval must split all ten documents, exclude superseded versions,
  exclude future documents from current answers, and cite only paragraphs that
  were actually used.
- A write is never saved before an explicit yes; a session has at most one
  pending proposal.
- Return only the six required response fields, and use HTTP errors only for the
  specified 400/401/403 cases.

## 2. Sources used

- `TASK.pdf` — assessment requirements and example conversations.
- `data/policies.md` — ten policy documents with `### §x.y` sections.
- `data/manifest.json` — document status and effective dates (current,
  superseded, future).
- `data/shopsphere.db` — SQLite tables for customers, orders, order items,
  shipments, returns, support tickets, and `policy_parameters`.

The supplied `data/` folder is intentionally not part of the submission; it is
ignored by `.gitignore` and must be present next to `main.py` at runtime.

## 3. Implemented requirements

### API

- `POST /chat` on port 8000 with `X-Customer-Id`.
- Request body: `{"session_id": "...", "message": "..."}`.
- Response fields exactly: `request_id`, `answer`, `route`, `citations`,
  `tool_calls`, `pending_confirmation`.
- `400` for malformed JSON, missing/blank fields, wrong field types, or other
  Pydantic body-validation failures.
- `401` for a missing or unknown customer header.
- `403` when a session ID is already owned by another customer.
- `GET /health` for readiness, and `/docs` for the generated OpenAPI schema.
- Unknown orders, policy misses and off-topic messages are normal 200 replies.

### Tools

All five tools use the exact required names:

1. `get_orders`
2. `search_policy`
3. `check_return_eligibility`
4. `create_return_request`
5. `create_support_ticket`

The customer ID is supplied by request context, never by the message. Repository
queries filter by `customer_id`, so a foreign order behaves exactly like a
missing order. `tool_calls` records the name, arguments, status and any error
code. Write tools are shown as `pending_confirmation` during the proposal turn.

### Business rules

- `policy_parameters` is read at runtime for the return window, warranty
  months, shipping fee, support email and similar values. No business number is
  hard-coded.
- Return decisions check ownership, item membership, delivery, returnability,
  window and the opened-item rule in a fixed order. Day 30 is inside the 30-day
  window; day 31 is outside.
- Warranty end dates use calendar-month arithmetic and include the end date.
- Cancellation is allowed only while fulfilment is still cancellable; shipped
  or delivered orders are refused.
- The exact not-found reply is `I couldn't find that order on your account.`
  with route `no_answer`, no citations and no write.

### Policy RAG

- `policies.md` is split into all ten documents and then into their `§x.y`
  sections (66 searchable paragraphs in the supplied data).
- The manifest removes superseded documents from the current index.
- Documents whose effective date is after `APP_TODAY` are kept separately and
  are never applied as current policy; a future change may only be mentioned as
  a future change.
- A small BM25 index retrieves relevant paragraphs, with title/heading boosts.
- Replies quote retrieved text and cite only the documents and sections used.

### Conversation and confirmation flow

- The default planner is deterministic, so the service works without any LLM
  key.
- Intent parsing detects tracking, returns, warranty, cancellation, policy
  questions, item/order references, reasons and confirmation/denial.
- Ambiguous orders or items produce `clarify`, and the clarifying context is
  remembered for short follow-ups.
- Policy questions are answered without pretending to perform an action.
- A return or ticket is first proposed with `pending_confirmation: true`.
  Nothing is inserted in that turn.
- Only an explicit yes in the next message executes the pending proposal, with
  the rule check repeated immediately before the insert.
- "No", a topic change or changed item details clear/replace the proposal; at
  most one proposal waits per session.
- Sessions are keyed by `session_id` and owned by the customer who created
  them; a different customer using the same ID receives `403`.
- An optional real LLM tool loop is available with `LLM_AGENT=1`; Python still
  intercepts writes and keeps the confirmation gate.

## 4. Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/chat` | Main conversation endpoint |
| GET | `/health` | Liveness check |
| GET | `/docs` | Generated API documentation |

Example:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-Customer-Id: C1009" \
  -d '{"session_id":"s1","message":"Where is order SS-10512?"}'
```

## 5. Files

| Path | Responsibility |
|---|---|
| `main.py` | FastAPI app, lifespan, Pydantic validation, HTTP status mapping |
| `app/schemas.py` | Request, response, citation, tool-call and error models |
| `app/config.py` | Environment configuration and defaults |
| `app/repository.py` | SQLite access; every customer query is owner-scoped |
| `app/rules.py` | Return, warranty and cancellation rules using DB parameters |
| `app/policy_search.py` | Document splitting, manifest filtering, BM25 retrieval |
| `app/tools.py` | Five tools, tool context and tool-call logging |
| `app/nlu.py` | Intent, order, item, reason and question detection |
| `app/assistant.py` | Planner, clarifications, proposals and confirmations |
| `app/assistant_types.py` | Shared reply dataclass |
| `app/sessions.py` | Per-customer session memory and one pending proposal |
| `app/agent.py` | Optional LLM tool loop and provider adapters |
| `app/llm.py` | Optional answer-polish pass |
| `tests/test_rules.py` | Rule boundaries and date arithmetic |
| `tests/test_api.py` | HTTP contract, validation, flows, confirmation and privacy |
| `tests/test_custom_parameters.py` | Proves return decisions read the DB parameters |
| `tests/test_agent.py` | Scripted optional-LLM loop and safety checks |
| `README.md` | Setup, configuration, design, assumptions and limitations |
| `REPORT.md` | This implementation report |

## 6. Dependencies

- Python 3.10+
- `fastapi>=0.110`
- `pydantic>=2.6`
- `uvicorn[standard]>=0.27`
- `python-dateutil>=2.9`
- `httpx>=0.27` (also used by the optional provider calls and tests)

No database migration, Docker, UI or build step is required. Environment
variables are documented in `.env.example`; `.env` and provider keys are ignored
by Git.

## 7. Actual test results

Executed in this environment on 2026-10-27:

```text
python -m unittest discover -s tests
..................
----------------------------------------------------------------------
Ran 49 tests in 0.414s

OK
```

The 49 tests consist of rule unit tests, HTTP integration tests against a
temporary copy of the SQLite database, scripted-provider tests for the optional
LLM loop, and a test that changes the return window in the database to prove the
logic follows `policy_parameters` rather than a constant.

A real `uvicorn main:app --port 8000` process was also started and checked:

- `GET /health` returned `{"status":"ok"}`.
- `GET /docs` returned HTTP 200.
- Tracking `SS-10512` for `C1009` returned the expected in-transit answer and
  tracking number.
- A return proposal for `SS-10458` returned `pending_confirmation: true` and
  `create_return_request: pending_confirmation`.
- Saying yes returned `pending_confirmation: false` and
  `create_return_request: success`.
- The response body contained exactly the six required fields.
- A foreign/missing order returned route `no_answer` with the exact required
  sentence.

The server was stopped after the smoke test. The test suite uses a temporary
copy of the database, so the supplied data is not modified by tests.

## 8. Limitations and assumptions

- The default planner is deterministic. It is robust for the supplied data and
  example flows, but unusual phrasing may require clarification. A real LLM tool
  loop exists behind `LLM_AGENT=1`; it was tested with scripted providers, not a
  live provider key in this environment.
- Intent parsing is keyword/pattern based by design: when a request is
  ambiguous, the assistant asks rather than guessing.
- Sessions are in memory and single-process. Multiple workers would not share
  pending proposals, and a restart clears conversations.
- There is no authentication beyond the header specified by the brief.
- `data/` is excluded from the submission and must be supplied at runtime.
- No refund amounts are calculated and no exchanges are processed, per the
  brief; the exchange policy can still be explained.
- Future policy changes are only mentioned as future changes, never applied
  before their effective date.
- Multi-intent handling is strongest for tracking plus one other action; three
  or more independent requests in one message are not guaranteed.
