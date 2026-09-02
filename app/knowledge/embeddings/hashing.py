"""
A deterministic, dependency-free embedder for development and tests.

This is a hashed bag-of-words projection (the "hashing trick"): word and
bigram features are hashed into a fixed number of buckets, weighted by a
sublinear term frequency, and L2-normalized. Cosine similarity between two
such vectors is a real lexical-overlap measure, so retrieval tests can assert
that a relevant chunk outranks an irrelevant one without any network call, any
API key, or any nondeterminism.

What it is not: semantic. It cannot match "how much does it cost" to "our
pricing is". That is precisely why `validate_security()` refuses to boot
production with this provider selected -- it is honest infrastructure for
tests, and a bad answer engine for customers.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata

from app.knowledge.embeddings.base import Embedder

_TOKEN = re.compile(r"[a-z0-9]+")

#: Words carrying no retrieval signal.
#:
#: This list started out deliberately small, on the theory that an aggressive
#: stop list hurts short queries like "are you open". Measurement said
#: otherwise: with function words left in, "what is your return policy on
#: tractors" matched the insurance section on nothing but "your" and "not",
#: scoring above genuinely relevant chunks for other questions. Generic words
#: appear in every chunk, so they contribute similarity without contributing
#: meaning -- the lexical equivalent of a document with no IDF weighting.
#:
#: Content words carry the query instead. "how much does a cleaning cost"
#: reduces to "clean cost", which is exactly the signal that should match.
_STOPWORDS = frozenset("""
a an the this that these those there here
is are was were be been being am s
do does did doing done
have has had having
will would shall should can could may might must
of to in on at for with by from as into onto up down out off over under
about after before between during through against
i you he she it we they me him her us them my your his its our their mine yours
what which who whom whose when where why how
and or but if then than so such nor yet both either neither
no not none very just only also too much many more most some any all each every
other another same own
get got getting go goes going come comes
please thank thanks hello hi hey ok okay yes yeah sure
""".split())


#: Suffixes stripped so "costs", "cost" and "costing" land in the same bucket.
#: Longest first, since "ing" must be tried before "s". This is a crude
#: stemmer, not Porter -- for a lexical dev embedder the win is that a caller
#: asking "how much does cleaning cost" matches a chunk saying "cleaning
#: costs", and that is worth far more than linguistic correctness.
_SUFFIXES = ("ications", "ication", "ations", "ation", "ingly", "ments",
             "ement", "ing", "ies", "ied", "ers", "est", "ly", "ed", "es", "s")
_MIN_STEM = 4


def _stem(token: str) -> str:
    if len(token) <= _MIN_STEM or token.isdigit():
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= _MIN_STEM:
            stem = token[: -len(suffix)]
            # "ies" -> "y" keeps "policies"/"policy" together.
            if suffix == "ies":
                return stem + "y"
            # Undo the doubled consonant in "shipping" -> "shipp" -> "ship".
            if len(stem) > _MIN_STEM and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
                return stem[:-1]
            return stem
    return token


def _tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    return [
        _stem(token)
        for token in _TOKEN.findall(normalized)
        if token not in _STOPWORDS
    ]


def _bucket(feature: str, dimensions: int) -> tuple[int, float]:
    """
    Map a feature to a bucket and a sign.

    The sign comes from a separate bit of the digest, which is the standard
    signed-hashing trick: collisions then cancel out on average instead of
    always adding, keeping the projection roughly unbiased.
    """
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dimensions, 1.0 if (value >> 63) & 1 else -1.0


class HashingEmbedder(Embedder):
    provider = "hashing"

    def __init__(self, dimensions: int = 512, model: str = "hashing-v1") -> None:
        self.dimensions = max(32, int(dimensions))
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_sync(text) for text in texts]

    def _embed_sync(self, text: str) -> list[float]:
        tokens = _tokenize(text)
        if not tokens:
            return [0.0] * self.dimensions

        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        # Bigrams give a little word-order sensitivity, so "open at nine"
        # scores above a chunk that merely contains all three words apart.
        for first, second in zip(tokens, tokens[1:]):
            feature = f"{first}_{second}"
            counts[feature] = counts.get(feature, 0) + 1

        vector = [0.0] * self.dimensions
        for feature, count in counts.items():
            index, sign = _bucket(feature, self.dimensions)
            # Sublinear scaling: the tenth mention of a word says much less
            # than the second.
            vector[index] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]
