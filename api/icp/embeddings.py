"""Voyage AI text embeddings (https://docs.voyageai.com/reference/embeddings-api). Rasa Incanta
addition, not part of the ICP bot: the ICP bot never embedded anything.

Used only to narrow Setu case studies to the few Claude actually reranks
(api/icp/setu_embeddings.py). With well under 100 case studies on file, retrieval is a
brute-force cosine-similarity scan in Python, not a vector database or a Postgres extension --
`cosine_similarity()` below is all the "index" this needs.

Confirmed from Voyage's own docs (2026-09): `POST https://api.voyageai.com/v1/embeddings`,
`Authorization: Bearer <key>`, body `{"input": [...strings...], "model": ..., "input_type":
"query"|"document"}` -> `{"embeddings": [[...floats...], ...], "total_tokens": int}`.
`input_type` is Voyage's own documented asymmetric encoding: a corpus item being indexed is
"document", a search query being encoded to look something up is "query" -- the two sides of a
retrieval pair are not meant to be encoded the same way.
"""
from __future__ import annotations

import logging
import math

import httpx

from api.icp import tracing
from api.icp.compat import get_settings

logger = logging.getLogger(__name__)

_EMBED_URL = "https://api.voyageai.com/v1/embeddings"
_DEFAULT_TIMEOUT_SECONDS = 30.0

# Voyage's own list pricing (2026-09, per million tokens) -- used only to log an estimated dollar
# cost alongside every call, the same way exa_search.py logs Exa's own reported cost; Voyage's
# response carries no per-call dollar figure itself. Not a substitute for Voyage's own invoice if
# exact numbers matter.
_PRICE_PER_MILLION_TOKENS = {"voyage-4-lite": 0.02, "voyage-4": 0.06, "voyage-4-large": 0.06}


class EmbeddingError(RuntimeError):
    pass


def _headers() -> dict:
    settings = get_settings()
    if not settings.voyage_api_key:
        raise EmbeddingError("VOYAGE_API_KEY is not configured — set it in .env.")
    return {"Authorization": f"Bearer {settings.voyage_api_key}", "Content-Type": "application/json"}


def embed_many(texts: list[str], *, input_type: str, model: str | None = None,
               timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> list[list[float]]:
    """One embedding per text, in the same order as `texts`. `input_type` must be "document" for
    corpus items being indexed or "query" for text being used to search that corpus — see this
    module's own docstring; the two are not interchangeable."""
    if not texts:
        return []
    settings = get_settings()
    model = model or settings.embedding_model
    body = {"input": texts, "model": model, "input_type": input_type}
    with tracing.observe_search(f"voyage.embed ({input_type}, {len(texts)})",
                                input={"model": model, "input_type": input_type, "count": len(texts)},
                                provider="voyage") as finish:
        try:
            response = httpx.post(_EMBED_URL, headers=_headers(), json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            finish(output={"error": type(exc).__name__})
            raise EmbeddingError(f"Voyage request failed: {exc}") from exc
        if response.status_code != 200:
            finish(output={"error": f"HTTP {response.status_code}"})
            raise EmbeddingError(f"Voyage returned {response.status_code}: {response.text[:500]}")
        data = response.json()
        embeddings = data.get("embeddings")
        tokens = data.get("total_tokens") or 0
        cost = tokens * (_PRICE_PER_MILLION_TOKENS.get(model, 0.06) / 1_000_000)
        finish(output={"count": len(embeddings) if isinstance(embeddings, list) else 0}, cost=cost)
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        got = len(embeddings) if isinstance(embeddings, list) else "no"
        raise EmbeddingError(f"Voyage returned {got} embedding(s) for {len(texts)} input(s).")
    return embeddings


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)
