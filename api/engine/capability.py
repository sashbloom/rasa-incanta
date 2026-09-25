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


def describe(match: CaseMatch) -> str:
    """The fact text: which case, what it covered, why it matched."""
    cs = match.case
    what = ", ".join(x for x in (cs.industry, cs.service_line) if x)
    why = []
    if match.industry_match:
        why.append("same industry")
    if match.keyword_hits:
        why.append("shared terms: " + ", ".join(match.keyword_hits))
    if match.geography_match:
        why.append("same geography")
    return f"{cs.name}" + (f" ({what})" if what else "") + f". Matched on {'; '.join(why)}."
