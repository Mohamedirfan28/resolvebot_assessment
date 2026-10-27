# API specification

## 1. Service

| Item | Value |
|---|---|
| Base URL | `http://localhost:8000` |
| Chat endpoint | `POST /chat` |
| Health endpoint | `GET /health` |
| Content type | `application/json` |
| Authentication | `X-Customer-Id` header (the customer is already logged in) |

OpenAPI is available at `/docs` and `/openapi.json` when the service is running.

---

## 2. `POST /chat`

### 2.1 Headers

| Header | Required | Description |
|---|---|---|
| `Content-Type` | yes | `application/json` |
| `X-Customer-Id` | yes | Authenticated customer ID, for example `C1009` |

The customer ID is never accepted from the body or from a model.

### 2.2 Request body

```json
{
  "session_id": "s1",
  "message": "Where is order SS-10512?"
}
```

| Field | Type | Rules |
|---|---|---|
| `session_id` | string | Required, 1-200 characters, stripped; caller chooses it |
| `message` | string | Required, 1-4000 characters, stripped |

Extra fields are rejected. A new `session_id` starts a new chat. Reusing the same
`session_id` for the same customer continues that chat.

### 2.3 Success response

HTTP 200, `application/json`:

```json
{
  "request_id": "e7191acb-fba1-4ec0-bf82-fceebd856372",
  "answer": "Order SS-10512 is in transit with SwiftPost Logistics. The estimated delivery date is 3 July 2026. Tracking number: SWP311402977.",
  "route": "answer",
  "citations": [],
  "tool_calls": [
    {
      "name": "get_orders",
      "args": {"order_id": "SS-10512"},
      "status": "success"
    }
  ],
  "pending_confirmation": false,
  "session_id": "s1"
}
```

| Field | Type | Meaning |
|---|---|---|
| `request_id` | string | Fresh UUID for this turn |
| `answer` | string | Customer-facing reply |
| `route` | enum | `answer`, `clarify`, `no_answer` or `out_of_scope` |
| `citations` | array | Policy sections used, each `{"doc_id", "section"}` |
| `tool_calls` | array | Every tool used this turn, in order |
| `pending_confirmation` | boolean | True when a proposal is waiting for the next message |
| `session_id` | string | Echo of the request session ID |

### 2.4 Tool-call object

```json
{
  "name": "check_return_eligibility",
  "args": {
    "order_id": "SS-10458",
    "item_ids": ["SS-10458-1"],
    "reason": "changed_mind"
  },
  "status": "success"
}
```

| Field | Type | Meaning |
|---|---|---|
| `name` | string | One of the five tool names |
| `args` | object | Arguments actually used |
| `status` | enum | `success`, `error` or `pending_confirmation` |
| `error_code` | string (optional) | Present when `status` is `error` |

A proposed write appears with `status: "pending_confirmation"`. The write tool is
executed on the next clear-yes turn and then appears with `status: "success"`.
Errors use one of the documented error codes.

### 2.5 Routes

| Route | When | Citation/tool-call rule |
|---|---|---|
| `answer` | Normal answer, proposal, refusal because of a rule, or confirmed action result | May include citations and tool calls |
| `clarify` | More details are needed (order, item, reason, which order) | May include read tool calls |
| `no_answer` | Order not found or no current policy covers the question | `citations` is always `[]` |
| `out_of_scope` | Not about ShopSphere orders or policies | `citations` and `tool_calls` are always `[]` |

The exact not-found reply is:

```text
I couldn't find that order on your account.
```

It is returned with `route: "no_answer"` for a missing order and for an order
that belongs to another customer.

---

## 3. HTTP errors

Only these HTTP error statuses are used:

| Status | Cause | Body |
|---|---|---|
| 400 | Body is not valid JSON, or `session_id`/`message` is missing or invalid | `{"detail": "Invalid request body"}` |
| 401 | `X-Customer-Id` missing or blank | `{"detail": "Missing X-Customer-Id header"}` |
| 401 | Customer ID is not in `customers` | `{"detail": "Unknown X-Customer-Id"}` |
| 403 | `session_id` already belongs to another customer | `{"detail": "This session belongs to another customer"}` |

Business failures (order not found, ineligible return, wrong ticket type, etc.)
are **not** HTTP errors. They return HTTP 200 with a normal response body.

---

## 4. Health check

```http
GET /health
```

```json
{
  "status": "ok",
  "policy_sections": 66,
  "app_today": "2026-07-01"
}
```

`policy_sections` is the number of parsed and indexed sections. It is a useful
startup check for the supplied data.

---

## 5. Session and confirmation semantics

1. A session belongs to the first customer that uses it.
2. Only one proposal waits at a time.
3. `pending_confirmation` is true only on the proposal turn.
4. A clear yes as the **very next message** executes the pending write.
5. Any other message abandons the proposal. A changed detail creates a new
   proposal; an unrelated question is answered normally.
6. "Just do it" alone is not a yes.
7. A question such as "can I return it?" produces an answer and a soft offer.
   Saying yes to the offer creates a proposal, not an immediate write.
8. Confirmed writes return the result of the write tool with `status: "success"`.

Clear affirmatives include yes, yeah, yep, yup, sure, ok/okay, confirm/confirmed,
go ahead, please do, do it, proceed, that works, sounds good, absolutely and
definitely.

---

## 6. Examples

### 6.1 Tracking

Request:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-Customer-Id: C1009" \
  -d '{"session_id":"s1","message":"Where is order SS-10512?"}'
```

Response (HTTP 200):

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
  "session_id": "s1"
}
```

### 6.2 Return proposal

Request:

```json
{
  "session_id": "s1",
  "message": "Return the speaker from SS-10458, I changed my mind."
}
```

Response (HTTP 200):

```json
{
  "request_id": "0d3f...",
  "answer": "You can return Nimbra Smart Speaker from order SS-10458. It was delivered on 21 June 2026 (10 days ago), which is within the 30-day return window. Shall I go ahead and create the return request?",
  "route": "answer",
  "citations": [{"doc_id": "returns_policy", "section": "§2.1"}],
  "tool_calls": [
    {"name": "get_orders", "args": {"order_id": "SS-10458"}, "status": "success"},
    {
      "name": "check_return_eligibility",
      "args": {"order_id": "SS-10458", "item_ids": ["SS-10458-1"], "reason": "changed_mind"},
      "status": "success"
    },
    {"name": "search_policy", "args": {"query": "return window days"}, "status": "success"},
    {
      "name": "create_return_request",
      "args": {"order_id": "SS-10458", "item_ids": ["SS-10458-1"], "reason": "changed_mind"},
      "status": "pending_confirmation"
    }
  ],
  "pending_confirmation": true,
  "session_id": "s1"
}
```

No `return_requests` row exists yet.

### 6.3 Confirmation

Request:

```json
{"session_id": "s1", "message": "Yes, go ahead."}
```

Response (HTTP 200):

```json
{
  "request_id": "a7c1...",
  "answer": "Done - the return request has been created for order SS-10458. Your reference is 96a90230-0e02-4f47-be14-1c97cb6a9ec9. We'll be in touch with the pickup details.",
  "route": "answer",
  "citations": [],
  "tool_calls": [
    {
      "name": "create_return_request",
      "args": {"order_id": "SS-10458", "item_ids": ["SS-10458-1"], "reason": "changed_mind"},
      "status": "success"
    }
  ],
  "pending_confirmation": false,
  "session_id": "s1"
}
```

### 6.4 Policy question

Request:

```json
{"session_id": "p1", "message": "How many days do I have to return something?"}
```

Response (HTTP 200, abbreviated):

```json
{
  "request_id": "b91e...",
  "answer": "ShopSphere Returns Policy §2.1 says: “You can return most items within 30 days of the delivery date for any reason, including a change of mind.”\n\nShopSphere Returns Policy §2.2 says: “Day 0 is the delivery date shown in your order tracking. Day 30 is the last day on which you can start a return. We count calendar days, including weekends and public holidays.”\n\nShopSphere Returns Policy §5.1 says: “If an item is defective or we sent the wrong item, you can return it within the standard return window (§2.1). This applies even to opened earphones and earbuds.”",
  "route": "answer",
  "citations": [
    {"doc_id": "returns_policy", "section": "§2.1"},
    {"doc_id": "returns_policy", "section": "§2.2"},
    {"doc_id": "returns_policy", "section": "§5.1"}
  ],
  "tool_calls": [
    {"name": "search_policy", "args": {"query": "how many days do i have to return something return window days"}, "status": "success"}
  ],
  "pending_confirmation": false,
  "session_id": "p1"
}
```

The quoted text and number come from the supplied policy and
`policy_parameters`, not from a prompt.

### 6.5 Clarification

Request:

```json
{"session_id": "c1", "message": "I want to return my charger."}
```

Response:

```json
{
  "request_id": "c2f0...",
  "answer": "I found that item in more than one order: SS-10503 (Voltix 30W Car Charger); SS-10489 (Nimbra 20W Fast Charger); SS-10471 (Voltix 65W USB-C Charger). Which order do you mean? Also, is this a change of mind, or is the item defective?",
  "route": "clarify",
  "citations": [],
  "tool_calls": [{"name": "get_orders", "args": {}, "status": "success"}],
  "pending_confirmation": false,
  "session_id": "c1"
}
```

The exact wording lists the matching orders and items. The next reply may name
the product variant and reason, for example "the 65W one, I changed my mind".

### 6.6 Order not found

Request:

```json
{"session_id": "n1", "message": "Where is order SS-99999?"}
```

Response (HTTP 200):

```json
{
  "request_id": "d5a2...",
  "answer": "I couldn't find that order on your account.",
  "route": "no_answer",
  "citations": [],
  "tool_calls": [
    {"name": "get_orders", "args": {"order_id": "SS-99999"}, "status": "error", "error_code": "ORDER_NOT_FOUND"}
  ],
  "pending_confirmation": false,
  "session_id": "n1"
}
```

The same answer is returned when the order exists but belongs to another
customer.

### 6.7 Out of scope

Request:

```json
{"session_id": "o1", "message": "What's the weather in Mumbai?"}
```

Response:

```json
{
  "request_id": "eaa1...",
  "answer": "I can only help with ShopSphere orders and policies. I can track an order, start a return, check warranty cover, cancel an order, or explain a store policy.",
  "route": "out_of_scope",
  "citations": [],
  "tool_calls": [],
  "pending_confirmation": false,
  "session_id": "o1"
}
```

### 6.8 HTTP errors

Missing header:

```http
HTTP/1.1 401 Unauthorized
{"detail": "Missing X-Customer-Id header"}
```

Invalid JSON:

```http
HTTP/1.1 400 Bad Request
{"detail": "Invalid request body"}
```

Another customer's session:

```http
HTTP/1.1 403 Forbidden
{"detail": "This session belongs to another customer"}
```

---

## 7. Business error codes

These appear in tool-call `error_code` fields and, where relevant, in the reply.
They do not change the HTTP status.

| Code | Meaning |
|---|---|
| `ORDER_NOT_FOUND` | Order missing or not owned by this customer |
| `INVALID_ITEM` | Item is not in the order, or the item list is empty |
| `INVALID_REASON` | Reason is not `changed_mind` or `defective` |
| `INVALID_TICKET_TYPE` | Ticket type is not `warranty` or `cancellation` |
| `NOT_DELIVERED` | The order has not been delivered |
| `NON_RETURNABLE` | Product is marked `returnable = no` |
| `OPENED_NOT_RETURNABLE` | Opened product is `unopened_only` for a change of mind |
| `RETURN_WINDOW_EXPIRED` | More than `return_window_days` have passed |
| `WARRANTY_EXPIRED` | No selected item is still covered |
| `NO_WARRANTY` | Selected products have no warranty |
| `ORDER_ALREADY_SHIPPED` | Cancellation attempted after processing |

---

## 8. Idempotency and side effects

- Read tools have no side effects.
- A write occurs only on the confirmation turn.
- The confirmation turn re-checks the rules; if they no longer pass, no row is
  written and the reply explains why.
- A proposal abandoned by another message cannot be revived by a later "yes".
- Request IDs are per turn; they are not idempotency keys for writes.
