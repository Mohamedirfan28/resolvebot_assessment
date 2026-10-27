# Requirements analysis

This document breaks the ResolveBot brief (`TASK.pdf`) into implementable
requirements, records the decisions taken where the brief leaves room for
interpretation, and maps each requirement to code and tests.

## 1. Scope

ResolveBot is a backend-only chat service for an already authenticated ShopSphere
customer. It must:

- read the authenticated customer's orders;
- answer questions from supplied policy documents, with citations;
- decide return, warranty and cancellation eligibility in Python;
- propose writes and save them only after an explicit yes;
- refuse unrelated topics politely;
- expose `POST /chat` on port 8000.

No UI, login, payments, refund amounts, email delivery, human hand-off or
exchanges are required.

## 2. Functional requirements

### 2.1 API and HTTP contract

| ID | Requirement | Implementation | Verification |
|---|---|---|---|
| API-1 | `POST /chat` on port 8000 | `app/api/routes/chat.py`, `main.py` | live uvicorn smoke test |
| API-2 | Customer identity from `X-Customer-Id` | `chat.py` reads the header; tools never accept a customer ID | `test_api_contract.py`, `test_tools.py` |
| API-3 | Caller-supplied `session_id` | `ChatRequest`; `SessionStore` | `test_api_contract.py`, `test_conversation.py` |
| API-4 | Return required response fields | `app/schemas/chat.py` response assembly | `test_api_contract.py` |
| API-5 | 400 for invalid JSON or missing/invalid body | `chat.py`; validation handler in `app/main.py` | `test_api_contract.py` |
| API-6 | 401 for missing or unknown customer | header check and `customer_exists` | `test_api_contract.py` |
| API-7 | 403 when a session belongs to another customer | `SessionConflict` mapped to 403 | `test_api_contract.py` |
| API-8 | Business errors remain HTTP 200 | `_process` always returns 200 after validation | `test_api_contract.py`, `test_conversation.py` |
| API-9 | Reply within 30 seconds | deterministic local path is milliseconds; LLM calls have a 24 s turn deadline | not load-tested |

### 2.2 The five tools

Tool names must be exact.

| Tool | Requirement | Implementation |
|---|---|---|
| `get_orders` | Optional order/status; returns orders, items, delivery details, days since delivery, warranty end and active flag | `ToolContext.get_orders`, `rules.enrich_order` |
| `search_policy` | Best matching paragraphs with `doc_id` and `section` | `ToolContext.search_policy`, `PolicyIndex` |
| `check_return_eligibility` | Ordered return checks; eligible or error code | `ToolContext.check_return_eligibility`, `rules.evaluate_return` |
| `create_return_request` | Insert one requested row after re-check | `ToolContext.create_return_request`, `repository.insert_return_request` |
| `create_support_ticket` | Insert one open warranty/cancellation ticket after re-check | `ToolContext.create_support_ticket`, `repository.insert_support_ticket` |

All tools:

- return a structured error instead of raising for bad inputs;
- scope order access by the authenticated customer;
- return the same `ORDER_NOT_FOUND` for a missing order and another customer's
  order;
- appear in `tool_calls` in order, with `success`, `error` or
  `pending_confirmation` status.

Verified by `tests/test_tools.py`.

### 2.3 Business rules

| ID | Requirement | Implementation |
|---|---|---|
| BR-1 | All business numbers from `policy_parameters`; none hardcoded | `PolicyParameters.require_int`; rules read keys at request time |
| BR-2 | Days since delivery = `APP_TODAY - delivery_date` | `rules.enrich_order` |
| BR-3 | Return window inclusive of final day | `days > window` fails, so day 30 passes |
| BR-4 | Return checks in the required order | `rules.evaluate_return` |
| BR-5 | `NON_RETURNABLE` for gift cards/software | `returnable == "no"` check |
| BR-6 | `OPENED_NOT_RETURNABLE` only for opened + `changed_mind` + `unopened_only` | final check in `evaluate_return` |
| BR-7 | Defective opened items can pass the opened-item check | reason guard |
| BR-8 | Warranty length by `warranty_class` | `rules.warranty_months` |
| BR-9 | Warranty ends N calendar months after delivery; final day covered; month-end clamps | `dateutil.relativedelta`, `warranty_active` uses `<=` |
| BR-10 | Warranty ticket requires delivery and at least one covered item | `rules.evaluate_warranty` |
| BR-11 | Cancellation only while `processing`; otherwise `ORDER_ALREADY_SHIPPED` | `rules.evaluate_cancellation` |
| BR-12 | Re-check immediately before every write | `create_*` methods re-run the rule |
| BR-13 | Add UUID ID, current time and required status | `repository.insert_*` |

Verified by `tests/test_business_rules.py`, `tests/test_tools.py` and parameter
override tests that alter `policy_parameters` in a temporary database.

### 2.4 Policy retrieval (RAG)

| ID | Requirement | Implementation |
|---|---|---|
| RAG-1 | Parse all ten documents at top-level headings | `PolicyIndex._split_documents` |
| RAG-2 | Match manifest by `doc_id` | `PolicyIndex._parse` |
| RAG-3 | Exclude superseded documents | `_is_active` requires `status == current` |
| RAG-4 | Exclude future-dated documents from current answers | `_is_active` requires `effective_date <= APP_TODAY` |
| RAG-5 | May mention future changes with start date | `_search_future`; labelled "takes effect on ..." note |
| RAG-6 | Follow policy over FAQ when they differ | policy ranking preference in `PolicyIndex._score`; policy text drives answers |
| RAG-7 | Cite `doc_id` + section; only documents used | `_policy` and `_cite`; agent validates citations against sections it saw |
| RAG-8 | `no_answer` with empty citations when no current document covers the question | `_policy`, coverage threshold; response assembly clears citations |
| RAG-9 | Send only relevant paragraphs, not the whole file | BM25 returns at most four candidates; assistant selects a small subset |

Verified by `tests/test_policy_search.py` and policy conversation tests.

### 2.5 Conversation and safety

| ID | Requirement | Implementation |
|---|---|---|
| CONV-1 | Ask for missing order/item/reason in one `clarify` reply | `_resolve`, `_return`, `_warranty`, `_cancel` |
| CONV-2 | List matching orders and ask which one | `_ask_which_order` |
| CONV-3 | Explicit action request -> proposal; question -> answer + offer | `_return`, `_warranty`, `_cancel`; `nlu.is_action_request` |
| CONV-4 | No proposal when the rules say no | rule result checked before proposal |
| CONV-5 | Set `pending_confirmation` and log a pending write | `_propose`, `ToolContext.log_pending` |
| CONV-6 | Only the very next message can confirm | `Session.pending` cleared on any other message |
| CONV-7 | Clear yes runs the write and re-checks | `_execute_proposal` calls the tool again |
| CONV-8 | Changed details start a new proposal | `_changes_proposal`, `_restart` |
| CONV-9 | "Just do it" is not a yes | `nlu.is_yes` matches only clear affirmatives; tests cover this |
| CONV-10 | One waiting proposal at a time | single `Session.pending` field |
| CONV-11 | Session memory per `session_id` | `SessionStore`, last 40 messages |
| CONV-12 | Off-topic -> `out_of_scope` with empty citations/tool calls | `nlu.looks_off_topic`; response assembly clears both |
| CONV-13 | Exact not-found text and route | `NOT_FOUND_ANSWER`, `_not_found` |
| CONV-14 | One message may ask two things | combined tracking + policy/return/warranty/cancel branch |
| CONV-15 | Dates written clearly, year implied as 2026 | `app/utils.format_date` |

Verified by `tests/test_conversation.py` and `tests/test_api_contract.py`. In
particular, tests assert row counts before and after abandoned proposals and
off-topic messages.

### 2.6 Optional LLM agent

The brief allows an LLM to choose tools. The implementation supports it but keeps
Python in control:

| ID | Requirement | Implementation |
|---|---|---|
| LLM-1 | Send message and tool definitions; loop until final reply | `agent.run` |
| LLM-2 | Stop after a fixed number of tool calls | `MAX_TOOL_CALLS`, plus wall-clock deadline |
| LLM-3 | Model never saves a row directly | write-tool interception in `_handle_write` |
| LLM-4 | Python re-checks rules before saving | deterministic confirmation turn |
| LLM-5 | Only cite sections returned by `search_policy` | `_collect_citations`, `_valid_citations` |
| LLM-6 | Temperature 0 | provider request payloads |
| LLM-7 | Provider failure does not break the service | fallback to deterministic planner |

Verified with a scripted provider in `tests/test_agent.py`. Live provider HTTP
calls were not tested because no API key was available.

## 3. Non-functional requirements

- **Safety over automation:** the deterministic planner is the default and the
  reference path. The optional LLM cannot write.
- **Data preservation:** the original `data/` files are read-only inputs. Tests
  copy the database into temporary directories. The supplied database had zero
  return requests and zero support tickets after all testing.
- **Configuration:** all paths default to `./data`; `APP_TODAY` defaults to
  2026-07-01 and is never replaced by the machine date.
- **Explainability:** rules, retrieval, tools, sessions and API routes live in
  separate files, matching the brief's code-organization request.
- **Determinism:** no wall-clock decisions and temperature 0 for LLM calls.
- **Fail closed:** unknown tool input returns a structured error; provider errors
  fall back; no write occurs without confirmation.

## 4. Decisions and assumptions

1. **Deterministic-first design.** The brief requires an LLM tool loop, but also
   says working safety rules matter most and that any framework or none may be
   used. The deterministic planner is always available and handles confirmations
   and rules. The provider loop is an optional add-on.
2. **`session_id` is caller-owned.** A session belongs to the first customer that
   uses it; a different customer receives 403. Sessions are in-memory and
   process-local, which is sufficient for a single-process assessment service.
3. **Not-found is indistinguishable.** A missing order and another customer's
   order both produce `ORDER_NOT_FOUND` and the exact required sentence.
4. **No answer means no guessing.** Lexical retrieval with a coverage threshold
   intentionally returns `no_answer` for uncovered topics such as gift wrapping
   or price matching.
5. **Policy over FAQ.** A modest ranking preference puts an authoritative policy
   ahead of the FAQ when both cover the query. If the FAQ is the only source (for
   example payment methods), it is still used.
6. **Future documents are notes, not rules.** They never enter normal retrieval
   and are never presented as current. The shipping-fee update is the only case
   where a future note is added, and it is explicitly dated.
7. **Writes store item IDs as JSON text.** The supplied `return_requests.item_ids`
   column is text; a JSON array keeps the list intact without schema changes.
8. **Unknown values fail loudly at the rule layer.** If a required
   `policy_parameters` key is missing, the request is handled as an internal
   error rather than silently using a hardcoded default.
9. **The exact day-30 boundary is inclusive.** `days > window` is the failure
   condition, so 30 passes and 31 fails.
10. **Month-end warranty dates use `relativedelta`.** A warranty starting on
    31 January for one month ends on the last day of February.

## 5. Explicitly out of scope

- User interface, authentication, login and real payments.
- Refund amount calculations.
- Sending email or handing the chat to a human.
- Exchanges as a write action (questions about the exchange window are answered
  from policy).
- Multi-process session sharing or persistent chat history.
- Real provider verification in this environment (no key).

## 6. Traceability summary

| Area | Primary code | Primary tests |
|---|---|---|
| HTTP contract | `app/api/routes/chat.py`, `app/main.py` | `test_api_contract.py` |
| Rules | `app/services/rules.py` | `test_business_rules.py` |
| Tools and persistence | `app/services/tools.py`, `app/db/repository.py` | `test_tools.py` |
| Policy search | `app/services/policy_search.py` | `test_policy_search.py` |
| Conversation safety | `app/services/assistant.py`, `sessions.py`, `nlu.py` | `test_conversation.py` |
| Optional LLM | `app/services/agent.py` | `test_agent.py` |
| Configuration | `app/core/config.py`, `.env.example` | app fixtures, live smoke |
