"""
When to retrieve.

Retrieval is not free: an embedding call plus a vector search inside a 1.5
second budget, on every turn, while a caller waits. Most turns of a phone call
do not need it -- "yeah", "okay", "Tuesday works", "my name is Sarah" carry no
question about the business.

So there is an explicit, testable policy rather than an implicit one. It is
intentionally cheap and conservative: a keyword-and-shape heuristic, no model
call, and it errs toward retrieving. A wasted lookup costs a few milliseconds;
a skipped lookup costs a wrong answer.

The agent's own tool call remains the primary signal. This gate exists for the
paths that consider retrieving without one, and to keep the "should we?"
decision in one reviewable place.
"""
from __future__ import annotations

import re

#: Turns this short are acknowledgements, not questions.
_MIN_CHARS = 8
_MIN_WORDS = 3

_QUESTION_WORDS = (
    "what", "when", "where", "which", "who", "why", "how", "do you", "does",
    "can you", "could you", "are you", "is there", "have you", "any",
)

#: Topics a caller asks about that are business facts, therefore must come
#: from documents rather than from the model's imagination.
_FACT_TOPICS = (
    "price", "cost", "fee", "charge", "rate", "quote", "how much",
    "hour", "open", "close", "time", "schedule", "availab",
    "polic", "refund", "cancel", "warrant", "guarantee", "term",
    "address", "location", "park", "direction", "where are you",
    "insur", "accept", "cover", "payment", "deposit",
    "service", "offer", "provide", "product", "package", "plan",
    "require", "bring", "need", "document", "qualif",
    "deliver", "ship", "turnaround", "how long", "lead time",
    "contact", "email", "phone", "number",
)

#: Pure conversational filler. Present as a fast reject so the common case
#: costs one set lookup.
_FILLER = frozenset("""
yes yeah yep no nope ok okay sure thanks thank you hi hello hey bye goodbye
right got it mhm uh huh sorry pardon what please wait hold on one sec
""".split())

_WORD = re.compile(r"[a-z']+")


def should_retrieve(utterance: str) -> bool:
    """
    Whether this caller turn is worth a knowledge lookup.

    Returns True for anything that looks like a question about the business,
    False for acknowledgements, greetings and scheduling chatter that the
    booking tools already handle.
    """
    if not utterance:
        return False

    text = utterance.strip().lower()
    if len(text) < _MIN_CHARS:
        return False

    words = _WORD.findall(text)
    if not words:
        return False
    # "okay thanks", "yeah sure" -- nothing but filler.
    if all(word in _FILLER for word in words):
        return False
    if len(words) < _MIN_WORDS and "?" not in text:
        return False

    if any(topic in text for topic in _FACT_TOPICS):
        return True
    if "?" in text:
        return True
    if any(text.startswith(word) for word in _QUESTION_WORDS):
        return True

    # Statements with no question shape: let the booking flow handle them.
    return False
