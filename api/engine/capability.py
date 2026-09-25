"""The `capability` signal: the Setu case studies closest to a deal, as evidence an NBA can cite.

Scoring is the ICP bot's P2 case-study matcher (`p2_case_study_matcher.find_case_study_matches`):
2 for the same industry, 1 for the deal's geography named in the case, plus inverse-document-
frequency weighted keyword hits over title and content. Keywords come from multi-word capitalised
phrases, so the deal name and company lead the query text.

The ICP bot sends every case study to a Claude re-rank, because the best proof can be a
conceptual analogy with no shared words. That costs one Claude call per deal per run, so it is
not done here yet. Instead a case study counts only if it shares the deal's industry, or shares
terms rare enough to mean something: keyword score of at least MIN_KEYWORD_SCORE, i.e. a term
found in at most two case studies. On the real corpus "transformation" appears in 59 of 90 case
studies, so a match on it alone is noise, not proof. Geography alone never counts. No qualifying
case study means the `no_setu_match` gap.
"""
from __future__ import annotations

from dataclasses import dataclass

from api.domain.matching import (
    GEOGRAPHY_KEYWORDS,
    SBU_GEOGRAPHY,
    document_frequencies,
    extract_query_keywords,
    has_industry,
    label_matches,
    resolve_case_study_industry,
    tokenize,
)
from api.sources.setu import CaseStudy
from api.sources.zoho import ZohoDeal

MAX_CASE_STUDIES = 3
MIN_KEYWORD_SCORE = 0.5  # for matches without the same industry: rare shared terms only


@dataclass(frozen=True)
class CaseMatch:
    case: CaseStudy
    industry_match: bool
    geography_match: bool
    keyword_hits: tuple[str, ...]
    score: float


def query_text(deal: ZohoDeal) -> str:
    """What the deal is about, name and company first (the only capitalised phrases in most
    problem statements), then the problem, problem areas and services."""
    parts = [deal.name, deal.account_name, *deal.problem_statements, *deal.problem_areas, deal.services]
    # ". " between fields, so a capitalised phrase can never span two of them ("Acme" + "Receivable
    # days..." must not become the phrase "Acme Receivable").
    return ". ".join(p for p in parts if p)


def match_case_studies(deal: ZohoDeal, corpus: list[CaseStudy], limit: int = MAX_CASE_STUDIES) -> list[CaseMatch]:
    if not corpus:
        return []
    keywords = extract_query_keywords(query_text(deal))
    texts = [f"{cs.name} {cs.content}" for cs in corpus]
    doc_frequency = document_frequencies(texts)
    geography_terms = GEOGRAPHY_KEYWORDS.get(SBU_GEOGRAPHY.get((deal.sbu or "").strip().lower(), ""), [])
    industry = deal.industry if has_industry(deal.industry) else None

    matches = []
    for cs, text in zip(corpus, texts):
        hits = tuple(sorted(keywords & tokenize(text)))
        industry_hit = bool(industry) and has_industry(cs.industry) and label_matches(
            industry, [cs.industry], resolve_alias=resolve_case_study_industry)
        keyword_score = sum(1.0 / doc_frequency.get(k, 1) for k in hits)
        if not (industry_hit or keyword_score >= MIN_KEYWORD_SCORE):
            continue  # common words and geography alone are not evidence of relevant proof
        geography_hit = bool(geography_terms) and any(term in text.lower() for term in geography_terms)
        score = 2 * industry_hit + geography_hit + keyword_score
        matches.append(CaseMatch(cs, industry_hit, geography_hit, hits, round(score, 3)))
    # On equal scores the same-industry case wins: the ICP skill ranks industry closeness first.
    matches.sort(key=lambda m: (-m.score, not m.industry_match, m.case.name))
    return matches[:limit]


def match_reason(match: CaseMatch) -> str:
    why = []
    if match.industry_match:
        why.append("same industry")
    if match.keyword_hits:
        why.append("shared terms: " + ", ".join(match.keyword_hits))
    if match.geography_match:
        why.append("same geography")
    return f"Matched on {'; '.join(why)}."


def describe(match: CaseMatch) -> str:
    """The fact text: which case, what it covered, why it matched."""
    cs = match.case
    what = ", ".join(x for x in (cs.industry, cs.service_line) if x)
    return f"{cs.name}" + (f" ({what})" if what else "") + f". {match_reason(match)}"


# ---------------------------------------------------------------- with the ICP bot's re-rank

@dataclass
class Capability:
    """What a deal can cite as proof, and who to bring in."""
    cases: list[dict]  # {name, industry, service_line, content, why, industry_match}
    smes: list[dict]  # {name, grade, why}
    reranked: bool  # False when the Claude re-rank failed and the strict deterministic match was used


def _geography(deal: ZohoDeal) -> str | None:
    return SBU_GEOGRAPHY.get((deal.sbu or "").strip().lower())


def _deterministic_cases(deal: ZohoDeal, corpus: list[CaseStudy]) -> list[dict]:
    return [{"name": m.case.name, "industry": m.case.industry, "service_line": m.case.service_line,
             "content": m.case.content, "why": match_reason(m),
             "industry_match": m.industry_match} for m in match_case_studies(deal, corpus)]


def _smes(deal: ZohoDeal, find_team) -> list[dict]:
    """Active Practus people with a real industry, service-line or CV match (the ICP bot's P3
    team matcher, deterministic, no extra Claude call)."""
    service_line = (deal.problem_areas or [None])[0] or deal.services
    try:
        people = find_team(industry=deal.industry, service_line=service_line, keyword_context=query_text(deal), limit=10)
    except Exception:
        return []
    out = []
    for p in people:
        if p.get("status") != "Active" or not (p.get("industry_match") or p.get("service_line_match") or p.get("keyword_hits")):
            continue
        why = []
        if p.get("industry_match"):
            why.append("industry experience")
        if p.get("service_line_match"):
            why.append("service line")
        if p.get("keyword_hits"):
            why.append("CV mentions " + ", ".join(p["keyword_hits"][:3]))
        if p.get("named_clients"):
            why.append("clients include " + ", ".join(p["named_clients"][:3]))
        out.append({"name": p["name"], "grade": p.get("grade"), "why": "; ".join(why)})
    return out[:MAX_SMES]


MAX_SMES = 2


def capability_for(deal: ZohoDeal, corpus: list[CaseStudy], *, find_cases=None, rerank=None, find_team=None) -> Capability:
    """The ICP bot's P2 case-study match with its one Claude re-rank, falling back to our strict
    deterministic match if the re-rank fails; plus up to two active SMEs from its P3 matcher."""
    from api.icp import p2_case_study_matcher as p2
    from api.icp import p3_team_matcher as p3

    find_cases = find_cases or p2.find_case_study_matches
    rerank = rerank or p2.rerank_case_studies_with_llm
    find_team = find_team or p3.find_team_matches

    context = query_text(deal)
    cases, reranked = [], False
    try:
        candidates = find_cases(problem_context=context, industry=deal.industry, geography=_geography(deal))
        picked = rerank(candidates, problem_context=context, limit=MAX_CASE_STUDIES) if candidates else []
        if picked and all("llm_rationale" in c for c in picked):
            reranked = True
            cases = [{"name": c["name"], "industry": c.get("industry"), "service_line": c.get("service_line"),
                      "content": c.get("content") or "", "why": c["llm_rationale"],
                      "industry_match": bool(c.get("industry_match"))} for c in picked]
    except Exception:
        cases = []
    if not reranked:  # never let the re-rank's fallback (top keyword scores, possibly zero) stand as proof
        cases = _deterministic_cases(deal, corpus)
    return Capability(cases=cases, smes=_smes(deal, find_team), reranked=reranked)
