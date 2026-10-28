# Implementation report

## 1. Summary

ResolveBot is complete and runnable as a FastAPI service on port 8000. The
delivery includes:

- `POST /chat` with the required header, session and response contract;
- exactly five owner-scoped tools;
- Python business rules whose numbers come from `policy_parameters`;
- policy retrieval over the supplied Markdown and manifest, with superseded and
  future-document filtering and citations;
- clarification and propose/confirm conversation handling with no-write
  guarantees;
- an optional LLM tool loop with write interception and deterministic fallback;
- 84 automated tests;
- README, `.env.example`, API specification, architecture, requirements analysis
  and this report.

The default configuration uses the deterministic planner, so the service starts
and works without an API key.

## 2. Delivered files

```text
main.py, app/main.py                 FastAPI app and uvicorn entry point
app/api/routes/chat.py              POST /chat contract
app/api/routes/health.py            GET /health
app/core/config.py                  Environment settings
app/core/logging.py                 Logging setup
app/db/repository.py                SQLite reads and inserts
app/schemas/chat.py                 Pydantic request/response models
app/services/nlu.py                 Intent and item helpers
app/services/rules.py               Return, warranty, cancellation rules
app/services/policy_search.py       Markdown parsing, manifest filter, BM25
app/services/tools.py               Five tools and tool-call log
app/services/sessions.py            Sessions and pending proposals
app/services/assistant.py           Deterministic planner
app/services/agent.py               Optional LLM providers and tool loop
tests/                              84 tests with temporary DB copies
docs/                               Design and API documentation
requirements.txt, requirements-dev.txt, .env.example, pytest.ini

README.md
docs/requirements_analysis.md
docs/architecture.md
docs/api_specification.md
docs/implementation_report.md
```

## 3. Requirement coverage

| Brief requirement | Status | Evidence |
|---|---|---|
| One `POST /chat` on port 8000 | Done | live uvicorn smoke test |
| `X-Customer-Id`, caller `session_id` | Done | API tests |
| Exact five tool names | Done | tool tests and agent specs |
| All business numbers from `policy_parameters` | Done | rule tests, including changed parameter values |
| Return checks in the required order | Done | `test_business_rules.py` |
| Inclusive day-30 boundary | Done | day 30 passes, day 31 fails |
| Warranty month arithmetic incl. month-end | Done | relativedelta tests |
| Cancellation only while processing | Done | rule and tool tests |
| Current, effective policies only | Done | manifest/future tests |
| Superseded documents excluded | Done | retrieval tests |
| Future documents only as dated notes | Done | shipping-fee tests |
| Cite `doc_id` + section, only used docs | Done | policy and API tests |
| Propose, then confirm; no write before yes | Done | conversation and tool tests |
| Only the next message can confirm | Done | abandonment tests |
| Exact not-found reply, `no_answer` | Done | conversation and API tests |
| Four routes with out-of-scope empties | Done | API tests |
| 400/401/403 only for the specified cases | Done | API tests |
| Business errors remain HTTP 200 | Done | API tests |
| README, `.env.example`, docs | Done | this delivery |

## 4. Verification evidence

### 4.1 Automated tests

Command:

```powershell
python -m pytest
```

Result:

```text
84 passed
```

Breakdown:

| File | Tests | Coverage area |
|---|---:|---|
| `tests/test_agent.py` | 8 | Scripted LLM tool loop, write interception, fallback |
| `tests/test_api_contract.py` | 13 | Response shape, root docs redirect, routes, 400/401/403, 200 business errors |
| `tests/test_business_rules.py` | 20 | Return order, boundary, warranty, cancellation, parameter overrides |
| `tests/test_conversation.py` | 19 | Clarification, confirmation, abandonment, combined intents, no-write safety |
| `tests/test_policy_search.py` | 9 | Manifest filtering, superseded/future, citations, coverage |
| `tests/test_tools.py` | 15 | Owner scoping, error codes, writes and re-checks |
| **Total** | **84** | |

Every test that needs the database copies `data/shopsphere.db` into a pytest
temporary directory (`tests/conftest.py`). The supplied database is never opened
for writing by the suite.

### 4.2 Live server smoke test

Command:

```powershell
uvicorn main:app --port 8000
```

Startup log:

```text
ResolveBot started: 66 policy sections, APP_TODAY=2026-07-01, LLM=none
```

Health:

```json
{"status":"ok","policy_sections":66,"app_today":"2026-07-01"}
```

Chat:

```json
{
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

The server was stopped cleanly after the check.

### 4.3 Supplied data preservation

After all implementation, manual checks and tests:

| File | Size | SHA-256 |
|---|---:|---|
| `data/policies.md` | 12,742 | `7ed2d1a1f6ba9b3b18bc80ca2ef9245032c3379995598aaf84d34000c0d49f61` |
| `data/manifest.json` | 2,046 | `51a947e556810acb911cee360a5a55b8f4d40fa76de68da5c52d97adcd6cd8d9` |
| `data/shopsphere.db` | 163,840 | `0058ba50ebb3ddc06878655f5a7210da95da4edf53ee57e74b7e6e7e2c9bc091` |

`return_requests` and `support_tickets` both contain **0** rows. No schema changes
were made.

### 4.4 Manual conversation checks

The following scenarios were exercised during development and are covered by
tests:

- tracking an in-transit order and a delivered order;
- return proposal followed by yes, and the resulting database row;
- abandoned proposal followed by "yes" (no write);
- off-topic message clearing a proposal;
- multi-order charger clarification, then narrowing by "the 65W one";
- opened earbuds refused for change of mind, allowed for defective;
- gift card and software refused as non-returnable;
- day 30 accepted, day 31 refused;
- warranty active, expired and no-warranty items;
- cancellation while processing, and after shipping;
- another customer's order producing the exact not-found reply;
- policy questions with citations and a clearly dated future note;
- product availability and unrelated topics returning `no_answer` or
  `out_of_scope`;
- combined tracking plus policy question answering both.

## 5. Key design decisions

1. **Deterministic planner by default.** The brief's five pass/fail checks are
   safety and correctness checks. Keeping rules and confirmation in Python makes
   them auditable and testable without a model. The optional LLM adds natural
   language flexibility but cannot write.
2. **Re-check before write.** Both the read eligibility tool and the write tool
   run the same rule function. The write path never trusts a previous result.
3. **One proposal, one turn.** A pending proposal is cleared by any message other
   than a clear yes. This prevents a delayed "yes" from saving something the user
   no longer meant.
4. **Owner scoping in SQL.** Every order query includes the header customer.
   Foreign and missing orders are indistinguishable.
5. **Manifest-driven retrieval.** Document status and effective dates are read
   from the manifest, not guessed from the Markdown. Future documents never enter
   the current index path.
6. **BM25, no embeddings.** The supplied corpus is small and vocabulary-driven.
   Lexical retrieval is fast, deterministic, CPU-only and needs no model
   download. A coverage threshold favors `no_answer` over guessing.
7. **Policy-over-FAQ preference.** When both cover a question, the authoritative
   policy ranks slightly higher. Exact FAQ-only topics such as payment methods
   remain answerable.
8. **Per-request SQLite connections.** No ORM or migration layer; the supplied
   schema is authoritative and easy to inspect.

## 6. Known limitations and unfinished work

1. **No live LLM provider test.** There was no API key in the environment. The
   OpenAI, Anthropic and Google adapters and the tool loop were verified with a
   scripted fake provider, including write interception and fallback. The real
   HTTP requests are implemented but not proven against a live endpoint.
2. **In-memory sessions.** Chats do not survive a process restart and are not
   shared between workers. This is acceptable for a single-process assessment
   service but would need Redis or a database for production.
3. **Lexical retrieval limits.** Unusual paraphrases that do not share vocabulary
   with a policy may receive `no_answer`. This is intentional fail-closed
   behavior, not a silent guess.
4. **Rule-based NLU in deterministic mode.** Very unusual item descriptions may
   require a clarification turn. The optional LLM improves this when configured.
5. **No rate limiting, authentication or UI.** The brief assumes an already
   logged-in customer and requires only the API.
6. **Exchanges are not write actions.** Questions about the exchange window are
   answered from policy, as permitted by the brief's "not needed" list.
7. **No load or concurrency testing.** The local paths are fast, but no
   multi-client benchmark was run.

## 7. How to reproduce

```powershell
cd <repository root>
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --port 8000
```

In another terminal:

```powershell
curl -X POST http://localhost:8000/chat `
  -H "Content-Type: application/json" `
  -H "X-Customer-Id: C1009" `
  -d '{"session_id":"demo","message":"Where is order SS-10512?"}'
```

Tests:

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

## 8. Conclusion

The implementation prioritizes the brief's five pass/fail checks: it starts with
one command, returns the required response shape, never writes without a yes,
applies return rules from database parameters (including weird boundaries), and
prevents cross-customer order access. The remaining limitation is honest: the
optional live LLM path is implemented and fake-tested, but could not be exercised
against a real provider without a key.
