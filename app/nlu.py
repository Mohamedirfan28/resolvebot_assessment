"""Small deterministic language helpers.

This is deliberately not a second LLM: it turns a customer message into
intents, order/item mentions and a return reason. The planner then applies the
business rules. Keeping this deterministic makes the graded behaviours stable.
"""

from __future__ import annotations

import re

ORDER_ID_RE = re.compile(r"\b(SS-\d{4,7})\b", re.IGNORECASE)
ITEM_ID_RE = re.compile(r"\b(SS-\d{4,7}-\d+)\b", re.IGNORECASE)

YES_RE = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|confirm|confirmed|go ahead|please do|"
    r"do it|proceed|that works|sounds good|yes please|absolutely|definitely)\b"
)
NO_RE = re.compile(
    r"^(no|nope|nah|not now|don't|do not|cancel that|stop|never mind|leave it)\b"
)
ACTION_RE = re.compile(
    r"\b(please|i want to|i'd like to|i would like to|i need to|can you|"
    r"could you|will you|would you|open a|create a|start a|file a|raise a|"
    r"go ahead|just (?:return|cancel|open)|process (?:the|my|this)|"
    r"return (?:it|them|the|my)|send (?:it|them) back|cancel (?:it|the|my|order))\b"
)
QUESTION_RE = re.compile(
    r"^(can i|could i|should i|do i|how do i|how can i|what|which|when|where|"
    r"is it|are they|am i|does|do you|will it|would it)\b"
)

CANCEL_WORDS = re.compile(r"\b(cancel|cancellation|cancelled)\b")
RETURN_WORDS = re.compile(r"\b(return|returns|returning|send .{0,12}back|refund)\b")
WARRANTY_WORDS = re.compile(
    r"\b(warranty|guarantee|guaranteed|covered|coverage|claim)\b"
)
TRACK_WORDS = re.compile(
    r"\b(where(?:'s| is)|track|tracking|status|eta|when will|has .{0,12}shipped|"
    r"delivery date|arrive|arriving|delivered)\b"
)
POLICY_WORDS = re.compile(
    r"\b(policy|policies|how many|how long|how much|what is|what's|do you|"
    r"does shopsphere|am i allowed|is there|are there|fee|fees|charge|charges|"
    r"cost|window|exchange|lost|delay|delays|refunds?)\b"
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
)

# Category words from the products schema. Only categories whose everyday name is
# generic enough to stand in for the product name are used as a fallback; for
# mixed categories (accessory, smart_home, gaming) the product name must match so
# that "charger" never selects a mouse.
CATEGORY_WORDS = {
    "in_ear": ("earbud", "earbuds", "earphone", "earphones", "in-ear", "in ear"),
    "headphones": ("headphone", "headphones", "headset"),
    "laptop": ("laptop", "notebook"),
    "phone": ("phone", "smartphone", "mobile"),
    "smart_home": ("speaker", "smart speaker", "home"),
    "gaming": ("console", "controller", "gaming"),
    "accessory": (
        "charger", "cable", "power bank", "powerbank", "adapter", "mouse", "keyboard",
    ),
    "gift_card": ("gift card", "giftcard", "voucher"),
    "software": ("software", "licence", "license", "antivirus"),
}

CATEGORY_FALLBACK = {"in_ear", "headphones", "laptop", "phone", "gift_card", "software"}

ITEMISH_WORDS = {
    word
    for words in CATEGORY_WORDS.values()
    for phrase in words
    for word in phrase.split()
} | {"item", "product", "order"}

# Words that do not help match a product name.
NAME_STOPWORDS = {
    "the", "a", "an", "my", "this", "that", "it", "them", "please", "from",
    "order", "and", "for", "with", "just", "only", "also", "both",
}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def extract_order_ids(text: str) -> list[str]:
    return [match.upper() for match in ORDER_ID_RE.findall(text or "")]


def extract_item_ids(text: str) -> list[str]:
    return [match.upper() for match in ITEM_ID_RE.findall(text or "")]


def is_yes(text: str) -> bool:
    return bool(YES_RE.match(normalize(text)))


def is_no(text: str) -> bool:
    return bool(NO_RE.match(normalize(text)))


def is_action_request(text: str) -> bool:
    normalized = normalize(text)
    if not ACTION_RE.search(normalized):
        return False
    if QUESTION_RE.match(normalized) and "please" not in normalized:
        return False
    return True


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
        "order_ids": extract_order_ids(text),
        "item_ids": extract_item_ids(text),
        "reason": detect_reason(text),
    }


def has_store_words(text: str) -> bool:
    words = {
        "order", "orders", "return", "refund", "warranty", "delivery", "deliver",
        "shipping", "ship", "cancel", "product", "item", "payment", "address",
        "tracking", "ticket", "policy", "policies", "purchase", "bought",
        "billing", "invoice", "shopsphere", "store", "account", "help",
        "support", "assistant",
    } | ITEMISH_WORDS
    tokens = set(re.findall(r"[a-z0-9-]+", normalize(text)))
    if tokens & words:
        return True
    # Accept simple plurals: "refunds", "returns", "orders".
    singular = {token[:-1] for token in tokens if len(token) > 3 and token.endswith("s")}
    return bool(singular & words)


def _name_tokens(name: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", (name or "").lower())
        if token not in NAME_STOPWORDS and len(token) > 2
    ]


def match_items(order: dict, text: str) -> list[dict]:
    """Return items in one order that the message seems to mention."""
    normalized = normalize(text)
    explicit = set(extract_item_ids(text))
    query_tokens = set(re.findall(r"[a-z0-9]+", normalized))

    if explicit:
        return [
            item for item in order.get("items", [])
            if item["item_id"].upper() in explicit
        ]

    name_matches: list[dict] = []
    category_matches: list[dict] = []
    for item in order.get("items", []):
        tokens = _name_tokens(item.get("name", ""))
        if any(token in query_tokens for token in tokens):
            name_matches.append(item)
            continue
        phrases = CATEGORY_WORDS.get(item.get("category", ""), ())
        if item.get("category") in CATEGORY_FALLBACK and any(
            phrase in normalized for phrase in phrases
        ):
            category_matches.append(item)
    return name_matches or category_matches


def mentions_item_like(text: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9-]+", normalize(text)))
    return bool(tokens & ITEMISH_WORDS) or bool(extract_item_ids(text))


def looks_off_topic(text: str) -> bool:
    normalized = normalize(text)
    if OFF_TOPIC_WORDS.search(normalized):
        return True
    if GREETING_RE.match(normalized) and len(normalized.split()) <= 4:
        return True
    return False
