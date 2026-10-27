"""Deterministic language helpers.

The planner uses these helpers to turn a message into intents, order/item
references and a return reason. Business decisions remain in ``rules.py``.
"""

from __future__ import annotations

import re

ORDER_ID_RE = re.compile(r"\bSS-\d{4,7}\b", re.IGNORECASE)
ITEM_ID_RE = re.compile(r"\bSS-\d{4,7}-\d+\b", re.IGNORECASE)

YES_RE = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|confirm|confirmed|go ahead|please do|"
    r"do it|proceed|that works|sounds good|yes please|absolutely|definitely)\b"
)
NO_RE = re.compile(
    r"^(no|nope|nah|not now|don't|do not|cancel that|stop|never mind|leave it)\b"
)
QUESTION_RE = re.compile(
    r"^(can i|could i|should i|do i|am i|how do i|how can i|what|which|when|"
    r"where|is it|are they|does|do you|will it|would it|is there|are there|"
    r"why|who)\b"
)
ACTION_RE = re.compile(
    r"\b(please|i want to|i'd like to|i would like to|i need to|can you|"
    r"could you|will you|would you|open a|create a|start a|file a|raise a|"
    r"go ahead|just (?:return|cancel|open)|process (?:the|my|this)|"
    r"return (?:it|them|the|my|this)|send (?:it|them) back|"
    r"cancel (?:it|the|my|this|order)|book (?:a|the) return)\b"
)
IMPERATIVE_RE = re.compile(
    r"^(please\s+)?(return|cancel|open|create|start|file|raise|send)\b"
)

CANCEL_WORDS = re.compile(r"\b(cancel|cancellation|cancelled)\b")
RETURN_WORDS = re.compile(
    r"\b(return|returns|returning|returnable|send .{0,12}back|refund)\b"
)
WARRANTY_WORDS = re.compile(
    r"\b(warranty|guarantee|guaranteed|covered|coverage|claim|faulty)\b"
)
TRACK_WORDS = re.compile(
    r"\b(where(?:'s| is)|track|tracking|status|eta|when will|has .{0,12}shipped|"
    r"delivery date|arrive|arriving|delivered|lost parcel|delay|delayed)\b"
)
POLICY_WORDS = re.compile(
    r"\b(policy|policies|how many|how long|how much|what is|what's|do you|"
    r"does shopsphere|am i allowed|is there|are there|fee|fees|charge|charges|"
    r"cost|window|exchange|delay|delays|refunds?|payment|pay|price|discount|"
    r"address|plus|international|outside india)\b"
)
OFF_TOPIC_WORDS = re.compile(
    r"\b(weather|forecast|temperature|poem|joke|story|sing|song|movie|film|"
    r"news|cricket|football|stock|bitcoin|recipe|capital of|president|"
    r"prime minister|translate|homework|math|who are you|what are you|"
    r"tell me about yourself)\b"
)
GREETING_RE = re.compile(
    r"^(hi|hello|hey|good morning|good afternoon|good evening|namaste)\b"
)

CHANGED_MIND_PHRASES = (
    "changed my mind",
    "change my mind",
    "don't want",
    "do not want",
    "no longer need",
    "no longer want",
    "ordered by mistake",
    "bought by mistake",
    "don't like",
    "do not like",
    "not what i wanted",
)
DEFECTIVE_PHRASES = (
    "broken",
    "faulty",
    "defective",
    "damaged",
    "damage",
    "not working",
    "doesn't work",
    "does not work",
    "stopped working",
    "stopped charging",
    "not charging",
    "won't turn on",
    "will not turn on",
    "wrong item",
    "wrong product",
    "not as described",
    "stopped responding",
)

CATEGORY_WORDS = {
    "in_ear": ("earbud", "earbuds", "earphone", "earphones", "in-ear", "in ear", "buds"),
    "headphones": ("headphone", "headphones", "headset", "over-ear", "on-ear"),
    "laptop": ("laptop", "notebook", "macbook"),
    "phone": ("phone", "smartphone", "mobile"),
    "smart_home": (
        "speaker", "lamp", "smart plug", "bulb", "display", "camera",
        "doorbell", "purifier",
    ),
    "gaming": (
        "console", "controller", "gaming", "racing wheel", "dock",
        "capture card",
    ),
    "accessory": (
        "charger", "cable", "power bank", "powerbank", "adapter", "mouse",
        "keyboard", "sleeve", "case", "hub", "screen guard", "webcam",
    ),
    "gift_card": ("gift card", "giftcard", "voucher"),
    "software": (
        "software", "licence", "license", "antivirus", "vpn", "backup",
        "office suite",
    ),
}

ITEMISH_WORDS = {
    word
    for words in CATEGORY_WORDS.values()
    for phrase in words
    for word in phrase.split()
} | {
    "item", "product", "order", "accessory", "device", "purchase", "thing",
}

NAME_STOPWORDS = {
    "the", "a", "an", "my", "this", "that", "it", "them", "please", "from",
    "order", "and", "for", "with", "just", "only", "also", "both", "all",
    "one", "two", "three", "new", "old", "pack", "set",
}

STORE_WORDS = {
    "order", "orders", "return", "refund", "warranty", "delivery", "deliver",
    "shipping", "ship", "cancel", "product", "item", "payment", "address",
    "tracking", "ticket", "policy", "policies", "purchase", "bought",
    "billing", "invoice", "shopsphere", "store", "account", "help",
    "support", "assistant", "plus", "membership", "exchange", "parcel",
    "courier", "arrive", "arriving", "damaged", "defective", "broken",
    "faulty", "claim", "delayed", "late", "lost", "pay", "price", "cost",
    "fee", "charge", "discount", "payment", "refund",
} | ITEMISH_WORDS

# Words that show a message is genuinely about the store. They are used to
# decide whether an off-topic phrase ("the weather in ...") is the main point
# of a message. Product nouns alone are deliberately not enough here because
# words such as "in" and "case" also occur in ordinary conversation.
STRONG_STORE_WORDS = {
    "order", "orders", "return", "returns", "refund", "refunds", "warranty",
    "cancel", "cancellation", "delivery", "deliver", "shipping", "ship",
    "track", "tracking", "payment", "pay", "address", "invoice", "billing",
    "shopsphere", "store", "support", "ticket", "parcel", "courier",
    "exchange", "claim", "repair",
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ss"):
        return word
    if word.endswith("es") and len(word) > 3:
        return word[:-2]
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    return [
        singular(word)
        for word in re.findall(r"[a-z0-9]+", normalize(text))
        if len(word) > 1
    ]


def extract_order_ids(text: str) -> list[str]:
    seen: list[str] = []
    for match in ORDER_ID_RE.findall(text or ""):
        value = match.upper()
        if value not in seen:
            seen.append(value)
    return seen


def extract_item_ids(text: str) -> list[str]:
    seen: list[str] = []
    for match in ITEM_ID_RE.findall(text or ""):
        value = match.upper()
        if value not in seen:
            seen.append(value)
    return seen


def is_yes(text: str) -> bool:
    return bool(YES_RE.match(normalize(text)))


def is_no(text: str) -> bool:
    return bool(NO_RE.match(normalize(text)))


def is_question(text: str) -> bool:
    normalized = normalize(text)
    return "?" in (text or "") or bool(QUESTION_RE.match(normalized))


def is_action_request(text: str) -> bool:
    normalized = normalize(text)
    if "just do it" in normalized or "do it now" in normalized:
        # This is still an action request, but the planner never treats it as
        # confirmation by itself.
        return True
    if QUESTION_RE.match(normalized) and "please" not in normalized:
        return False
    if ACTION_RE.search(normalized):
        return True
    if IMPERATIVE_RE.match(normalized) and not normalized.startswith(
        ("return policy", "returns policy", "cancellation policy", "refund policy")
    ):
        return True
    return False


def detect_reason(text: str) -> str | None:
    normalized = normalize(text)
    if any(phrase in normalized for phrase in DEFECTIVE_PHRASES):
        return "defective"
    if any(phrase in normalized for phrase in CHANGED_MIND_PHRASES):
        return "changed_mind"
    return None


def detect_intents(text: str) -> dict:
    normalized = normalize(text)
    return {
        "cancel": bool(CANCEL_WORDS.search(normalized)),
        "return": bool(RETURN_WORDS.search(normalized)),
        "warranty": bool(WARRANTY_WORDS.search(normalized)),
        "track": bool(TRACK_WORDS.search(normalized)),
        "policy": bool(POLICY_WORDS.search(normalized)),
        "off_topic": bool(OFF_TOPIC_WORDS.search(normalized)),
        "greeting": bool(GREETING_RE.match(normalized)),
        "action": is_action_request(normalized),
        "question": is_question(normalized),
        "order_ids": extract_order_ids(text),
        "item_ids": extract_item_ids(text),
        "reason": detect_reason(text),
    }


def has_store_words(text: str) -> bool:
    raw = set(re.findall(r"[a-z0-9-]+", normalize(text)))
    if raw & STORE_WORDS:
        return True
    sing = {singular(token) for token in raw if len(token) > 3}
    return bool(sing & STORE_WORDS)


def mentions_item_like(text: str) -> bool:
    raw = set(re.findall(r"[a-z0-9-]+", normalize(text)))
    sing = {singular(token) for token in raw}
    return bool(sing & ITEMISH_WORDS) or bool(extract_item_ids(text))


def mentions_product(text: str) -> bool:
    generic = {"item", "product", "order", "accessory", "device", "purchase", "thing"}
    raw = set(re.findall(r"[a-z0-9-]+", normalize(text)))
    sing = {singular(token) for token in raw}
    return bool((sing & ITEMISH_WORDS) - generic) or bool(extract_item_ids(text))


def looks_off_topic(text: str) -> bool:
    normalized = normalize(text)
    if OFF_TOPIC_WORDS.search(normalized):
        raw = set(re.findall(r"[a-z0-9-]+", normalized))
        if not (raw & STRONG_STORE_WORDS or extract_order_ids(text)):
            return True
    if (
        GREETING_RE.match(normalized)
        and len(normalized.split()) <= 4
        and not has_store_words(text)
    ):
        return True
    return False


def _name_tokens(name: str) -> set[str]:
    return {
        singular(token)
        for token in re.findall(r"[a-z0-9]+", (name or "").lower())
        if token not in NAME_STOPWORDS and len(token) > 2
    }


def match_items(order: dict, text: str) -> list[dict]:
    """Return items in one order that the message most plausibly mentions."""
    normalized = normalize(text)
    explicit = set(extract_item_ids(text))
    if explicit:
        return [
            item
            for item in order.get("items", [])
            if item["item_id"].upper() in explicit
        ]

    if re.search(r"\b(all|everything|whole order|entire order|both items)\b", normalized):
        return list(order.get("items", []))

    query_tokens = set(tokens(text))
    name_matches: list[dict] = []
    category_matches: list[dict] = []

    for item in order.get("items", []):
        name_tokens = _name_tokens(item.get("name", ""))
        if query_tokens & name_tokens:
            name_matches.append(item)
            continue
        category = str(item.get("category") or "")
        phrases = CATEGORY_WORDS.get(category, ())
        if any(phrase in normalized for phrase in phrases):
            category_matches.append(item)

    return name_matches or category_matches


def item_mentioned_in_text(item: dict, text: str) -> bool:
    normalized = normalize(text)
    if item["item_id"].lower() in normalized:
        return True
    query_tokens = set(tokens(text))
    if query_tokens & _name_tokens(item.get("name", "")):
        return True
    category = str(item.get("category") or "")
    return any(phrase in normalized for phrase in CATEGORY_WORDS.get(category, ()))


def asks_for_action_verb(text: str) -> bool:
    """Whether the message explicitly asks to create the action now."""
    normalized = normalize(text)
    if re.search(r"\b(just do it|do it now)\b", normalized):
        return True
    return is_action_request(text)
