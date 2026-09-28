"""Embedding-based narrowing of Setu case-study candidates to the few Claude Sonnet actually
reranks (p2_case_study_matcher.rerank_case_studies_with_llm) -- Rasa Incanta addition, not part
of the ICP bot's own P2 matcher, which only ever did keyword scoring over the whole corpus.

Two Postgres tables cache the expensive parts, both append-only and keyed by a hash of the exact
source text (`knowledge_chunks` case studies have no stable id -- CLAUDE.md), so a changed source
is a new hash and a new row, never an overwrite:
- `setu_case_study_contexts`: a Claude Haiku-written 2-3 sentence context for a case study whose
  own `content` is missing or under MIN_CONTENT_CHARS characters, generated once and reused
  forever after. A case study with real content skips this call entirely -- its own content IS
  the context; there's nothing for Haiku to add.
- `setu_case_study_embeddings`: one Voyage embedding of {name, industry, service line, context}.
  Read once per deal or company scored and reused; with under 100 case studies on file this is a
  brute-force cosine-similarity scan (embeddings.cosine_similarity), not a vector index.

Both tables are read and written through a session opened here, same pattern as
mail_client.py / meetings_store.py: this module is called from inside p2_case_study_matcher.py
(an ICP-bot-ported file with no session of its own to thread through), exactly like those two.

Never raises: on any failure (VOYAGE_API_KEY unset, a Voyage or Claude call failing, an embedding
missing) `top_by_similarity()` returns the candidates already sorted by keyword score, unchanged --
the same resilience pattern as rerank_case_studies_with_llm()'s own fallback on failure. One bad
embedding never fails a deal's case-study match, let alone the run.
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select

from api.db import get_sessionmaker
from api.icp import embeddings as voyage
from api.icp import tracing
from api.icp.compat import get_settings
from api.models import SetuCaseStudyContext, SetuCaseStudyEmbedding

logger = logging.getLogger(__name__)

MIN_CONTENT_CHARS = 50  # real content this short or shorter is treated as "no description"

_ENRICHMENT_SYSTEM = """You write short contextual summaries of Practus case studies for internal search, \
from only the name, industry, service line and whatever real text is given. In 2-3 sentences say: what \
problem was solved, what kind of company it fits, and what challenges it is relevant for. Use only what is \
given -- never invent a client name, number or outcome that isn't stated. If almost nothing is given, say \
plainly what little is known rather than padding."""


def source_hash(name: str, industry: str | None, service_line: str | None, content: str) -> str:
    """Identity for a case study's exact source text -- the corpus has no stable id, so this IS
    the id: a changed name/industry/service_line/content is a different case study as far as the
    cache is concerned, not an update to the old one."""
    raw = "|".join((name or "", industry or "", service_line or "", content or ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_context(name: str, industry: str | None, service_line: str | None, content: str, *, model: str) -> str:
    from api.icp.anthropic_client import get_client, log_usage

    user = (f"Name: {name}\nIndustry: {industry or 'unknown'}\nService line: {service_line or 'unknown'}\n"
            f"Available text: {content or '(none)'}")
    label = f"setu_case_study_context: {name}"
    with tracing.observe_generation(label, model=model, input=user, tags=["setu-enrichment"]) as finish:
        response = get_client().messages.create(model=model, max_tokens=200, system=_ENRICHMENT_SYSTEM,
                                                 messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        finish(output=text, usage=response.usage)
    log_usage(label, response.usage)
    return text


def _embedding_text(name: str, industry: str | None, service_line: str | None, context: str) -> str:
    return f"{name}. Industry: {industry or 'unknown'}. Service line: {service_line or 'unknown'}. {context}".strip()


def _ensure_embeddings(case_studies: list[dict]) -> dict[str, list[float]]:
    """{source_hash: embedding vector} for every case study, generating and caching whatever is
    missing (enriching first, when a case study's own content is too thin to embed directly)."""
    settings = get_settings()
    hashed = [(source_hash(cs["name"], cs.get("industry"), cs.get("service_line"), cs.get("content") or ""), cs)
              for cs in case_studies]
    hashes = [h for h, _ in hashed]

    with get_sessionmaker()() as session:
        # Filtered on `model` too, not just source_hash: an embedding is only reusable if it was
        # made by the SAME model as settings.embedding_model asks for now. Two different models'
        # vectors are not comparable (different embedding spaces, sometimes different dimensions
        # outright) -- switching EMBEDDING_MODEL must re-embed everything, not silently compare a
        # new query vector against an old model's stale rows under the same source_hash.
        existing = {row.source_hash: row.embedding for row in
                    session.scalars(select(SetuCaseStudyEmbedding).where(
                        SetuCaseStudyEmbedding.source_hash.in_(hashes),
                        SetuCaseStudyEmbedding.model == settings.embedding_model))}
        missing = [(h, cs) for h, cs in hashed if h not in existing]
        if not missing:
            return existing

        thin_hashes = [h for h, cs in missing if len((cs.get("content") or "").strip()) < MIN_CONTENT_CHARS]
        cached_contexts = {row.source_hash: row.generated_context for row in
                           session.scalars(select(SetuCaseStudyContext).where(SetuCaseStudyContext.source_hash.in_(thin_hashes)))}

        texts: list[str] = []
        order: list[tuple[str, str]] = []
        for h, cs in missing:
            content = (cs.get("content") or "").strip()
            if len(content) >= MIN_CONTENT_CHARS:
                context = content  # real content IS the context; nothing to generate
            elif h in cached_contexts:
                context = cached_contexts[h]
            else:
                context = _generate_context(cs["name"], cs.get("industry"), cs.get("service_line"), content,
                                            model=settings.anthropic_model_extraction)
                session.add(SetuCaseStudyContext(source_hash=h, case_study_name=cs["name"], generated_context=context,
                                                 model=settings.anthropic_model_extraction))
            texts.append(_embedding_text(cs["name"], cs.get("industry"), cs.get("service_line"), context))
            order.append((h, cs["name"]))

        vectors = voyage.embed_many(texts, input_type="document")
        for (h, name), vector in zip(order, vectors):
            session.add(SetuCaseStudyEmbedding(source_hash=h, case_study_name=name, model=settings.embedding_model,
                                               embedding=vector))
            existing[h] = vector
        session.commit()
    return existing


def top_by_similarity(candidates: list[dict], *, problem_context: str, limit: int = 5) -> list[dict]:
    """The `limit` candidates whose enriched text is closest to `problem_context` by cosine
    similarity, most similar first. `candidates` should already carry the industry/geography/
    keyword-score fields p2_case_study_matcher.find_case_study_matches() computes -- those ride
    through unchanged on whichever candidates survive; only which ones survive, and their order,
    changes. Falls back to `candidates[:limit]` (the existing keyword-score order) on any failure,
    and never raises."""
    if not candidates:
        return candidates
    try:
        embeddings = _ensure_embeddings(candidates)
        hashes = [source_hash(cs["name"], cs.get("industry"), cs.get("service_line"), cs.get("content") or "")
                  for cs in candidates]
        [query_vector] = voyage.embed_many([problem_context], input_type="query")
        scored = [(voyage.cosine_similarity(query_vector, embeddings[h]), cs)
                  for h, cs in zip(hashes, candidates) if h in embeddings]
        if not scored:
            raise voyage.EmbeddingError("No candidate had an embedding available.")
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [cs for _, cs in scored[:limit]]
    except Exception as exc:
        logger.warning("Case-study embedding retrieval failed: %s — falling back to the keyword ranking.", exc)
        return candidates[:limit]
