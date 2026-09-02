"""
OpenAI embeddings.

Written against the HTTP API with httpx rather than the openai SDK: the SDK is
a large dependency to carry for one endpoint, and this keeps the provider
swappable for any OpenAI-compatible server (vLLM, LiteLLM, Together, a local
gateway) by changing the base URL.

Not exercised in this environment -- no API key and no network. The retry and
timeout behaviour below is written defensively for that reason.
"""
from __future__ import annotations

import asyncio

import structlog

from app.knowledge.embeddings.base import Embedder, EmbeddingError

log = structlog.get_logger()

#: Published output dimensions, used to validate configuration early rather
#: than discovering a mismatch after embedding a thousand chunks.
KNOWN_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

_MAX_ATTEMPTS = 3


class OpenAIEmbedder(Embedder):
    provider = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int = 1536,
        *,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 20.0,
        batch_size: int = 32,
    ) -> None:
        if not api_key:
            raise EmbeddingError("OpenAI embeddings require an API key")
        self._api_key = api_key
        self.model = model
        self.dimensions = int(dimensions)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._batch_size = max(1, batch_size)

        native = KNOWN_DIMENSIONS.get(model)
        # The v3 models support Matryoshka truncation, so a smaller configured
        # dimension is legitimate. A larger one is a configuration error.
        if native and self.dimensions > native:
            raise EmbeddingError(
                f"{model} produces at most {native} dimensions but "
                f"knowledge_embedding_dimensions is {self.dimensions}. "
                f"The default is tuned for the hashing embedder; set it to "
                f"{native} for this model."
            )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start:start + self._batch_size]
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        try:
            import httpx
        except ImportError as exc:      # pragma: no cover - deployment issue
            raise EmbeddingError("httpx is required for OpenAI embeddings") from exc

        payload: dict = {
            # The API rejects empty strings, so a blank chunk is sent as a
            # single space and comes back as a near-zero vector.
            "input": [text if text.strip() else " " for text in batch],
            "model": self.model,
        }
        if KNOWN_DIMENSIONS.get(self.model, 0) != self.dimensions:
            payload["dimensions"] = self.dimensions

        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        f"{self._base_url}/embeddings",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
                if response.status_code in (429, 500, 502, 503, 504):
                    raise EmbeddingError(f"provider returned {response.status_code}")
                if response.status_code >= 400:
                    # 4xx other than rate limiting will not improve on retry.
                    raise EmbeddingError(
                        f"embedding request rejected ({response.status_code})"
                    ) from None
                data = response.json().get("data") or []
                if len(data) != len(batch):
                    raise EmbeddingError("provider returned the wrong vector count")
                # Order is guaranteed by index, not by position in the array.
                ordered = sorted(data, key=lambda item: item.get("index", 0))
                vectors = [item["embedding"] for item in ordered]
                for vector in vectors:
                    self.validate(vector)
                return vectors
            except Exception as exc:
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    break
                # Exponential backoff. Ingestion is a background job, so
                # waiting is cheaper than failing the document.
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                log.warning(
                    "embeddings.retry", attempt=attempt, error=str(exc)[:200]
                )

        raise EmbeddingError(f"embedding failed: {last_error}") from last_error
