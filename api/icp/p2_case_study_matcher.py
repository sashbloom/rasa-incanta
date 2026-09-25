# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/p2_case_study_matcher.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Deterministic P2 (icp-skill.md STEP 3, "problem-solution proof") case-
study matching against the Setu (Wisible) Postgres mirror — same
architecture as p3_team_matcher.py (deterministic DB retrieval + inverse-
document-frequency scoring + a small, bounded LLM rerank on top), applied
to case studies instead of people.

Built after a live-confirmed real gap: the Setu CHAT question for P2
(setu_questions.p2_case_study_question()) reliably FOUND real, relevant
case studies (unlike the P3 team question, this one doesn't fail), but its
own free-text synthesis decides their ORDER with no explicit priority rule
— a real run for a wine company put its closest real analogue (an F&B
case) LAST, after several less-relevant manufacturing cases, with no
named FMCG cases surfaced at all despite FMCG-tagged case studies existing
in the same database. icp-skill.md's own P2 scoring table (line 357-363)
is explicit that industry closeness is the PRIMARY ranking signal ("same
problem AND same/adjacent industry" outranks "same problem, different
industry"), which a free-text chat answer has no structural way to
guarantee — this gives it one.
"""

from __future__ import annotations

import logging

from . import p3_team_matcher, setu_db

logger = logging.getLogger(__name__)

# case_study's own industry vocabulary is a SEPARATE, largely CRM-shaped
# taxonomy from skill_profile's (used by p3_team_matcher for employee
# matching) -- confirmed live it already has "FMCG"/"FMCG / Retail" as
# literal values, unlike skill_profile which has no "FMCG" value at all.
# Reusing p3_team_matcher.CRM_TO_SETU_INDUSTRY here was a real, confirmed
# bug: it translated "FMCG" -> "Consumer Goods" *before* comparing against
# a case study's own native "FMCG" tag, so the two never matched even
# though they're the same real value. This table starts empty/minimal on
# purpose -- most CRM industry values should now match a case study's own
# label directly with no alias needed at all; only add an entry here once
# a real, confirmed mismatch is found (mirroring how CRM_TO_SETU_INDUSTRY
# itself grew).
CASE_STUDY_INDUSTRY_ALIASES: dict[str, str] = {
    "auto component/ ancilliary": "Automotive",
    "auto components/ancillary": "Automotive",
    "auto component/ancillary": "Automotive",
}


def _resolve_case_study_industry_alias(value: str) -> str:
    return CASE_STUDY_INDUSTRY_ALIASES.get(p3_team_matcher.normalize_label(value), value)


# case_study.metadata has no structured geography field -- real geography
# mentions live loosely in `content` prose ("USA HQ, 6 countries",
# "...Pharma Company (India)"). ownership_classifier.OwnershipClassification
# .geography is already computed earlier in assemble() as one of
# "india"/"us"/"mea"/"other" -- this maps that onto real country-name
# keywords to search case-study content against, same spirit as
# CRM_TO_SETU_INDUSTRY. "other"/unknown geography deliberately has no
# keywords, since it names no real signal to search for.
GEOGRAPHY_KEYWORDS: dict[str, list[str]] = {
    "india": ["india", "indian"],
    "us": ["usa", "u.s.", "united states", "america", "american"],
    "mea": [
        "middle east", "uae", "u.a.e.", "dubai", "saudi", "qatar", "kuwait",
        "bahrain", "oman", "africa", "african", "nigeria", "kenya", "egypt",
    ],
}


def find_case_study_matches(
    *, problem_context: str, industry: str | None, geography: str | None = None, limit: int = 100
) -> list[dict]:
    """Returns up to `limit` case studies ranked by keyword score
    (descending), each as {name, industry, service_line, content,
    industry_match, keyword_hits, score}. Reuses p3_team_matcher's proven
    text-matching primitives (tokenization, stopwords, inverse-document-
    frequency weighting, alias resolution).

    Live-confirmed real gap: unlike P3's resume matching (where a literal
    keyword like "wine" usually does appear), the MOST relevant case study
    for a given problem can be a genuinely conceptual analogy with ZERO
    keyword overlap — "Patisserie & Bakes" (rapid multi-city expansion,
    weak financial governance) is the closest real match for a wine
    company's growth/margin problem despite sharing no literal words with
    it at all. A hard keyword-score cutoff would exclude it from ever
    reaching the LLM rerank step, which is the only place that kind of
    conceptual judgment can actually happen. Since the whole case-study
    corpus is small (~83 total, confirmed live) and bounded — unlike an
    open-ended web/tool search — this deliberately does NOT exclude zero-
    score case studies. `limit` defaults to 100, comfortably above the
    real total (~83-90 confirmed live) so in practice this sends the WHOLE
    corpus to the LLM rerank step, score-ranked so real keyword matches
    surface first in what it reads — live-confirmed a smaller cap (25) was
    still an arbitrary cut among tied zero-score entries and excluded the
    exact conceptual match this fix exists for. A single bounded LLM call
    over ~90 short case-study summaries is a trivial cost/context size,
    nothing like an open-ended search."""
    case_studies = setu_db.fetch_case_studies()
    keywords = p3_team_matcher._extract_query_keywords(problem_context)
    # Live-confirmed real gap: the "Patisserie & Bakes" case study's own
    # `content` field never mentions "Patisserie"/"Bakes"/"bakery" anywhere
    # -- that distinctive sector signal lives ONLY in `entity_name` (the
    # case title). Searching content alone made this case invisible to any
    # keyword search, even one built directly from a company named after
    # its own product category. Both fields together are the real
    # searchable text. Built as a parallel list (not a dict keyed by
    # entity_name) since duplicate case-study titles exist in the real data
    # (confirmed live: "F-Mart Mobiles" appears twice with different
    # content) and a dict would silently collapse them.
    searchable_texts = [f"{cs['entity_name']} {cs['content'] or ''}" for cs in case_studies]
    doc_frequency = p3_team_matcher._document_frequencies(searchable_texts)

    geography_terms = GEOGRAPHY_KEYWORDS.get((geography or "").lower(), [])

    scored = []
    for cs, searchable_text in zip(case_studies, searchable_texts):
        content = cs["content"] or ""
        cs_tokens = p3_team_matcher._tokenize(searchable_text)
        keyword_hits = sorted(keywords & cs_tokens)
        industry_hit = bool(industry) and bool(cs["industry"]) and p3_team_matcher._label_matches(
            industry, [cs["industry"]], resolve_alias=_resolve_case_study_industry_alias
        )
        # Smaller weight than industry_match on purpose -- icp-skill.md's
        # own P2 priority puts "same problem AND same/adjacent industry"
        # ahead of everything else; geography is a secondary tiebreaker on
        # top of that, not a replacement for it.
        geography_hit = bool(geography_terms) and any(
            term in searchable_text.lower() for term in geography_terms
        )
        keyword_score = sum(1.0 / doc_frequency.get(k, 1) for k in keyword_hits)
        score = int(industry_hit) * 2 + int(geography_hit) * 1 + keyword_score
        scored.append(
            {
                "name": cs["entity_name"],
                "industry": cs["industry"],
                "service_line": cs["service_line"],
                "content": content,
                "industry_match": industry_hit,
                "geography_match": geography_hit,
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
                    "name": {"type": "string", "description": "Must be copied verbatim from the candidate list — never invent a case study name."},
                    "rationale": {"type": "string", "description": "One sentence: why this case is genuinely relevant, and how close a match it is (same industry / adjacent / generic)."},
                },
                "required": ["name", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["selected"],
    "additionalProperties": False,
}


def rerank_case_studies_with_llm(matches: list[dict], *, problem_context: str, limit: int = 3) -> list[dict]:
    """Keeps the deterministic DB retrieval (the reliable part) but hands
    its already-retrieved, already-evidenced candidates to one small,
    bounded LLM call whose only job is to pick and ORDER the best `limit`
    of them, closest match first — enforcing icp-skill.md's own P2
    priority (same problem+same/adjacent industry > same problem,
    different industry > adjacent problem, same industry > generic
    capability only), which pure keyword scoring alone doesn't capture
    (industry closeness matters more than raw keyword overlap, and this
    task requires judging that, not just counting hits).

    Falls back to the deterministic ranking (matches[:limit]) on ANY
    failure or hallucinated name, same resilience pattern as
    p3_team_matcher.rerank_matches_with_llm()."""
    if not matches:
        return matches

    from .anthropic_client import generate_structured_narrative

    candidate_lines = []
    for m in matches:
        parts = [f"{m['name']} (Industry: {m['industry'] or 'unknown'}, Service line: {m['service_line'] or 'unknown'})"]
        if m["industry_match"]:
            parts.append("documented industry match for this deal")
        if m.get("geography_match"):
            parts.append("documented geography match for this deal")
        parts.append(f"content: \"{m['content'][:500]}\"")
        candidate_lines.append(" — ".join(parts))

    prompt = f"""From the case studies below, pick and ORDER the {limit} most relevant to this client's \
problem, closest match FIRST. Follow icp-skill.md's own priority exactly: (1) same problem AND same/\
adjacent industry ranks highest, (2) same problem but a different industry ranks next, (3) an adjacent \
problem but the same industry ranks next, (4) generic capability with no specific case ranks last. Within \
the same priority tier, a documented geography match is a secondary tiebreaker — same problem AND same/\
adjacent industry AND same geography should rank above the same problem+industry match with no geography \
overlap — but never let geography alone outrank a genuinely closer industry/problem match. Use \
ONLY the evidence given — never invent a detail or an impact number where none is stated, and never \
select a name that isn't in the list below. Picking fewer than {limit} is better than padding with a \
weak or irrelevant match.

CLIENT'S PROBLEM / CONTEXT: {problem_context[:1500]}

CASE STUDIES:
{chr(10).join(f"{i + 1}. {line}" for i, line in enumerate(candidate_lines))}"""

    try:
        # Raised 2000->4000 proactively: this call now reasons over
        # effectively the WHOLE case-study corpus (~90, not a small
        # pre-filtered set — see find_case_study_matches()'s docstring),
        # a bigger reasoning load than P3's team rerank, which already
        # confirmed live (2/2 real runs) that 2000 wasn't enough for a
        # 10-candidate comparison. Same "prompt/reasoning load grew, budget
        # needs raising" pattern as every other narrative call this project
        # has hit -- this step already degrades gracefully to the
        # deterministic ranking on failure, but that silently loses the
        # whole benefit of reranking, not just a smaller one.
        data = generate_structured_narrative(prompt, _RERANK_SCHEMA, max_tokens=4000, label="p2_case_study_rerank", include_skill_reference=False)
    except Exception as exc:
        logger.warning("P2 case-study LLM rerank FAILED: %s — falling back to the deterministic ranking.", exc)
        return matches[:limit]

    by_name = {m["name"]: m for m in matches}
    reranked = []
    for item in data.get("selected", [])[:limit]:
        match = by_name.get(item.get("name"))
        if match:
            reranked.append({**match, "llm_rationale": item.get("rationale", "")})
    if not reranked:
        logger.info("P2 case-study LLM rerank returned no usable selections — falling back to the deterministic ranking.")
        return matches[:limit]
    return reranked


def format_case_studies_as_evidence_text(matches: list[dict]) -> str:
    """Renders `find_case_study_matches()`/`rerank_case_studies_with_llm()`'s
    output as prose, so it plugs into the same
    UnstructuredEvidenceItem(criterion_tags=["P2"]) pathway the chat
    question already used — no downstream change needed in
    llm_interpreter.py or narrative/practus_section.py."""
    if not matches:
        return (
            "Direct Setu database lookup found no case study with a documented industry match or "
            "keyword overlap relevant to this deal's stated problem. This is a genuine zero-match "
            "against the real case-study data, not a search failure."
        )
    lines = [
        f"{len(matches)} case study match(es), from a direct database lookup against the real case-study "
        "data (not the Setu chat endpoint), ordered by relevance — closest match first:"
    ]
    for m in matches:
        parts = [f"- {m['name']}"]
        if m["industry"]:
            parts.append(f"industry: {m['industry']}")
        if m["industry_match"]:
            parts.append("documented industry match for this deal")
        if m.get("geography_match"):
            parts.append("documented geography match for this deal")
        parts.append(f"details: \"{m['content'][:400]}\"")
        if m.get("llm_rationale"):
            parts.append(f"why relevant: {m['llm_rationale']}")
        lines.append(" — ".join(parts))
    return "\n".join(lines)
