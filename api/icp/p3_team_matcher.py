# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/p3_team_matcher.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Deterministic P3 (icp-skill.md STEP 3, "the team we would field") matching
against the Setu (Wisible) Postgres mirror — built to replace the P3 "team"
Setu chat question, which was unreliable in production. See
setu_questions.p3_team_question()'s docstring for the full live comparison:
that compound chat question failed 0/5 real production runs on one account,
while this direct SQL/Python path needs no LLM agent to converge at all —
industries/service_lines/grade are structured JSON fields, and resume
keyword matching is a plain substring search, not a multi-step search the
answering agent has to plan and might run out of budget on.
"""

from __future__ import annotations

import logging
import re

from . import setu_db

logger = logging.getLogger(__name__)

# Long enough to skip generic short words/stray characters, short enough to
# still catch a distinctive keyword like "wine" or "winery".
_MIN_KEYWORD_LEN = 4

# Common English filler words that would otherwise pollute keyword matching
# when tokenizing an arbitrary deal name / industry / service-line string
# (e.g. "Growth", "Expansion", "with", "from" are frequent CRM noise, not
# genuine sector signal). Live-confirmed real gap: once `keyword_context`
# started including full secondary-research prose (thousands of characters,
# not just a short deal name), generic business/resume vocabulary common to
# almost ANY person's resume and ANY company's research ("finance",
# "board", "strategic", "professional"...) produced 40+ incidental
# "matches" for people with no real connection to the deal, burying a
# genuinely distinctive hit (e.g. "wine") alphabetically among them and
# making the displayed snippet pick a meaningless one instead.
_STOPWORDS = {
    "with", "from", "this", "that", "have", "will", "your", "their", "growth",
    "expansion", "penetration", "project", "walkthrough", "scheduled",
    "conducted", "potential", "limited", "private",
    "advisory", "after", "area", "available", "bank", "board", "business",
    "company", "completed", "cost", "data", "date", "debt", "director",
    "finance", "financial", "government", "having", "high", "india",
    "investment", "leadership", "legal", "level", "life", "limit", "line",
    "next", "personal", "point", "presentation", "professional", "profit",
    "qualification", "regulation", "report", "sebi", "strategic", "strong",
    "summary", "through", "time", "tool", "year", "years", "management",
    "team", "work", "working", "experience", "role", "roles", "senior",
    "junior", "market", "markets", "quarter", "annual", "capital", "group",
    "national", "international", "global", "corporate", "operations",
    "operational", "process", "processes", "system", "systems", "client",
    "clients", "service", "services", "value", "review", "reviewed",
    "including", "various", "multiple", "across", "within", "based",
    "provided", "ensure", "ensuring", "responsible", "support", "supporting",
    # Live-confirmed leaking through even after requiring multi-word
    # capitalized phrases (_extract_query_keywords) -- financial reporting
    # prose still routinely pairs a generic term with another capitalized
    "advisor", "allied", "brief", "central", "disclosed", "fee", "former",
    "free", "given", "hard", "journal", "liquidity", "managing", "margin",
    "more", "named", "note", "operating", "peer", "position", "press",
    "prioritie", "promoter", "sale", "simply", "since", "standard",
    "stated", "trend", "trigger", "type", "wall",
}

_INVISIBLE_CHARS = (chr(0x200B), chr(0x200C), chr(0x200D), chr(0xFEFF))


def normalize_label(value: str) -> str:
    """Lowercase, trim, strip invisible Unicode characters (a real Zoho
    export was confirmed to have at least one industry value with a
    trailing zero-width space), and strip a trailing 's' so singular/plural
    forms match ("Metal" vs "Metals"). Shared by industry AND service-line
    comparisons — same normalizer, same reasoning, ported from the sibling
    ep-el-exception-report project's matching.py, which already does this
    exact comparison against the same Setu roster."""
    text = str(value).strip()
    for ch in _INVISIBLE_CHARS:
        text = text.replace(ch, "")
    text = text.lower()
    if text.endswith("s") and not text.endswith("ss"):
        text = text[:-1]
    return text


# The CRM's `industry_type` free-text values don't share a taxonomy with
# Setu's `industries` labels (e.g. CRM "FMCG" vs Setu "Consumer Goods").
# Confirmed-with-user mappings, ported from the sibling ep-el-exception-
# report project (matching.py), which already resolves this exact mismatch
# against the same Setu roster this module reads. Keys are
# normalize_label(<CRM value>); values are the corresponding Setu label
# (also run through normalize_label() before comparison).
CRM_TO_SETU_INDUSTRY = {
    "auto component/ ancilliary": "Automobile",
    "auto components/ancillary": "Automobile",
    "auto component/ancillary": "Automobile",
    "consumer goods industry": "Consumer Goods",
    "banking, financial services and insurance (bfsi)": "BFSI",
    "banking, financial services and insurance": "BFSI",
    "pharmacuetical": "Pharmaceuticals",
    "retail": "Retail and e-commerce",
    "retail and e-comm": "Retail and e-commerce",
    "internet & direct marketing retail": "Retail and e-commerce",
    "health care service": "Healthcare Services",
    "healthcare": "Healthcare Services",
    "health care - equipment": "Healthcare Equipments",
    "financials service": "BFSI",
    "insurance": "BFSI",
    "media and entertainment": "Technology, Media & Telecom",
    "telecom": "Technology, Media & Telecom",
    "tech": "Technology, Media & Telecom",
    "technology - saa": "IT SAAS",
    "education and edutec": "Education & Edtech",
    "education": "Education & Edtech",
    "fmcg": "Consumer Goods",
    "trading & distributor": "B2B Distribution",
    "epc industry": "Infrastructure",
}

# Same idea, for CRM `problem_area`/service-line values vs Setu's
# `service_lines` labels. Also ported from the sibling project.
CRM_TO_SETU_SERVICE_LINE = {
    "finance transformation": "Strategic Finance Transformation",
    "analytic": "Analytics as a Service",
}


def _resolve_industry_alias(value: str) -> str:
    return CRM_TO_SETU_INDUSTRY.get(normalize_label(value), value)


def _resolve_service_line_alias(value: str) -> str:
    return CRM_TO_SETU_SERVICE_LINE.get(normalize_label(value), value)


def _tokenize(text: str) -> set[str]:
    """Singular/plural-normalized (strip a trailing 's') so a company name
    like "Sula Wines" matches a resume's "Wine-yard" mention — confirmed
    live this exact mismatch (plural company name vs. singular resume
    text) would otherwise silently miss a real hit.

    Live-confirmed bug: checking stopword membership BEFORE normalizing let
    a plural stopword slip through untouched ("regulations" isn't itself in
    _STOPWORDS) and then get stripped down to a word that IS a stopword
    ("regulation") — the check must happen on the normalized form, or the
    stopword list silently fails to catch its own plurals."""
    tokens = set()
    for w in re.findall(r"[a-z]{%d,}" % _MIN_KEYWORD_LEN, text.lower()):
        normalized = w[:-1] if w.endswith("s") and not w.endswith("ss") else w
        if normalized in _STOPWORDS:
            continue
        tokens.add(normalized)
    return tokens


# Requires TWO OR MORE consecutive capitalized words, not one -- live-
# confirmed that single-word capitalization alone isn't a clean enough
# signal: financial reporting prose routinely capitalizes ordinary terms
# as headers/labels too ("Revenue", "Cash Flow", "Auditor's Report",
# "Quarterly Performance"), which single-word extraction let straight
# through. A genuine company/peer name is reliably a repeated MULTI-WORD
# phrase ("Sula Wines", "United Spirits", "Radico Khaitan"); a capitalized
# section header for a generic financial line item almost never is.
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z&]{2,}(?:\s+[A-Z][a-zA-Z&]{2,})+\b")


def _extract_query_keywords(text: str) -> set[str]:
    """Live-confirmed real gap, in two rounds: once `keyword_context` grew
    from a short CRM-derived phrase into full secondary-research prose
    (thousands of characters), plain word-tokenization stopped being a
    usable signal at all -- every EP/EL/TL candidate here is a finance
    professional, and the query text is itself financial reporting prose,
    so ordinary finance/accounting vocabulary ("account", "compliance",
    "filing", "revenue"...) overlaps almost ANY candidate's resume
    regardless of real relevance. No stopword list can keep up with that:
    expanding it just chases the last false-positive word forever, and
    single-word capitalization alone doesn't fix it either (see
    _PROPER_NOUN_RE's docstring).

    Requiring a genuine MULTI-WORD capitalized phrase is a much cleaner
    signal: company names, sector terms, and named peers/competitors
    reliably appear as a repeated multi-word phrase ("Sula Wines", "United
    Spirits", "Radico Khaitan"), while generic reporting vocabulary in
    running prose essentially never does. Deal names from the CRM are
    typically already multi-word phrases too ("Sula Wines - Growth &
    Expansion"), so this works for the short CRM-derived parts of the
    context as well as the long prose parts -- no separate branch needed."""
    tokens: set[str] = set()
    for phrase in _PROPER_NOUN_RE.findall(text):
        tokens |= _tokenize(phrase)
    return tokens


def _label_matches(target: str, candidates: list[str], *, resolve_alias) -> bool:
    target_norm = normalize_label(resolve_alias(target))
    return any(target_norm == normalize_label(c) for c in candidates)


def _document_frequencies(resume_texts) -> dict[str, int]:
    """How many DISTINCT resumes each token appears in, across the whole
    roster — the denominator for the inverse-document-frequency weighting
    in find_team_matches(). A word every resume shares is worthless as a
    relevance signal no matter how it slipped past the stopword list; a
    word only one or two resumes contain is a real, strong signal."""
    freq: dict[str, int] = {}
    for text in resume_texts:
        for token in _tokenize(text):
            freq[token] = freq.get(token, 0) + 1
    return freq


def _snippet_around(text: str, keyword: str, radius: int = 90) -> str:
    idx = text.lower().find(keyword)
    if idx == -1:
        return ""
    start = max(0, idx - radius)
    end = min(len(text), idx + len(keyword) + radius)
    snippet = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def find_team_matches(
    *, industry: str | None, service_line: str | None, keyword_context: str, limit: int = 4
) -> list[dict]:
    """Returns up to `limit` EP/EL/TL people ranked by relevance, each as
    {name, grade, status, industries, service_lines, named_clients,
    industry_match, service_line_match, keyword_hits, resume_snippet,
    score}. Inactive people are only included if no Active person scores
    at all, so a stale record never crowds out a real current option but
    also never silently hides the only match that exists.

    `keyword_context` should be whatever's known about the deal (company/
    deal name, industry, service line) — tokenized and searched against
    each person's resume text, which is how a real prior-career hit (e.g.
    "Wine-yard" experience for a wine company) gets found even when the
    CRM's own industry field is generic or wrong, the same free-text
    mechanism a Setu chat answer would have used.
    """
    roster = setu_db.fetch_ep_el_tl_roster()
    skill_profiles = setu_db.fetch_skill_profiles()
    resumes = setu_db.fetch_resume_text()
    employee_codes = [p["employee_code"] for p in roster if p["employee_code"]]
    named_clients = setu_db.fetch_named_clients(employee_codes)
    keywords = _extract_query_keywords(keyword_context)
    # Live-confirmed: even after requiring multi-word capitalized phrases
    # AND an extensive hand-curated stopword list, a fresh secondary-
    # research run introduces its OWN new incidental words ("department",
    # "over") that tie every single candidate at the same score -- hand-
    # tuning stopwords per run is an endless chase. Weighting each hit by
    # how RARE it is across the WHOLE roster's resumes (classic inverse-
    # document-frequency) self-corrects for this without any manual
    # curation: a word only one person's resume contains ("wine") is a
    # real, strong signal regardless of what it is, while a word many
    # resumes share ("department", "over") is definitionally weak, no
    # matter how it slipped past the stopword list.
    doc_frequency = _document_frequencies(resumes.values())

    def score_roster(people: list[dict]) -> list[dict]:
        scored = []
        for person in people:
            norm_name = setu_db.normalize_person_name(person["name"])
            profile = skill_profiles.get(norm_name, {"industries": [], "service_lines": []})
            resume_text = resumes.get(norm_name, "")
            resume_tokens = _tokenize(resume_text)

            industry_hit = bool(industry) and _label_matches(
                industry, profile["industries"], resolve_alias=_resolve_industry_alias
            )
            service_line_hit = bool(service_line) and _label_matches(
                service_line, profile["service_lines"], resolve_alias=_resolve_service_line_alias
            )
            keyword_hits = sorted(keywords & resume_tokens)

            # Live-confirmed bug (round 1): counting "has ANY staffing
            # history at all" as a scoring signal put nearly everyone in
            # the roster at the same score (virtually every Active EP/EL/TL
            # has been staffed on *something*). Named clients are real,
            # ground-truth evidence worth surfacing, but only as supporting
            # detail on someone who already qualifies on a real signal --
            # never a qualifying signal by itself.
            #
            # Live-confirmed bug (round 2): a flat +1 for "any keyword hit"
            # has the exact same problem one level up -- a match on a
            # genuinely rare, distinctive word ("wine", document frequency
            # 1) scored identically to a match on a word several unrelated
            # resumes happen to share ("department", "over"), so ties were
            # still common and broke on arbitrary roster order rather than
            # relevance. Summing 1/doc_frequency per hit (inverse document
            # frequency) fixes this without any manual stopword tuning: a
            # unique word contributes a full point, a word shared by 10
            # resumes contributes a tenth of one.
            keyword_score = sum(1.0 / doc_frequency.get(k, 1) for k in keyword_hits)
            score = int(industry_hit) * 2 + int(service_line_hit) * 2 + keyword_score
            if score == 0:
                continue
            clients = named_clients.get(person["employee_code"], []) if person["employee_code"] else []
            scored.append(
                {
                    "name": person["name"],
                    "grade": person["grade_label"],
                    "status": person["status"],
                    "industries": profile["industries"],
                    "service_lines": profile["service_lines"],
                    "named_clients": clients,
                    "industry_match": industry_hit,
                    "service_line_match": service_line_hit,
                    "keyword_hits": keyword_hits,
                    # The rarest hit (lowest document frequency) is the one
                    # most likely to actually mean something -- same
                    # reasoning as the score above, applied to which hit
                    # the displayed snippet centers on.
                    "resume_snippet": (
                        _snippet_around(resume_text, min(keyword_hits, key=lambda k: doc_frequency.get(k, 1)))
                        if keyword_hits
                        else ""
                    ),
                    "score": score,
                }
            )
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored

    active_matches = score_roster([p for p in roster if p["status"] == "Active"])
    if active_matches:
        return active_matches[:limit]
    return score_roster([p for p in roster if p["status"] != "Active"])[:limit]


def find_assigned_person_profile(name: str | None) -> dict | None:
    """icp-skill.md P3 / a real Manappuram Finance run (2026-09-09): the
    deal's own already-assigned EP, Shashank Silhare, has a real, on-file
    skill_profile and resume — but find_team_matches()'s fuzzy industry/
    keyword scoring genuinely scored him 0 (his documented industries don't
    include this deal's BFSI tag, and nothing in the deal's own text hit a
    keyword in his resume) and silently excluded him from the candidate
    list, so the generated report claimed "no skill-profile or staffing
    record... was found" for him — false: a record DOES exist, it just
    isn't a strong sector match for this specific deal.

    A direct, deterministic by-name lookup — separate from and ALWAYS run
    alongside the fuzzy industry/keyword search, never gated on scoring
    above zero — is the only way to guarantee the report can state what is
    actually on file for the person already staffed on this deal, instead
    of silently treating them the same as a genuine stranger to the roster.
    Returns None only when the name genuinely isn't in the EP/EL/TL roster
    at all — a real person with zero documented industries/service lines/
    resume still returns a profile (with those fields empty), since "in
    the roster but undocumented" and "not in the roster" are different,
    both real facts the report should be able to state accurately."""
    if not name or not name.strip():
        return None

    roster = setu_db.fetch_ep_el_tl_roster()
    norm_target = setu_db.normalize_person_name(name)
    person = next((p for p in roster if setu_db.normalize_person_name(p["name"]) == norm_target), None)
    if person is None:
        return None

    skill_profiles = setu_db.fetch_skill_profiles()
    resumes = setu_db.fetch_resume_text()
    named_clients = setu_db.fetch_named_clients([person["employee_code"]] if person["employee_code"] else [])

    norm_name = setu_db.normalize_person_name(person["name"])
    profile = skill_profiles.get(norm_name, {"industries": [], "service_lines": []})
    resume_text = resumes.get(norm_name, "")
    return {
        "name": person["name"],
        "grade": person["grade_label"],
        "status": person["status"],
        "industries": profile["industries"],
        "service_lines": profile["service_lines"],
        "named_clients": named_clients.get(person["employee_code"], []) if person["employee_code"] else [],
        "has_resume_on_file": bool(resume_text.strip()),
    }


def format_matches_as_evidence_text(
    matches: list[dict], *, industry: str | None, service_line: str | None,
    assigned_profiles: list[dict] | None = None,
) -> str:
    """Renders `find_team_matches()`'s output as prose, so it plugs into the
    same UnstructuredEvidenceItem(criterion_tags=["P3"]) pathway every other
    P2/P3 Setu answer already uses — no downstream change needed in
    llm_interpreter.py or narrative/practus_section.py.

    `assigned_profiles` (see find_assigned_person_profile()) are ALWAYS
    described, even when empty/absent from `matches` — this is what stops
    the report from ever again claiming "no record found" for a deal's own
    already-assigned EP/EL who simply isn't a strong fuzzy-search match."""
    assigned_profiles = assigned_profiles or []
    matched_names = {m["name"] for m in matches}
    extra_assigned = [p for p in assigned_profiles if p["name"] not in matched_names]

    if not matches and not extra_assigned:
        return (
            "Direct Setu database lookup found no Practus EP/EL/TL person with a documented "
            f"industry match, service-line match, resume keyword hit, or staffing history relevant "
            f"to this deal (industry={industry or 'unknown'}, service line={service_line or 'unknown'}). "
            "This is a genuine zero-match against the real roster/skill_profile/resume/staffing data, "
            "not a search failure."
        )

    lines: list[str] = []
    if matches:
        lines.append(
            f"{len(matches)} Practus EP/EL/TL match(es), from a direct database lookup against the real "
            "employee roster, skill profiles, resumes, and staffing history (not the Setu chat endpoint):"
        )
        for m in matches:
            parts = [f"- {m['name']} ({m['grade']}, {m['status']})"]
            if m["industry_match"]:
                parts.append(f"documented industry match: \"{industry}\"")
            if m["service_line_match"]:
                parts.append(f"documented service-line match: \"{service_line}\"")
            if m["named_clients"]:
                parts.append(f"named clients actually staffed on: {', '.join(m['named_clients'][:5])}")
            if m["resume_snippet"]:
                parts.append(f"resume mentions relevant prior-career/background: \"{m['resume_snippet']}\"")
            if m.get("llm_rationale"):
                parts.append(f"why relevant: {m['llm_rationale']}")
            lines.append(" — ".join(parts))

    for p in extra_assigned:
        has_documentation = p["has_resume_on_file"] or p["industries"] or p["service_lines"]
        parts = [f"- {p['name']} ({p['grade']}, {p['status']}) — already assigned to this deal (Zoho ep_involved/el_involved)"]
        parts.append(f"documented industries: {', '.join(p['industries']) if p['industries'] else '(none on file)'}")
        parts.append(f"documented service lines: {', '.join(p['service_lines']) if p['service_lines'] else '(none on file)'}")
        if p["named_clients"]:
            parts.append(f"named clients actually staffed on: {', '.join(p['named_clients'][:5])}")
        parts.append(
            "no industry/service-line/keyword match to this specific deal, but this is a real, on-file "
            "record, not a missing one" if has_documentation else
            "no resume, skill profile, or staffing history is on file for this person at all"
        )
        lines.append(" — ".join(parts))

    return "\n".join(lines)


_RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Must be copied verbatim from the candidate list — never invent a name."},
                    "rationale": {"type": "string", "description": "One sentence: why this person is genuinely relevant to this specific deal."},
                },
                "required": ["name", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["selected"],
    "additionalProperties": False,
}


def rerank_matches_with_llm(
    matches: list[dict], *, keyword_context: str, industry: str | None, service_line: str | None, limit: int = 4
) -> list[dict]:
    """Live-confirmed real gap in the purely deterministic ranking: pure
    inverse-document-frequency weighting (find_team_matches' own score)
    favors RARE words over common ones as a proxy for relevance, but
    rarity isn't the same thing as relevance -- a coincidentally rare word
    from an unrelated resume detail (e.g. a case study that happens to
    mention "canteen") can outscore a genuinely relevant hit purely by
    chance. This keeps the deterministic DB retrieval (still the reliable
    part -- no web search, no multi-round agent loop, nothing that can run
    out of its own step budget) but hands its ALREADY-RETRIEVED, already-
    evidenced candidate list to one small, bounded LLM call whose only job
    is to pick and explain the best `limit` of them -- a task with a fixed,
    tiny amount of reading and no research to do, unlike the original P3
    chat question this whole module replaced (see
    setu_questions.p3_team_question()'s docstring for why that failed).

    Falls back to the deterministic ranking (matches[:limit]) on ANY
    failure -- this reranking step is a quality improvement on top of an
    already-working result, never a new point of failure that could turn a
    working P3 lookup into a broken one."""
    if not matches:
        return matches

    from .anthropic_client import generate_structured_narrative

    candidate_lines = []
    for m in matches:
        parts = [f"{m['name']} ({m['grade']}, {m['status']})"]
        if m["industry_match"]:
            parts.append(f"documented industry match: \"{industry}\"")
        if m["service_line_match"]:
            parts.append(f"documented service-line match: \"{service_line}\"")
        if m["keyword_hits"]:
            parts.append(f"resume keyword hits: {', '.join(m['keyword_hits'])}")
        if m["resume_snippet"]:
            parts.append(f"resume excerpt: \"{m['resume_snippet']}\"")
        if m["named_clients"]:
            parts.append(f"named clients actually staffed on: {', '.join(m['named_clients'][:5])}")
        candidate_lines.append(" — ".join(parts))

    prompt = f"""From the candidate list below, pick the {limit} Practus employees genuinely most relevant \
to this deal, ranked best first. Use ONLY the evidence given for each candidate — never invent a \
detail, and never select a name that isn't in the list below.

DEAL CONTEXT: {keyword_context[:2000]}

CANDIDATES:
{chr(10).join(f"{i + 1}. {line}" for i, line in enumerate(candidate_lines))}

A documented industry/service-line match is stronger evidence than a resume keyword hit alone. A \
keyword hit that's clearly coincidental (e.g. matches an unrelated case study, not this deal's real \
sector or company) should be ranked low or excluded even if no other candidate is a better fit — \
picking fewer than {limit} genuinely relevant people is better than padding with a coincidental \
match. If none of the candidates are genuinely relevant, return an empty list."""

    try:
        # Raised 2000->4000: confirmed live (2/2 real runs) -- comparing up
        # to 10 candidates' full evidence (grade, industries, service
        # lines, named clients, resume snippets) needs real reasoning
        # before the small JSON output, and adaptive thinking consumes
        # budget before any visible output -- the same "prompt/reasoning
        # load grew, budget needs raising" pattern as every other narrative
        # call this project has hit. This step already degrades gracefully
        # to the deterministic ranking on failure, but that means silently
        # losing the whole benefit of reranking, not just a smaller one.
        data = generate_structured_narrative(prompt, _RERANK_SCHEMA, max_tokens=4000, label="p3_team_rerank", include_skill_reference=False)
    except Exception as exc:
        logger.warning("P3 team LLM rerank FAILED: %s — falling back to the deterministic ranking.", exc)
        return matches[:limit]

    by_name = {m["name"]: m for m in matches}
    reranked = []
    for item in data.get("selected", [])[:limit]:
        match = by_name.get(item.get("name"))
        if match:
            reranked.append({**match, "llm_rationale": item.get("rationale", "")})
    if not reranked:
        logger.info("P3 team LLM rerank returned no usable selections — falling back to the deterministic ranking.")
        return matches[:limit]
    return reranked
