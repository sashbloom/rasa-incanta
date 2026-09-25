"""Text-matching primitives for pairing deals with Setu case studies (and, later, SMEs).

Copied from the ICP bot, which proved them against the same Setu data:
`icp-bot/backend/domains/mahak/people/mahak/icp/p3_team_matcher.py` (normalize_label, stopwords,
tokenizer, keyword extraction, document frequencies, label matching) and
`p2_case_study_matcher.py` (case-study industry aliases, geography keywords). The comments on
the originals explain each choice; the short versions are kept here.
"""
from __future__ import annotations

import re

MIN_KEYWORD_LEN = 4

# Words so common in Practus prose that matching on them means nothing (the ICP bot's list).
STOPWORDS = {
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
    "advisor", "allied", "brief", "central", "disclosed", "fee", "former",
    "free", "given", "hard", "journal", "liquidity", "managing", "margin",
    "more", "named", "note", "operating", "peer", "position", "press",
    "prioritie", "promoter", "sale", "simply", "since", "standard",
    "stated", "trend", "trigger", "type", "wall",
}

_INVISIBLE_CHARS = (chr(0x200B), chr(0x200C), chr(0x200D), chr(0xFEFF))

# Zoho industry values that differ from a case study's own label (the ICP bot's confirmed list;
# add an entry only once a real mismatch is found).
CASE_STUDY_INDUSTRY_ALIASES: dict[str, str] = {
    "auto component/ ancilliary": "Automotive",
    "auto components/ancillary": "Automotive",
    "auto component/ancillary": "Automotive",
}

# Setu labels that carry no industry at all.
NO_INDUSTRY = {"nan", "none", "multiple", "other", "others", ""}

# Country words to look for in case-study prose, by geography.
GEOGRAPHY_KEYWORDS: dict[str, list[str]] = {
    "india": ["india", "indian"],
    "us": ["usa", "u.s.", "united states", "america", "american"],
    "mea": [
        "middle east", "uae", "u.a.e.", "dubai", "saudi", "qatar", "kuwait",
        "bahrain", "oman", "africa", "african", "nigeria", "kenya", "egypt",
    ],
}

# Zoho SBU -> geography above. Europe has no keywords in the ICP bot, so it adds no signal.
SBU_GEOGRAPHY = {"india": "india", "usa": "us", "mea": "mea"}

_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z&]{2,}(?:\s+[A-Z][a-zA-Z&]{2,})+\b")


def normalize_label(value: str) -> str:
    """Lowercase, trim, drop invisible characters and a trailing plural 's' ("Metals" = "Metal")."""
    text = str(value).strip()
    for ch in _INVISIBLE_CHARS:
        text = text.replace(ch, "")
    text = text.lower()
    if text.endswith("s") and not text.endswith("ss"):
        text = text[:-1]
    return text


def resolve_case_study_industry(value: str) -> str:
    return CASE_STUDY_INDUSTRY_ALIASES.get(normalize_label(value), value)


def label_matches(target: str, candidates: list[str], *, resolve_alias=lambda v: v) -> bool:
    target_norm = normalize_label(resolve_alias(target))
    return any(target_norm == normalize_label(c) for c in candidates)


def tokenize(text: str) -> set[str]:
    """Words of 4+ letters, plural 's' stripped, then stopwords removed (checked after
    normalising, so plural stopwords are caught too)."""
    tokens = set()
    for w in re.findall(r"[a-z]{%d,}" % MIN_KEYWORD_LEN, text.lower()):
        normalized = w[:-1] if w.endswith("s") and not w.endswith("ss") else w
        if normalized not in STOPWORDS:
            tokens.add(normalized)
    return tokens


def extract_query_keywords(text: str) -> set[str]:
    """Keywords from multi-word capitalised phrases only (company names, sector terms, deal
    names such as "Sula Wines"). Plain running prose overlaps every document and is ignored."""
    tokens: set[str] = set()
    for phrase in _PROPER_NOUN_RE.findall(text):
        tokens |= tokenize(phrase)
    return tokens


def document_frequencies(texts) -> dict[str, int]:
    """How many distinct documents each token appears in: rare words are strong signals."""
    freq: dict[str, int] = {}
    for text in texts:
        for token in tokenize(text):
            freq[token] = freq.get(token, 0) + 1
    return freq


def has_industry(label: str | None) -> bool:
    return bool(label) and normalize_label(label) not in NO_INDUSTRY
