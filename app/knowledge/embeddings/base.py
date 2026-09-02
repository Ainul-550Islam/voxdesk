"""
Embedding provider interface.

Retrieval code must never name a provider. It asks for `get_embedder()` and
receives something with a model id, a dimension count, and the ability to
embed a list of strings. Swapping OpenAI for a local model is a config change,
not a code change.

The other job of this module is version safety. A vector produced by
`openai:text-embedding-3-small` is meaningless to `hashing-v1`, and mixing
them silently produces a search that returns plausible-looking garbage. So
every provider exposes an `identity` string that is stored next to each vector
and compared before that vector is ever used.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class EmbeddingError(Exception):
    """Embedding failed. The caller decides whether to retry or fail the job."""


class EmbeddingDimensionMismatch(EmbeddingError):
    """A stored vector does not match the configured model. Requires reindex."""


class Embedder(ABC):
    #: Stable provider name, e.g. "hashing" or "openai".
    provider: str = "unknown"
    #: Model identifier, e.g. "hashing-v1" or "text-embedding-3-small".
    model: str = "unknown"
    #: Vector length. Stored per chunk and validated on read.
    dimensions: int = 0

    @property
    def identity(self) -> str:
        """`provider:model:dimensions` -- the compatibility key for vectors."""
        return f"{self.provider}:{self.model}:{self.dimensions}"

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        Embed a batch. Must return one vector per input, in order, each of
        length `self.dimensions`.
        """

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        if not vectors:
            raise EmbeddingError("embedding provider returned no vector")
        return vectors[0]

    def validate(self, vector: list[float]) -> None:
        if len(vector) != self.dimensions:
            raise EmbeddingDimensionMismatch(
                f"vector has {len(vector)} dimensions, "
                f"{self.model} expects {self.dimensions}"
            )


def is_compatible(stored_identity: str | None, embedder: Embedder) -> bool:
    """
    Whether a stored vector may be compared against this embedder's output.

    Deliberately an exact string match. A near-match is still a different
    vector space, and being lenient here is how a silent quality regression
    gets shipped.
    """
    return bool(stored_identity) and stored_identity == embedder.identity
