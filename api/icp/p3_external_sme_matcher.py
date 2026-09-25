# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/p3_external_sme_matcher.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Deterministic P3 external-SME matching against the Setu (Wisible)
Postgres mirror's `partner_profile` chunks — same architecture as
p3_team_matcher.py / p2_case_study_matcher.py (deterministic DB retrieval +
inverse-document-frequency scoring + a small, bounded LLM rerank on top),
applied to external partner profiles instead of internal people or case
studies.

Confirmed live: unlike `skill_profile` (P3 team) or `case_study` (P2),
`partner_profile` chunks carry NO structured industry/service_line tag at
all — `metadata` is just `{"chunk": N}`. Scoring here is therefore purely
IDF-weighted keyword overlap between the deal's problem/capability-gap
context and each partner's merged profile content; there is no
industry_match dimension to add, unlike P2/P3 team.

icp-skill.md's own known data-quality flag for this source (mis-filed
internal Practus engagement letters/contracts sitting alongside genuine
external-partner profiles) is a judgment call keyword scoring can't make —
that's exactly what the LLM rerank step exists to do here, more so than in
P2/P3 team where the rerank step is mostly about ORDERING already-plausible
candidates.
"""

from __future__ import annotations

import logging
import re

from . import p3_team_matcher, setu_db

logger = logging.getLogger(__name__)

# Real bug (Livpure Smart Homes, 2026-09-09): a `partner_profile` chunk's
# own `entity_name` was a raw working-file name ("Vivek, Vamesh,
# Bimal.pptx") -- not a person, firm, or specialist name at all -- and it
# reached client-facing prose in the External Specialists section verbatim.
# The LLM rerank step's own "exclude mis-filed internal documents"
# instruction judges CONTENT, not the candidate's own name, and evidently
# didn't catch this. A literal file-extension suffix is a cheap, zero-
# false-positive structural signal no genuine partner/firm name would ever
# have -- filtering it out deterministically, before the LLM even sees it,
# is more reliable than leaving this entirely to the LLM's judgment call.
_FILENAME_SUFFIX_RE = re.compile(r"\.(pptx?|docx?|xlsx?|pdf|csv|zip)$", re.IGNORECASE)


def _looks_like_a_raw_filename(name: str) -> bool:
    return bool(_FILENAME_SUFFIX_RE.search(name.strip()))


def find_external_sme_matches(*, problem_context: str, limit: int = 100) -> list[dict]:
    """Returns up to `limit` partner profiles ranked by keyword score
    (descending), each as {name, content, keyword_hits, score}. Reuses
    p3_team_matcher's proven text-matching primitives (tokenization,
    stopwords, inverse-document-frequency weighting).

    `limit` defaults to 100, comfortably above the real total (85 distinct
    entities, confirmed live) so in practice this sends the whole corpus to
    the LLM rerank step — the same "small bounded corpus, let the LLM see
    all of it" reasoning as p2_case_study_matcher.find_case_study_matches().
    Zero-score profiles are NOT excluded for the same reason: with no
    structured tag to fall back on, a genuinely relevant partner whose
    profile happens to share no literal keyword with the problem statement
    would otherwise never reach the step that could actually judge it."""
    profiles = setu_db.fetch_partner_profiles()
    keywords = p3_team_matcher._extract_query_keywords(problem_context)
    doc_frequency = p3_team_matcher._document_frequencies(p["content"] for p in profiles)

    scored = []
    for profile in profiles:
        if _looks_like_a_raw_filename(profile["entity_name"]):
            continue
        content = profile["content"] or ""
        tokens = p3_team_matcher._tokenize(f"{profile['entity_name']} {content}")
        keyword_hits = sorted(keywords & tokens)
        score = sum(1.0 / doc_frequency.get(k, 1) for k in keyword_hits)
        scored.append(
            {
                "name": profile["entity_name"],
                "content": content,
                "keyword_hits": keyword_hits,
                "score": score,
            }
        )
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]


_RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Must be copied verbatim from the candidate list — never invent a partner name."},
                    "rationale": {"type": "string", "description": "One sentence: why this partner is genuinely relevant to this deal."},
                },
                "required": ["name", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["selected"],
    "additionalProperties": False,
}


def rerank_external_smes_with_llm(matches: list[dict], *, problem_context: str, limit: int = 3) -> list[dict]:
    """Keeps the deterministic DB retrieval (the reliable part) but hands
    its already-retrieved candidates to one small, bounded LLM call whose
    job is to pick and order the best `limit` of them AND exclude anything
    that reads like a mis-filed internal Practus engagement letter or
    client contract rather than a genuine external partner's own profile —
    a real, known data-quality issue in this source that keyword scoring
    has no way to detect.

    Falls back to the deterministic ranking (matches[:limit]) on ANY
    failure or hallucinated name, same resilience pattern as
    p2_case_study_matcher.rerank_case_studies_with_llm() /
    p3_team_matcher.rerank_matches_with_llm()."""
    if not matches:
        return matches

    from .anthropic_client import generate_structured_narrative

    candidate_lines = [f"{m['name']} — content: \"{m['content'][:500]}\"" for m in matches]

    prompt = f"""From the partner profiles below, pick and ORDER the {limit} most relevant external \
specialists for this deal, closest match FIRST. These are meant to be genuine EXTERNAL partner/\
specialist profiles — this source is known to sometimes contain mis-filed internal Practus engagement \
letters or client contracts instead of a real external partner's own profile; EXCLUDE any candidate \
that reads like that rather than a genuine external partner. Use ONLY the evidence given — never \
invent a detail, and never select a name that isn't in the list below. Picking fewer than {limit} \
(including zero) is better than padding with an irrelevant or mis-filed entry.

DEAL / CAPABILITY-GAP CONTEXT: {problem_context[:1500]}

PARTNER PROFILES:
{chr(10).join(f"{i + 1}. {line}" for i, line in enumerate(candidate_lines))}"""

    try:
        data = generate_structured_narrative(prompt, _RERANK_SCHEMA, max_tokens=4000, label="p3_external_sme_rerank", include_skill_reference=False)
    except Exception as exc:
        logger.warning("P3 external-SME LLM rerank FAILED: %s — falling back to the deterministic ranking.", exc)
        return matches[:limit]

    by_name = {m["name"]: m for m in matches}
    reranked = []
    for item in data.get("selected", [])[:limit]:
        match = by_name.get(item.get("name"))
        if match:
            reranked.append({**match, "llm_rationale": item.get("rationale", "")})
    if not reranked:
        logger.info("P3 external-SME LLM rerank returned no usable selections — falling back to the deterministic ranking.")
        return matches[:limit]
    return reranked


def format_external_smes_as_evidence_text(matches: list[dict]) -> str:
    """Renders find_external_sme_matches()/rerank_external_smes_with_llm()'s
    output as prose, so it plugs into the same
    UnstructuredEvidenceItem(criterion_tags=["P3"]) pathway the chat
    question already used — no downstream change needed in
    llm_interpreter.py or narrative/practus_section.py."""
    if not matches:
        return (
            "Direct Setu database lookup found no external partner profile with a keyword overlap "
            "relevant to this deal's stated problem/capability gap. This is a genuine zero-match "
            "against the real partner-profile data, not a search failure."
        )
    lines = [
        f"{len(matches)} external partner match(es), from a direct database lookup against the real "
        "partner-profile data (not the Setu chat endpoint), ordered by relevance — closest match first:"
    ]
    for m in matches:
        parts = [f"- {m['name']}", f"details: \"{m['content'][:400]}\""]
        if m.get("llm_rationale"):
            parts.append(f"why relevant: {m['llm_rationale']}")
        lines.append(" — ".join(parts))
    return "\n".join(lines)
