# Architecture

## 1. Overview

ResolveBot is a single-process FastAPI service. The design deliberately separates
four concerns that the brief calls out:

- **API/transport:** header validation, JSON validation, HTTP status mapping.
- **Tools/persistence:** owner-scoped reads and re-checked writes.
- **Business rules:** pure Python decisions using `policy_parameters` values.
- **Conversation policy:** intent handling, clarification and propose/confirm.

Policy retrieval is a separate lexical index built at startup. An optional LLM
tool loop can choose tools, but it cannot make decisions or write rows.

```text
Client
  |
  | POST /chat  (X-Customer-Id, session_id, message)
  v
FastAPI route  app/api/routes/chat.py
  |-- header/body validation -> 400/401
  |-- session ownership check -> 403
  v
Assistant  app/services/assistant.py
  |-- NLU helpers  app/services/nlu.py
  |-- ToolContext  app/services/tools.py
  |     |-- repository  app/db/repository.py  -> SQLite
  |     |-- rules       app/services/rules.py
  |     `-- PolicyIndex app/services/policy_search.py
  |-- SessionStore  app/services/sessions.py  (in-memory)
  `-- optional LLM loop  app/services/agent.py
  v
ChatResult -> JSON response (always HTTP 200 after validation)
```

## 2. Components

### 2.1 Application factory and lifecycle (`app/main.py`)

At startup the lifespan handler:

1. loads settings from environment variables and optional `.env`;
2. configures logging;
3. parses policies and the manifest into a `PolicyIndex`;
4. creates the `SessionStore`;
5. creates the `Assistant`.

The index is immutable after startup and shared by requests. The session store is
also shared, but guarded by a lock. The app factory is used directly by tests so
each test gets isolated state and a private database copy.

### 2.2 HTTP layer (`app/api/routes/chat.py`)

The route intentionally avoids FastAPI's automatic 422 for a chat-body validation
failure:

- missing/blank `X-Customer-Id` -> 401;
- invalid JSON -> 400;
- missing/invalid `session_id` or `message` -> 400;
- unknown customer -> 401;
- session owned by a different customer -> 403;
- everything else -> 200, including business errors.

`_process` runs in a worker thread through `run_in_threadpool` because SQLite is
blocking. It opens one connection per request and always closes it.

The route assembles the exact response shape required by the brief, adding
`session_id` for convenience. `request_id` is a fresh UUID per turn.

### 2.3 Schemas (`app/schemas/chat.py`)

Pydantic models validate:

- `session_id`: 1-200 characters, whitespace-trimmed;
- `message`: 1-4000 characters, whitespace-trimmed;
- `route`: one of the four allowed values;
- tool-call status: `success`, `error` or `pending_confirmation`.

Extra request fields are rejected. The response is assembled as a dictionary to
avoid serialization surprises.

### 2.4 Sessions (`app/services/sessions.py`)

`SessionStore` maps `session_id` to a `Session`. It is:

- **ownership-checked:** the first customer to use a session owns it; a different
  customer gets `SessionConflict`, mapped to 403;
- **bounded by message count:** the last 40 messages are retained for context;
- **single-proposal:** `Session.pending` holds at most one `Proposal`;
- **lock-protected** for lookup/creation.

A `Proposal` contains the action kind, order, item IDs and names, reason, summary,
citations, tool name and tool arguments. `Session.offer` represents a soft offer
created by a question such as "can I return this?". `Session.clarification`
remembers a missing-order/item/reason question so the next reply can continue it.

Sessions are in-memory by design. A restart starts new chats.

### 2.5 NLU helpers (`app/services/nlu.py`)

These are deterministic text helpers, not business rules:

- normalize text, singularize tokens, match product names/categories;
- extract order IDs (`SS-10458`) and item IDs (`SS-10458-1`);
- detect intents (track, return, warranty, cancel, policy);
- detect a return reason (`changed_mind` or `defective`);
- detect clear yes/no and action requests;
- decide whether a message is off-topic or store-related.

The detected intents are inputs to the planner; they never decide eligibility.

### 2.6 Tools (`app/services/tools.py`)

`ToolContext` is created per request and owns:

- the SQLite connection;
- the authenticated `customer_id`;
- the startup `PolicyIndex`;
- `APP_TODAY` and `MAX_TOOL_CALLS`;
- a typed `PolicyParameters` view of `policy_parameters`;
- the ordered `calls` log.

Read tools:

- `get_orders` filters by customer first, then optional order/status; enriches
  each order with delivery and warranty fields;
- `search_policy` returns at most six sections with doc metadata and scores;
- `check_return_eligibility` loads the owner-scoped order and runs
  `rules.evaluate_return`.

Write tools:

- `create_return_request` loads the order again, re-runs eligibility, then inserts
  a row. The write is refused if the second check fails.
- `create_support_ticket` validates the ticket type, loads the order again, runs
  cancellation or warranty rules, then inserts.

Every call is logged before the response is assembled. `ToolLimitExceeded` is
raised if a message exceeds the configured budget.

### 2.7 Business rules (`app/services/rules.py`)

`PolicyParameters` exposes typed access to the `policy_parameters` table:
`get`, `get_int`, `require_int` and `source`. `require_int` raises
`PolicyParameterError` if a required number is missing or malformed; the service
fails closed instead of using a hardcoded value.

`enrich_order` adds computed fields without mutating the database:

- delivery date, days since delivery, shipment status, ETA, carrier, tracking;
- per item: warranty months, warranty end, active flag, opened flag, quantity.

`evaluate_return` applies the four checks in order and stops at the first
failure. `evaluate_warranty` classifies each item as covered, expired or
no-warranty and succeeds if at least one is covered. `evaluate_cancellation`
allows only `processing`.

### 2.8 Policy index (`app/services/policy_search.py`)

Parsing:

1. split the Markdown at top-level `#` headings;
2. find `Document ID` in each document;
3. match that ID in `manifest.json`;
4. take status/effective date/title/doc type from the manifest, with document
   metadata as fallback;
5. split the body at numbered headings into `PolicyChunk` objects.

Indexing:

- tokenization lowercases, strips punctuation and stop words, and applies light
  stemming;
- document title and heading tokens are weighted more heavily than body tokens;
- BM25 statistics (term frequency, document frequency, average length) are
  computed once.

Filtering:

- `search` only considers `status == current` and
  `effective_date <= APP_TODAY`;
- `search_future` only considers current documents effective after `APP_TODAY`;
- superseded documents are never in either set.

Ranking:

- BM25 score;
- phrase bonus when the full query appears in the text;
- minimum query-term coverage (a phrase hit bypasses coverage);
- small policy-over-FAQ factor so the authoritative document wins when both
  cover the same question.

The assistant selects the top section, closely scoring sections from the same
document, and at most one near-equal section from another document. This keeps
answers short while allowing a rule and its exception to appear together.

### 2.9 Assistant (`app/services/assistant.py`)

The assistant is the conversation state machine. Its order of operations is
safety-critical:

1. Record the user message.
2. Off-topic messages are rejected immediately, clearing any waiting proposal or
   offer so a later "yes" cannot write.
3. If the optional agent is enabled and no confirmation/offer is waiting, run the
   LLM loop; on failure, fall back to the deterministic planner.
4. Otherwise run the deterministic planner:
   - a pending proposal survives only the next message;
   - a clear yes executes it;
   - no, changed details, or another message abandons it;
   - an unrelated tracking/policy question is answered normally;
   - a soft offer can be accepted with yes;
   - a clarification flow continues when the reply adds details;
   - general greetings and off-topic text are refused;
   - product availability becomes `no_answer`;
   - policy-only questions go to the policy path;
   - combined tracking + second intent answers both;
   - otherwise dispatch by intent, with a bare order ID treated as tracking.
5. Clear citations/tool calls for `out_of_scope` and citations for `no_answer`.
6. Record the assistant reply.

The planner resolves orders and items through `_resolve`:

- explicit order ID first, else an item ID's order prefix, else a single matching
  order, else clarify;
- explicit item IDs, else product/category matching, else the only item, else
  clarify.

Before proposing a write it calls the read-side rule check. Before executing a
confirmed write it calls the write tool, which checks again.

### 2.10 Optional LLM layer (`app/services/agent.py`)

`agent.run` builds a provider-neutral message list with a system prompt and the
five tool specifications. It loops until the model returns no tool calls or the
tool/time budget is exhausted.

Provider adapters convert the neutral format to OpenAI, Anthropic and Google
payloads and back. All use temperature 0.

Safety interception:

- read tools execute and their results are appended to the tool history;
- `create_return_request` and `create_support_ticket` are **not executed**;
  `_handle_write` validates the arguments and rules and creates a `Proposal`;
- if the user only asked a question, the write call is rejected with an
  instruction to answer and offer instead;
- the model's citations are filtered to sections returned by `search_policy` in
  that turn;
- `order not found` is remembered and forces the exact not-found reply;
- any provider exception falls back to the deterministic planner.

The next user turn is a confirmation turn, so the LLM is bypassed and the
deterministic planner re-checks and writes.

### 2.11 Persistence (`app/db/repository.py`)

Direct `sqlite3`, one connection per request, `Row` factory and foreign keys on.
No ORM or migration framework: the supplied schema is authoritative.

Customer-facing read functions always include `customer_id = ?`:

- `fetch_orders`;
- `customer_exists`.

Inserts generate a UUID and UTC ISO timestamp and set the required status:
`requested` for returns, `open` for tickets. `item_ids` is serialized as a JSON
array into the supplied text column.

## 3. Request sequences

### 3.1 Tracking

```text
message -> get_orders(order_id) -> owner check -> enrich
        -> status/ETA/tracking reply, route answer
```

### 3.2 Return proposal and confirmation

```text
Turn 1:
  resolve order/items/reason
  get_orders -> check_return_eligibility
  create_return_request is logged as pending_confirmation; no INSERT
  reply asks "Shall I go ahead?", pending_confirmation=true

Turn 2 (clear yes):
  deterministic confirmation path
  create_return_request re-loads the order and re-runs evaluate_return
  INSERT one row, status requested
  tool_calls shows success, pending_confirmation=false
```

### 3.3 Policy question

```text
message -> strip order/item IDs -> query expansion
        -> PolicyIndex.search (current + effective only)
        -> select one to three sections
        -> quote text, cite doc_id + section
        -> optionally add a dated future-change note
```

### 3.4 Not found

```text
order ID -> get_orders scoped by customer -> no row
        -> ORDER_NOT_FOUND
        -> answer exactly "I couldn't find that order on your account."
        -> route no_answer, citations []
```

## 4. Data model

Read-only supplied tables:

- `customers(customer_id, full_name, email, city)`
- `products(product_id, name, category, price, returnable, warranty_class)`
- `orders(order_id, customer_id, order_date, status, total)`
- `order_items(item_id, order_id, product_id, qty, unit_price, opened)`
- `shipments(order_id, carrier, status, delivered_at, eta)`
- `policy_parameters(key, value, source_doc_id, source_section)`

Written tables:

- `return_requests(request_id, order_id, customer_id, item_ids, reason, status,
  created_at)`
- `support_tickets(ticket_id, order_id, customer_id, ticket_type, summary,
  status, created_at)`

The code does not alter the schema. It reads only the columns named in the
supplied schema and rules.

## 5. Safety invariants

1. **No write without a yes.** A write tool is only executed from the
   confirmation path after a clear yes on the immediately preceding turn.
2. **Rules decide, prose explains.** Eligibility and cancellation decisions are
   in `rules.py`, not in a prompt.
3. **Owner scoping is mandatory.** Every order lookup includes the header
   customer; tools never receive a customer ID from a model.
4. **Missing and foreign orders look identical.** Both are `ORDER_NOT_FOUND` with
   the same reply.
5. **Re-check before write.** The write tools re-run the rules even after an
   earlier eligibility check.
6. **No stale proposals.** Off-topic, unrelated, negative or detail-changing
   messages clear the waiting proposal.
7. **Current policy only.** Superseded and future documents cannot become current
   answers; future documents are labeled with their start date.
8. **Citations are grounded.** Deterministic citations come from retrieved
   chunks; LLM citations are filtered against the sections it actually saw.
9. **Out-of-scope replies are empty.** `citations` and `tool_calls` are forcibly
   cleared.
10. **Fail closed.** Rule/parameter/tool errors produce an explanation and no
    write.

## 6. Concurrency and failure handling

- SQLite uses per-request connections and a 10-second busy timeout.
- The session store uses a lock around lookup/creation.
- The policy index is read-only after startup.
- A tool-budget overrun returns a polite limit message.
- An unexpected exception is logged and returns a safe 200 reply.
- LLM provider errors fall back to deterministic handling; if the planner also
  fails, the service returns a generic safe message with no write.
- No retries are performed for writes, avoiding duplicate rows.

## 7. Trade-offs

- **Lexical retrieval instead of embeddings:** no model download, fast startup,
  deterministic and good on the supplied vocabulary. The cost is weaker
  paraphrase handling.
- **In-memory sessions:** simple and fast for a single process; not durable or
  shared.
- **Deterministic-first:** less flexible conversationally than a pure agent, but
  auditable and safe. The optional LLM adds flexibility without gaining write
  access.
- **No ORM:** fewer dependencies and no hidden queries; repository functions are
  explicit SQL and easy to review.
