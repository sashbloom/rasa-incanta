# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/entity_resolver.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Step 0 — identity resolution (icp-skill.md §0.1–§0.2): "a single
word-search is not sufficient and has produced a false negative in live
use." Runs all four of the skill's lookup steps before concluding anything:

1. Every account record, including misspellings (fuzzy match, client-side —
   see below for why).
2. Every deal linked to those accounts.
3. Deals fuzzy-matched by name, independent of account (catches a Potential
   whose account link is missing or wrong).
4. Identity confirmation via `reachout_tracker.email` domain.

**Fuzzy search finds candidates; it does not decide identity.** Steps 1–3
cast a deliberately wide net (that's the whole point — the skill's own
"single word-search is not sufficient" warning), which surfaces both true
duplicates ("Sobha" / "Shobha Limited") and coincidentally similar but
UNRELATED companies ("Trent" / "Trident Limited" — different real
businesses that just happen to be edit-distance 2 apart). Only step 4's
email-domain confirmation (or a name that is already exact/near-exact after
normalization) promotes a candidate from "surfaced for human review" to
"its deals get pooled into this run's evidence." A candidate that's merely
fuzzy-similar and has no confirming domain is listed as an unconfirmed
`DuplicateAccount` but never contributes deals — pooling an unrelated
company's deals into this one's scoring would be a real correctness bug,
not just a cosmetic false positive.

**Why fuzzy matching happens in Python, not SQL:** "Sobha" vs "Shobha"
don't share a contiguous substring at all (there's an extra `h` inserted),
so no `ILIKE '%...%'` pattern can catch both sides no matter what variant
string is chosen. Postgres has fuzzy-search extensions (`pg_trgm`,
`fuzzystrmatch`) available on the replica but not installed, and installing
one would be a schema-level write this pipeline isn't allowed to make (see
CHECKLIST.md: never write back to a source). Pulling all ~9k accounts /
~10k deals into Python and scoring them locally is cheap at this scale.

**Confirmed against real, live cases** (not synthetic): "Sobha Ltd." vs
"Shobha Limited" (true duplicate, confirmed via @sobha.com), "Sula Wines"
vs "Sula Vineyards" (true duplicate, confirmed via @sulawines.com), and
"Trent Limited" vs "Trident Limited"/"Tata Trent" (fuzzy-similar but NOT
the same company — correctly excluded from scoring by this module).
"""

from __future__ import annotations

import difflib
import re
from datetime import datetime

from . import zoho_db
from .models import DuplicateAccount, EntityResolution

_NOISE_WORDS = re.compile(
    r"\b(private limited|pvt\.?\s*ltd\.?|limited|ltd\.?|inc\.?|llp|llc|corp\.?|corporation|"
    r"group|industries|india|holdings|enterprises|company|and co|co\.?)\b\.?",
    re.IGNORECASE,
)

# Candidate search threshold — deliberately loose (see module docstring):
# this only controls what gets SURFACED for review, not what gets scored.
CANDIDATE_THRESHOLD = 0.75
_MIN_SUBSTRING_LEN = 3

# A name this close to the query, after normalization, is treated as the
# same company without needing email-domain corroboration (trivial casing/
# punctuation/typo noise, not a different word — "trent" vs "trident" is
# 0.833 on this scale and must NOT clear this bar; see the module
# docstring's real test case).
NAME_EXACT_THRESHOLD = 0.95


def normalize_for_matching(name: str) -> str:
    """Strips legal suffixes and generic corporate noise words ('Group',
    'India', 'Industries' — icp-skill.md's own examples of words that may
    or may not appear on a given record for the same company) down to the
    distinctive core, so matching isn't thrown off by them."""
    text = (name or "").strip().lower()
    text = _NOISE_WORDS.sub("", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return text


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _is_candidate(normalized_query: str, normalized_candidate: str, *, threshold: float = CANDIDATE_THRESHOLD) -> bool:
    """A candidate is worth surfacing if either name contains the other
    (catches 'Sula' vs 'Sula Wines' — a short root against a longer real
    name, in either direction), or the two are fuzzy-similar above
    `threshold`. This is intentionally permissive — see module docstring."""
    if not normalized_query or not normalized_candidate:
        return False
    if len(normalized_query) >= _MIN_SUBSTRING_LEN and len(normalized_candidate) >= _MIN_SUBSTRING_LEN:
        if normalized_query in normalized_candidate or normalized_candidate in normalized_query:
            return True
    return similarity(normalized_query, normalized_candidate) >= threshold


def _account_domains(account_id: int) -> set[str]:
    """This account's own contact email domains, via its own deals'
    `reachout_tracker` rows — never pooled across other candidates, so one
    account's evidence can't be used to wrongly vouch for a different one."""
    domains: set[str] = set()
    for deal in zoho_db.find_deals_by_account_ids([account_id]):
        for reachout in zoho_db.get_reachouts(deal["id"]):
            email = reachout.get("email")
            if email and "@" in email:
                domain = email.rsplit("@", 1)[1].strip().lower()
                if domain:
                    domains.add(domain)
    return domains


def _deal_domains(deal_id: int) -> set[str]:
    domains: set[str] = set()
    for reachout in zoho_db.get_reachouts(deal_id):
        email = reachout.get("email")
        if email and "@" in email:
            domain = email.rsplit("@", 1)[1].strip().lower()
            if domain:
                domains.add(domain)
    return domains


def _domain_confirms(domains: set[str], normalized_query: str) -> bool:
    """icp-skill.md §0.1 step 4: a contact email domain containing the
    queried name is the reliable proof a differently-spelled record is the
    same company (their own example: yogesh.bansal@sobha.com confirms
    'Shobha Limited' is Sobha Limited). Compares against the domain's own
    label (before the first dot) so 'sobha.com' matches query 'sobha'
    without a TLD getting in the way — and against a space-stripped form of
    the query too, since a multi-word normalized name ('sula wines') still
    commonly appears space-free in a real domain ('sulawines.com')."""
    if not normalized_query:
        return False
    compact_query = normalized_query.replace(" ", "")
    for domain in domains:
        label = domain.split(".", 1)[0]
        if normalized_query in label or label in normalized_query or compact_query in label or label in compact_query:
            return True
    return False


# Confirmed live against real data: "Sula Wines" and "Sula Vineyards" are
# the same company (one Client-Lost deal each), but "Sula Vineyards"'s only
# deal has zero logged reachout_tracker rows, so it can never self-confirm
# via _domain_confirms, and its full-string similarity to "Sula Wines"
# (0.75) is well under NAME_EXACT_THRESHOLD -- it was silently excluded
# from matched_account_ids, and with it, a lost deal Gate 3 needed to see.
_MIN_ROOT_TOKEN_LEN = 4


def _root_token(normalized_name: str) -> str | None:
    """icp-skill.md's own §0.1 step 1 technique is to search on "the
    distinctive root only" (its example: "Kalyan" for "Kalyani"). Prefers
    the first word when it's substantial, so "sula wines"/"sula vineyards"
    both reduce to "sula" rather than to a generic trailing word like
    "wines"; falls back to the longest word otherwise."""
    words = normalized_name.split()
    if not words:
        return None
    if len(words[0]) >= _MIN_ROOT_TOKEN_LEN:
        return words[0]
    candidates = [w for w in words if len(w) >= _MIN_ROOT_TOKEN_LEN]
    return max(candidates, key=len) if candidates else None


def _root_token_confirms(normalized_query: str, normalized_candidate: str) -> bool:
    """An exact shared distinctive root is real corroboration that two
    differently-worded names are the same company, unlike mere fuzzy
    edit-distance similarity -- "sula" == "sula" confirms, while "trent" !=
    "trident" still correctly does not (the module's own documented
    true-negative, see the module docstring)."""
    query_root = _root_token(normalized_query)
    return bool(query_root) and query_root == _root_token(normalized_candidate)


def _is_confirmed(normalized_query: str, normalized_name: str, domains: set[str]) -> bool:
    if similarity(normalized_query, normalized_name) >= NAME_EXACT_THRESHOLD:
        return True
    if _domain_confirms(domains, normalized_query):
        return True
    return _root_token_confirms(normalized_query, normalized_name)


def find_matching_accounts(company_name: str, *, threshold: float = CANDIDATE_THRESHOLD, limit: int = 20) -> list[dict]:
    """icp-skill.md §0.1 step 1 — every account record, including
    misspellings. Candidates only; see `resolve()` for confirmation."""
    query = normalize_for_matching(company_name)
    if not query:
        return []

    scored: list[tuple[float, dict]] = []
    for account in zoho_db.list_all_accounts():
        candidate = normalize_for_matching(account.get("account_name") or "")
        if not candidate or not _is_candidate(query, candidate, threshold=threshold):
            continue
        scored.append((similarity(query, candidate), account))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [account for _, account in scored[:limit]]


def find_matching_deals_by_name(company_name: str, *, threshold: float = CANDIDATE_THRESHOLD, limit: int = 20) -> list[dict]:
    """icp-skill.md §0.1 step 3 — fuzzy-match on deal name, independent of
    account. Catches a Potential whose account link is broken or missing.
    Candidates only; see `resolve()` for confirmation."""
    query = normalize_for_matching(company_name)
    if not query:
        return []

    scored: list[tuple[float, dict]] = []
    for deal in zoho_db.list_all_deal_names():
        candidate = normalize_for_matching(deal.get("deal_name") or "")
        if not candidate or not _is_candidate(query, candidate, threshold=threshold):
            continue
        scored.append((similarity(query, candidate), deal))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [deal for _, deal in scored[:limit]]


_HOLDCO_SIGNAL_WORDS = frozenset({"group", "holdings", "holding", "ventures"})

# Legal-form suffixes stripped before checking the LAST remaining word — so
# "XYZ Holdings Limited" is still recognized as ending in "Holdings", not in
# "Limited". Deliberately narrower than `_NOISE_WORDS` above: this only
# strips forms that sit *after* a holdco word in real filings, it never
# strips the holdco word itself.
_LEGAL_SUFFIX_STRIP = re.compile(
    r"\b(private limited|pvt\.?\s*ltd\.?|limited|ltd\.?|inc\.?|llp|llc|corp\.?|corporation)\b\.?\s*$",
    re.IGNORECASE,
)


def _looks_like_group_or_holdco(company_name: str) -> bool:
    """icp-skill.md §0.2 / Scenario 6-7: does the QUERIED name itself read as
    a parent/holding entity rather than an operating company? Deliberately
    narrow — only "Group", "Holdings", "Holding", "Ventures" as the name's
    own last word (after stripping a trailing legal-form suffix). This is
    NOT the same list as `_NOISE_WORDS`/`normalize_for_matching`'s "India",
    "Industries", etc., which are generic corporate noise words that say
    nothing about group/holdco structure (icp-skill.md §0.1 line 45 lists
    both kinds of words together as spelling variants to search for — only
    a subset of them are an actual holdco signal). Deliberately does not
    include "Enterprises": real single operating entities commonly carry
    that word (e.g. "Piramal Enterprises Limited"), so treating it as a
    holdco signal would false-positive on ordinary, perfectly scoreable
    companies — this check must stay a precise signal, not a vibe check."""
    text = (company_name or "").strip()
    if not text:
        return False
    stripped = _LEGAL_SUFFIX_STRIP.sub("", text).strip()
    words = re.findall(r"[a-z]+", stripped.lower())
    if not words:
        return False
    return words[-1] in _HOLDCO_SIGNAL_WORDS


def _has_distinct_sibling_accounts(confirmed_accounts: list[dict]) -> bool:
    """Weaker, speculative signal: do multiple CONFIRMED accounts (not mere
    fuzzy candidates) have names that don't even look like spelling variants
    of each other (similarity below the loose `CANDIDATE_THRESHOLD`)? That's
    consistent with genuinely distinct sibling subsidiaries of a common
    parent. This is intentionally never fired on its own — it only
    strengthens an existing `_looks_like_group_or_holdco` flag. It must not
    be confused with the separate CRM-hygiene "N confirmed account records
    for this company, needs merging" note built elsewhere in `resolve()`,
    which is about probable DUPLICATES of the same company, not siblings."""
    names = [normalize_for_matching(a.get("account_name") or "") for a in confirmed_accounts]
    names = [n for n in names if n]
    if len(names) < 2:
        return False
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if similarity(names[i], names[j]) < CANDIDATE_THRESHOLD:
                return True
    return False


def _pick_contracting_entity_name(accounts: list[dict]) -> str | None:
    """icp-skill.md §0.2, best-effort default: the longest CONFIRMED
    account name tends to be the most complete legal form ('Sobha Limited'
    over 'Sobha Ltd.' or bare 'Sobha'). Genuine holdco/opco ambiguity — a
    group with several *legitimately distinct* legal entities, not just
    spelling variants of one — needs a human or LLM judgment call this
    function deliberately doesn't attempt."""
    names = [a["account_name"] for a in accounts if a.get("account_name")]
    if not names:
        return None
    return max(names, key=len)


def resolve(company_name: str) -> EntityResolution:
    """Runs all four of icp-skill.md §0.1's steps before concluding
    anything — "only after all four steps may the report say 'no deal
    exists.'" Only confirmed accounts/deals (name-exact or email-domain
    corroborated) contribute to `matched_account_ids`/`matched_deal_ids`;
    merely-fuzzy-similar candidates are still listed in `duplicate_accounts`
    (flagged unconfirmed) so a human can check them, but never pool their
    deals into this run's evidence."""
    query = normalize_for_matching(company_name)
    account_candidates = find_matching_accounts(company_name)

    confirmed_account_ids: set[int] = set()
    for account in account_candidates:
        candidate_name = normalize_for_matching(account.get("account_name") or "")
        domains = set() if similarity(query, candidate_name) >= NAME_EXACT_THRESHOLD else _account_domains(account["id"])
        if _is_confirmed(query, candidate_name, domains):
            confirmed_account_ids.add(account["id"])

    deal_candidates = find_matching_deals_by_name(company_name)
    confirmed_deals_by_id: dict[int, dict] = {}
    for deal in deal_candidates:
        candidate_name = normalize_for_matching(deal.get("deal_name") or "")
        account_id = deal.get("account_id")
        if account_id in confirmed_account_ids:
            confirmed_deals_by_id[deal["id"]] = deal
            continue
        domains = set() if similarity(query, candidate_name) >= NAME_EXACT_THRESHOLD else _deal_domains(deal["id"])
        if _is_confirmed(query, candidate_name, domains):
            confirmed_deals_by_id[deal["id"]] = deal
            if account_id:
                confirmed_account_ids.add(account_id)

    account_deals = zoho_db.find_deals_by_account_ids(sorted(confirmed_account_ids)) if confirmed_account_ids else []
    for deal in account_deals:
        confirmed_deals_by_id.setdefault(deal["id"], deal)
    deals = list(confirmed_deals_by_id.values())

    confirmed_accounts = [a for a in account_candidates if a["id"] in confirmed_account_ids]
    # An account confirmed only via the deal-name-independent path (§0.1
    # step 3) never appeared in `account_candidates` (that's account-name
    # search, step 1) -- without this, `confirmed_accounts` could exclude
    # it, leaving `contracting_entity_name` None even though
    # `entity_resolved` is True from `deals` being non-empty. That silently
    # skipped Gate 1's "unresolvable contracting entity -> stop" for a case
    # where the entity genuinely IS resolved, just not by account name.
    missing_ids = confirmed_account_ids - {a["id"] for a in confirmed_accounts}
    for account_id in missing_ids:
        account = zoho_db.get_account(account_id)
        if account:
            confirmed_accounts.append(account)

    # Live-confirmed real gap (Gabriel Auto): an account can be confirmed
    # via the deal-name-independent path (§0.1 step 3) without its own
    # account name ever surfacing in `account_candidates` (step 1's name
    # search) at all -- "Gabriel Auto"'s account-name search only found
    # "Gabriel India", but a deal named "Anand Group - Gabriel Auto - AI
    # Automation" under a DIFFERENT account confirmed that second account
    # via its deal name alone (see the loop above). That second account
    # never appeared in `account_candidates`, so gating duplicate_accounts
    # on `len(account_candidates) > 1` silently dropped a real 2-confirmed-
    # account hygiene finding -- exactly the bug check #2 exists to catch
    # (confirmed live: matched_account_ids had 2 entries, duplicate_accounts
    # had 0). `confirmed_accounts` above already backfills this same gap
    # for contracting_entity_name's sake (see `missing_ids`); this needs
    # the same union.
    candidate_pool_by_id = {a["id"]: a for a in account_candidates}
    for a in confirmed_accounts:
        candidate_pool_by_id.setdefault(a["id"], a)
    candidate_pool = list(candidate_pool_by_id.values())

    duplicate_accounts = [
        DuplicateAccount(
            account_id=a["id"],
            account_name=a["account_name"],
            modified_time=a["modified_time"].isoformat() if isinstance(a.get("modified_time"), datetime) else a.get("modified_time"),
            confirmed=a["id"] in confirmed_account_ids,
        )
        for a in candidate_pool
    ] if len(candidate_pool) > 1 else []

    entity_resolved = bool(confirmed_account_ids) or bool(deals)
    contracting_entity_name = _pick_contracting_entity_name(confirmed_accounts)

    unconfirmed_count = sum(1 for d in duplicate_accounts if not d.confirmed)

    if deals:
        most_recent = max(deals, key=lambda d: d.get("modified_time") or datetime.min)
        note = f"Scoring against the most recently modified Potential ({most_recent.get('deal_name')!r})."
    elif confirmed_accounts:
        note = "Account record(s) found, but no Potential/deal exists yet — ran all four lookup steps before concluding this."
    else:
        note = "No account or Potential record found after the full four-step lookup (icp-skill.md §0.1)."

    if len(confirmed_accounts) > 1:
        note = f"{len(confirmed_accounts)} confirmed account records for this company — CRM hygiene finding, needs merging. " + note
    if unconfirmed_count:
        note += f" {unconfirmed_count} additional similarly-named account(s) found but NOT confirmed as the same company (no matching contact domain) — excluded from scoring, worth a human check."

    # icp-skill.md §0.2 / Scenario 6-7. Scenario 6: "user names a group, not
    # an entity -> Gate 1. Never sum subsidiaries." Scenario 7: "holdco
    # named, opco is the buyer -> score the opco, note the relationship."
    # The dividing line is whether a genuinely more-specific operating
    # entity was actually resolved: if the query itself reads as a
    # group/holdco AND the best contracting-entity name we found is still
    # essentially that same group name (Zoho's own record is itself titled
    # as the group -- no distinct opco surfaced), this IS scenario 6 and
    # must stop, not just carry a caveat a human might miss. If a
    # meaningfully different, more specific name was found, that's scenario
    # 7 -- score it, note the relationship, no Gate 1.
    if _looks_like_group_or_holdco(company_name):
        no_distinct_opco_found = contracting_entity_name is None or (
            similarity(normalize_for_matching(contracting_entity_name), normalize_for_matching(company_name))
            >= NAME_EXACT_THRESHOLD
        )
        if no_distinct_opco_found:
            entity_resolved = False
            note = (
                f"{company_name!r} reads as a group/holding company name, not an operating "
                "entity, and no distinct operating subsidiary could be resolved as the actual "
                "contracting party — never sum revenues across group entities (icp-skill.md "
                "§0.2). Insufficient basis to score until the specific subsidiary is named."
            )
        else:
            group_note = (
                f"{company_name!r} reads as a group/holding company name — scoring against the "
                f"more specific operating entity found, {contracting_entity_name!r}, not the "
                "group itself; do not sum revenues across group entities (icp-skill.md §0.2)."
            )
            if _has_distinct_sibling_accounts(confirmed_accounts):
                group_note += (
                    " Multiple confirmed account records under different-sounding names were "
                    "found, consistent with separate sibling subsidiaries under this parent."
                )
            note += " " + group_note

    return EntityResolution(
        company_name_queried=company_name,
        matched_account_ids=sorted(confirmed_account_ids),
        matched_deal_ids=[d["id"] for d in deals],
        duplicate_accounts=duplicate_accounts,
        contracting_entity_name=contracting_entity_name,
        contracting_entity_note=note,
        entity_resolved=entity_resolved,
    )


def most_recent_deal(deals: list[dict]) -> dict | None:
    """icp-skill.md §0.1: "score against the most recently modified
    Potential." Exposed separately so callers (evidence_assembler.py) that
    already fetched deals via `resolve()`'s ids don't need to re-derive
    this themselves."""
    if not deals:
        return None
    return max(deals, key=lambda d: d.get("modified_time") or datetime.min)
