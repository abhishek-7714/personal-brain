"""
core/embedder.py

Text-embedding provider for the Postgres -> Qdrant indexing layer.

Defines the Embedder protocol so tests inject a mock without a network call
or an API key. The production implementation is VoyageEmbedder, wrapping the
Voyage AI SDK. Requires VOYAGE_API_KEY.

Why Voyage: Anthropic recommends Voyage AI for embeddings (Anthropic itself
does not offer an embeddings endpoint). voyage-4 (Jan 2026) is the current
general-purpose model; voyage-3/3-large are in maintenance.

Dimension handling: EMBEDDING_DIM in .env is NOT trusted blindly. The Qdrant
collections (vector/collections.py) are created with a fixed vector size that
must be decided before any embedding happens, so a human-set env var is
unavoidable there -- but core/indexer.py cross-checks every real embedding's
actual length against the live Qdrant collection's configured size at write
time and fails loudly on a mismatch, rather than trusting VOYAGE_MODEL and
EMBEDDING_DIM to silently agree. See core/indexer.py:_check_dimension.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Optional, Protocol

# voyage-4's supported output_dimension values (256/512/1024/2048); requested
# explicitly on every call rather than relying on the SDK/model's own default,
# so a future default change upstream can't silently change our vector size.
DEFAULT_VOYAGE_MODEL = "voyage-4"
DEFAULT_OUTPUT_DIMENSION = 1024


# ---------------------------------------------------------------------------
# Embedder protocol -- injected so tests can pass a mock
# ---------------------------------------------------------------------------

class Embedder(Protocol):
    provider: str
    model_name: str

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        """
        Embed a batch of texts, one vector per input, same order.

        input_type distinguishes how the text will be used: "document" when
        indexing (the default), "query" at retrieval time. Voyage embeds
        queries and documents asymmetrically for better retrieval quality --
        passing the wrong one at query time silently degrades results rather
        than erroring, so callers must not just default this everywhere.
        """
        ...


# ---------------------------------------------------------------------------
# Production implementation
# ---------------------------------------------------------------------------

class VoyageEmbedder:
    """
    Wraps the Voyage AI Python SDK (`voyageai`, PyPI). model defaults to
    VOYAGE_MODEL env var or DEFAULT_VOYAGE_MODEL. Requires VOYAGE_API_KEY
    (read by the SDK itself from the environment).
    """

    def __init__(
        self,
        model: Optional[str] = None,
        output_dimension: Optional[int] = None,
    ) -> None:
        self.provider = "voyage"
        self.model_name = model or os.environ.get("VOYAGE_MODEL", DEFAULT_VOYAGE_MODEL)
        self.output_dimension = output_dimension or int(
            os.environ.get("EMBEDDING_DIM", str(DEFAULT_OUTPUT_DIMENSION))
        )

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        import voyageai  # imported here so the module loads without the package
        client = voyageai.Client()  # reads VOYAGE_API_KEY from env
        result = client.embed(
            texts=texts,
            model=self.model_name,
            input_type=input_type,
            output_dimension=self.output_dimension,
            output_dtype="float",
        )
        return result.embeddings


def from_env() -> VoyageEmbedder:
    """Construct a VoyageEmbedder from environment variables."""
    return VoyageEmbedder()


# ---------------------------------------------------------------------------
# Mock implementation for tests
# ---------------------------------------------------------------------------

class MockEmbedder:
    """
    Deterministic feature-hashed bag-of-words embedding for tests -- no
    network call, no API key required. Each token in the text is hashed into
    one of `dim` buckets with a signed hash (the standard "hashing trick"),
    then the vector is L2-normalized so cosine similarity behaves the way it
    would for a real embedding.

    This is NOT a neural embedding and captures only lexical overlap, not
    real semantics -- two sentences that share vocabulary score similar even
    if unrelated, and paraphrases with no shared words score dissimilar even
    if related. It exists so the idempotency/dimension/payload tests and the
    end-to-end retrieval smoke test (core/indexer.py:semantic_search) can run
    in this environment without VOYAGE_API_KEY, while still exercising real
    nearest-neighbor behavior in Qdrant rather than random noise. The smoke
    test fixture is worded so lexical overlap is enough to prove the
    projection is wired correctly; it does not claim anything about real
    embedding quality, which requires VoyageEmbedder against a live key.
    """

    def __init__(self, dim: Optional[int] = None) -> None:
        self.provider = "mock"
        self.model_name = "mock-hashed-bow"
        self.dim = dim or int(os.environ.get("EMBEDDING_DIM", str(DEFAULT_OUTPUT_DIMENSION)))

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = re.findall(r"[a-z0-9]+", text.lower()) or [""]
        for tok in tokens:
            h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0 if (h // self.dim) % 2 == 0 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
