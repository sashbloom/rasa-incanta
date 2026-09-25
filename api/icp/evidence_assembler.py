# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/evidence_assembler.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Stage 4 — evidence assembly. Turns the now-working connector set (Zoho,
Setu, Anthropic secondary research, `practus_history.py`,
`entity_resolver.py`, `ownership_classifier.py`, Read.ai, Outlook) into one
`EvidenceBundle` the LLM Interpretation stage and scorer can consume.

Financial figures never come from CRM fields for A1/A2/A3 — this module
only ever puts context-only amounts on `CrmStructured` (see that model's
own docstring) and routes every secondary-research/Setu finding into
`unstructured` instead, which is a structurally separate field. That's what
makes the skill's EVIDENCE DISCIPLINE ban real rather than a convention
this module could accidentally violate.

Every external call below is individually wrapped: a failure in one source
(Setu DB down, no meetings for this domain, Outlook mailbox not yet
authorized) becomes a `data_gaps` entry, never an exception that kills the
whole run. Setu is accessed exclusively via direct read-only Postgres
queries (`setu_db.py`) — there is no chat-endpoint fallback.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from . import (
    entity_resolver,
    exa_search,
    mail_client,
    meetings_store,
    ownership_classifier,
    p2_case_study_matcher,
    p3_external_sme_matcher,
    p3_team_matcher,
    practus_history,
    secondary_research,
    tracing,
    zoho_db,
)
from .anthropic_client import research
from .models import (
    CrmStructured,
    DealHistoryRow,
    EntityResolution,
    EvidenceBundle,
    ManualCompanyInfo,
    ReachoutRow,
    SecondaryFinancials,
    UnstructuredEvidenceItem,
)

logger = logging.getLogger(__name__)


def _preview(text: str | None, n: int = 220) -> str:
    """Truncates a long answer/prompt for a log line -- full text already
    lives in evidence_bundle.json on disk; the log just needs enough to
    tell at a glance whether a call returned something real or empty/junk."""
    if not text:
        return "(empty)"
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + f"... [{len(text)} chars total]"


def _stringify(value: object) -> str | None:
    return None if value is None else str(value)


def fetch_resolved_deals(entity: EntityResolution) -> list[dict]:
    """`EntityResolution` only carries deal ids — this re-fetches the full
    rows for the ones `entity_resolver.resolve()` confirmed."""
    return [d for d in (zoho_db.get_deal(deal_id) for deal_id in entity.matched_deal_ids) if d]


def build_crm_structured(deal: dict | None) -> CrmStructured:
    if not deal:
        return CrmStructured()

    reachouts = [
        ReachoutRow(
            reachout_date=_stringify(r.get("reachout_date")),
            client_contact_name=r.get("client_contact_name"),
            designation=r.get("designation"),
            contact_role=r.get("contact_role"),
            email=r.get("email"),
            reachout_medium=r.get("reachout_medium"),
            remarks=r.get("remarks"),
        )
        for r in zoho_db.get_reachouts(deal["id"])
    ]
    stage_history = [
        DealHistoryRow(stage=h["stage"], modified_time=_stringify(h["modified_time"]) or "")
        for h in zoho_db.get_deal_history(deal["id"])
    ]

    return CrmStructured(
        deal_id=deal.get("id"),
        deal_name=deal.get("deal_name"),
        stage=deal.get("stage"),
        stage_modified_time=_stringify(deal.get("modified_time")),
        created_time=_stringify(deal.get("created_time")),
        closing_date=_stringify(deal.get("closing_date")),
        ep_involved=deal.get("ep_involved"),
        el_involved=deal.get("el_involved"),
        designation=deal.get("designation"),
        problem_area_1=deal.get("problem_area_1"),
        problem_area_2=deal.get("problem_area_2"),
        client_problem_statement=deal.get("client_problem_statement"),
        specify_reference=deal.get("specify_reference"),
        potential_lead_source=deal.get("potential_lead_source"),
        reason_for_loss=deal.get("reason_for_loss"),
        industry_type=deal.get("industry_type"),
        reachout_tracker=reachouts,
        stage_history=stage_history,
        amount_context_only=float(deal["amount"]) if deal.get("amount") is not None else None,
        mrr_context_only=float(deal["monthly_recurring_revenue_amount"]) if deal.get("monthly_recurring_revenue_amount") is not None else None,
    )


MANUAL_ENTRY_NOTE = (
    "Manually entered — no Zoho CRM record found for this company after the full "
    "four-step lookup (icp-skill.md §0.1). Stage/industry/problem statement/EP below "
    "are user-supplied, not Zoho-verified; every other criterion still degrades "
    "honestly to Data gap where no evidence was supplied."
)


def build_crm_structured_from_manual(manual: ManualCompanyInfo) -> CrmStructured:
    """Sibling to build_crm_structured() for the no-Zoho-record fallback —
    deliberately NOT a variant of that function, since build_crm_structured()
    is shaped entirely around a real Zoho `deal` dict and unconditionally
    calls zoho_db.get_reachouts()/get_deal_history() against a real
    deal["id"], neither of which a manual entry can supply. reachout_tracker
    and stage_history are left empty; every other field this model has
    (closing_date, reason_for_loss, the *_context_only financials, etc.)
    intentionally has no manual-entry counterpart — see ManualCompanyInfo's
    own docstring for why."""
    return CrmStructured(
        stage=manual.stage,
        industry_type=manual.industry,
        client_problem_statement=manual.problem_statement,
        ep_involved=manual.ep_name,
    )


def problem_statement_text(crm: CrmStructured) -> str | None:
    """icp-skill.md P2 step 1's priority order, collapsed to whichever
    field is actually populated: the deal name often names the problem
    outright ("Cost Visibility & Control") when the dedicated statement
    field is empty, which the skill notes is common on live deals."""
    return crm.client_problem_statement or crm.problem_area_1 or crm.problem_area_2 or crm.deal_name


def contact_domain(crm: CrmStructured) -> str | None:
    for row in crm.reachout_tracker:
        if row.email and "@" in row.email:
            return row.email.rsplit("@", 1)[1].strip().lower()
    return None


def gather_setu_evidence(
    crm: CrmStructured, data_gaps: list[str], *, extra_keyword_context: str = "", geography: str | None = None,
    inferred_problem_statement: str | None = None,
) -> list[UnstructuredEvidenceItem]:
    """`extra_keyword_context` (typically the secondary-research finding
    text, once that's available — see assemble()'s call order) widens the
    P3 team keyword search beyond the CRM's own fields. Live-confirmed gap:
    the CRM deal name alone ("Sula Wines - Growth & Expansion") found the
    resume hit for "wine" but never the real peer-set match (a person whose
    resume names Diageo/Olam as clients) — those names only ever surface in
    secondary research (which identifies real competitors/peers per
    icp-skill.md A3), not in any CRM field.

    `inferred_problem_statement` (see secondary_research.infer_problem_
    statement_from_research()) is icp-skill.md P2 step 1's own documented
    4th/5th-priority sources ("disclosures", "inference") — only ever
    consulted here as a last resort, since it's passed in already computed
    by the caller only when the CRM/meeting/mail sources above it in that
    priority order came up empty."""
    items: list[UnstructuredEvidenceItem] = []

    # 'Others' is filtered the same way P1's confirmed_clients_in_industry()
    # already does -- it's a generic Zoho placeholder, not a real industry.
    industry = crm.industry_type
    if industry and practus_history.is_generic_industry_value(industry):
        industry = None
    service_line = crm.problem_area_1 or crm.problem_area_2

    crm_problem = problem_statement_text(crm)
    problem = crm_problem or inferred_problem_statement
    if problem and not crm_problem:
        # Live-confirmed real gap (Uniparts India, 2026-09-09): a no-CRM
        # manual entry with rich secondary research (dated, quoted
        # management priorities) still scored P2 as "problem not
        # established" because nothing besides the CRM's own (empty)
        # fields ever reached this function OR the P2 scoring prompt.
        # Surfaced as its own evidence item -- not silently folded into
        # `problem` alone -- so llm_interpreter.py's P2 scoring (which
        # reads "evidence tagged for P2 below", not this function's local
        # variables) can see it too and label it an Inference, never a Fact.
        items.append(UnstructuredEvidenceItem(
            text=f"Inferred client problem/priority (no CRM problem-statement field populated for this deal "
            f"-- identified from secondary research/disclosures per icp-skill.md P2's own 4th/5th-priority "
            f"sources, not stated directly by a logged contact): {problem}",
            source="Secondary research (inferred)", source_type="secondary_research", criterion_tags=["P2"],
        ))
    # Live-confirmed real gap (Manappuram Finance, 2026-09-09): the deal's
    # own already-assigned EP, Shashank Silhare, has a real, on-file
    # skill_profile and resume — but the fuzzy industry/keyword search below
    # genuinely scored him 0 (his documented industries don't cover this
    # deal's sector) and silently dropped him, so the report claimed no
    # record existed for him at all. A direct by-name lookup, independent of
    # the fuzzy search's score, is the only way to guarantee the report can
    # always state what's actually on file for whoever is already staffed
    # on this deal. Computed unconditionally (even when the fuzzy search
    # below is skipped or fails) — it's a separate, simple DB lookup with
    # its own fail-open degrade.
    assigned_profiles: list[dict] = []
    for person_name in (crm.ep_involved, crm.el_involved):
        try:
            profile = p3_team_matcher.find_assigned_person_profile(person_name)
        except Exception as exc:
            logger.warning("Assigned EP/EL direct-by-name lookup FAILED for %r: %s", person_name, exc)
            profile = None
        if profile and not any(p["name"] == profile["name"] for p in assigned_profiles):
            assigned_profiles.append(profile)

    # The three Setu lookups below (P2 case studies, P3 team roster, P3
    # external SME) are mutually independent: none reads another's output,
    # they hit different Setu tables, and each owns its own try/except that
    # degrades to a `data_gaps` entry. They ran strictly back to back until
    # now, and Langfuse put real numbers on that (2026-09-10 Manappuram
    # run): 27s + 26s + 27s = ~80s of pure serial waiting for three calls
    # that could have cost ~27s together. Same pattern, and same rationale,
    # as the research()/extract_revenue_figure() pair already parallelized
    # in assemble() below.
    #
    # `assigned_profiles` above is deliberately still computed SEQUENTIALLY,
    # before the fan-out: the P3 team block needs it, and it is a plain
    # by-name DB lookup with no LLM call, so it costs almost nothing and
    # keeping it here avoids making the team block depend on a second
    # future. Results are collected in a FIXED order (P2, P3 team, P3 SME)
    # so the assembled evidence list -- and therefore every prompt built
    # from it downstream -- stays byte-identical to the sequential version;
    # only `data_gaps` ordering can vary, which nothing depends on.
    def _p2_case_studies() -> list[UnstructuredEvidenceItem]:
        if not problem:
            logger.info("P2 case-study lookup SKIPPED — no client problem statement available (Gate 6 territory).")
            data_gaps.append("No client problem statement available — P2 case-study lookup skipped, Gate 6 territory.")
            return []
        problem_context = " / ".join(p for p in (problem, extra_keyword_context) if p)
        try:
            candidates = p2_case_study_matcher.find_case_study_matches(
                problem_context=problem_context, industry=industry, geography=geography,
            )
            matches = p2_case_study_matcher.rerank_case_studies_with_llm(candidates, problem_context=problem_context)
            text = p2_case_study_matcher.format_case_studies_as_evidence_text(matches)
            logger.info(
                "P2 case-study match (direct Setu DB lookup, %d candidates, LLM-reranked to %d): %s",
                len(candidates), len(matches), _preview(text),
            )
            return [UnstructuredEvidenceItem(text=text, source="Setu database", source_type="setu_db", criterion_tags=["P2"])]
        except Exception as exc:
            # Setu is DB-only now (no chat-endpoint fallback) -- a failed
            # lookup is a data gap, not a second, less-reliable attempt.
            logger.warning("P2 case-study direct-DB lookup FAILED: %s", exc)
            data_gaps.append(f"P2 case-study direct-DB lookup failed — treated as data gap. ({exc})")
            return []

    def _p3_external_smes() -> list[UnstructuredEvidenceItem]:
        if not problem:
            return []
        try:
            candidates = p3_external_sme_matcher.find_external_sme_matches(problem_context=problem)
            matches = p3_external_sme_matcher.rerank_external_smes_with_llm(candidates, problem_context=problem)
            text = p3_external_sme_matcher.format_external_smes_as_evidence_text(matches)
            logger.info(
                "P3 external-SME match (direct Setu DB lookup, %d candidates, LLM-reranked to %d): %s",
                len(candidates), len(matches), _preview(text),
            )
            return [UnstructuredEvidenceItem(text=text, source="Setu database", source_type="setu_db", criterion_tags=["P3"])]
        except Exception as exc:
            logger.warning("P3 external-SME direct-DB lookup FAILED: %s", exc)
            data_gaps.append(f"P3 external-SME direct-DB lookup failed — treated as data gap. ({exc})")
            return []

    def _p3_team() -> list[UnstructuredEvidenceItem]:
        relevance_parts = [p for p in (crm.deal_name, industry, service_line) if p]
        if not (relevance_parts or extra_keyword_context):
            logger.info("P3 team lookup SKIPPED — no industry, service line, or deal name known for this deal.")
            data_gaps.append("No industry, service line, or deal name known for this deal — P3 team lookup skipped.")
            if assigned_profiles:
                text = p3_team_matcher.format_matches_as_evidence_text(
                    [], industry=industry, service_line=service_line, assigned_profiles=assigned_profiles,
                )
                return [UnstructuredEvidenceItem(text=text, source="Setu database", source_type="setu_db", criterion_tags=["P3"])]
            return []
        db_keyword_context = " / ".join(relevance_parts + ([extra_keyword_context] if extra_keyword_context else []))
        try:
            # Retrieve a wider pool (10, not the final 4) so the LLM
            # reranking step below has real alternatives to choose among --
            # pure inverse-document-frequency scoring is a good filter for
            # "plausibly relevant" but, confirmed live, occasionally lets a
            # coincidentally-rare irrelevant word outrank a genuinely
            # relevant one; the rerank step is what corrects for that.
            candidates = p3_team_matcher.find_team_matches(
                industry=industry, service_line=service_line, keyword_context=db_keyword_context, limit=10,
            )
            matches = p3_team_matcher.rerank_matches_with_llm(
                candidates, keyword_context=db_keyword_context, industry=industry, service_line=service_line,
            )
            text = p3_team_matcher.format_matches_as_evidence_text(
                matches, industry=industry, service_line=service_line, assigned_profiles=assigned_profiles,
            )
            logger.info(
                "P3 team match (direct Setu DB lookup, %d candidates, LLM-reranked to %d): %s",
                len(candidates), len(matches), _preview(text),
            )
            return [UnstructuredEvidenceItem(text=text, source="Setu database", source_type="setu_db", criterion_tags=["P3"])]
        except Exception as exc:
            logger.warning("P3 team direct-DB lookup FAILED: %s", exc)
            data_gaps.append(f"P3 team direct-DB lookup failed — treated as data gap. ({exc})")
            if assigned_profiles:
                text = p3_team_matcher.format_matches_as_evidence_text(
                    [], industry=industry, service_line=service_line, assigned_profiles=assigned_profiles,
                )
                return [UnstructuredEvidenceItem(text=text, source="Setu database", source_type="setu_db", criterion_tags=["P3"])]
            return []

    with ThreadPoolExecutor(max_workers=3) as pool:
        p2_future = pool.submit(tracing.propagate_context(_p2_case_studies))
        team_future = pool.submit(tracing.propagate_context(_p3_team))
        sme_future = pool.submit(tracing.propagate_context(_p3_external_smes))
        items.extend(p2_future.result())
        items.extend(team_future.result())
        items.extend(sme_future.result())

    return items


def gather_secondary_research(
    *,
    company_name: str,
    contracting_entity: str,
    ownership: ownership_classifier.OwnershipClassification,
    data_gaps: list[str],
    now: datetime,
) -> list[UnstructuredEvidenceItem]:
    # Live evidence-gathering now happens via Exa (exa_search.search_many()),
    # not Anthropic's own agentic web_search tool -- see the plan's "run
    # time" investigation: this was the single most expensive call site in
    # a real run (~12 min) before this migration. `research()` below is now
    # a plain, tools-free synthesis call over the evidence gathered here.
    bucket = secondary_research.source_bucket(
        geography=ownership.geography, is_listed=ownership.is_listed, is_pe_vc_funded=ownership.is_pe_vc_funded,
    )
    queries = secondary_research.build_research_queries(contracting_entity, bucket=bucket)
    evidence_block = exa_search.search_many(queries, num_results_per_query=6)
    logger.info(
        "Secondary research: Exa search_many() gathered %d char(s) across %d quer(ies) for %r",
        len(evidence_block), len(queries), contracting_entity,
    )
    if not evidence_block:
        logger.warning("Secondary research call SKIPPED — Exa search returned no usable results for any query.")
        data_gaps.append("Secondary research: Exa search returned no usable results — treated as a data gap.")
        return []

    prompt = secondary_research.build_research_prompt(
        company_name=company_name,
        contracting_entity=contracting_entity,
        geography=ownership.geography,
        is_listed=ownership.is_listed,
        is_pe_vc_funded=ownership.is_pe_vc_funded,
        control=ownership.control,
        today=now.strftime("%Y-%m-%d"),
        needed=[
            "latest 3 years' revenue and operating margin trend",
            "three named peers in the same industry with their operating margins",
            "any of the 9 hard-trigger types (CXO change, M&A, new PE/VC event, transformation "
            "announced, expansion announced, performance shock, regulatory deadline, "
            "covenant/refinancing, succession event) dated within the last 12 months",
        ]
        + secondary_research.build_extra_needed_items(ownership.control),
    ) + (
        f"\n\nSEARCH RESULTS ALREADY GATHERED (you have no further tool access — cite from these; "
        f"explicitly note anything from the list above that isn't covered here):\n{evidence_block}"
    )
    logger.info("Secondary research prompt (%s, %s): %s", contracting_entity, ownership.geography, _preview(prompt, 300))
    try:
        text = research(prompt)
    except Exception as exc:
        logger.warning("Secondary research call FAILED: %s", exc)
        data_gaps.append(f"Secondary research call failed — treated as data gap. ({exc})")
        return []
    logger.info("Secondary research answer (%d chars): %s", len(text), _preview(text))
    return [UnstructuredEvidenceItem(text=text, source="Secondary research", source_type="secondary_research")]


def gather_secondary_financials(
    *, contracting_entity: str, ownership: ownership_classifier.OwnershipClassification, data_gaps: list[str], now: datetime
) -> SecondaryFinancials:
    """The structured companion to gather_secondary_research()'s free text —
    see secondary_research.extract_revenue_figure()'s docstring for why the
    ticket-ratio math needs a real number, not prose."""
    logger.info(
        "Secondary-research revenue extraction: entity=%r geography=%s control=%s",
        contracting_entity, ownership.geography, ownership.control,
    )
    try:
        data = secondary_research.extract_revenue_figure(
            contracting_entity=contracting_entity,
            geography=ownership.geography,
            is_listed=ownership.is_listed,
            is_pe_vc_funded=ownership.is_pe_vc_funded,
            control=ownership.control,
            today=now.strftime("%Y-%m-%d"),
        )
    except Exception as exc:
        logger.warning("Secondary-research revenue/EBITDA extraction FAILED: %s", exc)
        data_gaps.append(f"Secondary-research revenue/EBITDA extraction failed — treated as a data gap. ({exc})")
        return SecondaryFinancials()
    logger.info("Secondary-research revenue extraction result: %s", data)
    return SecondaryFinancials(
        revenue_cr=data.get("revenue_cr"),
        ebitda_cr=data.get("ebitda_cr"),
        post_money_cash_cr=data.get("post_money_cash_cr"),
        source=data.get("source"),
        date=data.get("date"),
    )


def gather_meeting_evidence(crm: CrmStructured, data_gaps: list[str]) -> list[UnstructuredEvidenceItem]:
    domain = contact_domain(crm)
    if not domain:
        logger.info("Read.ai meeting lookup SKIPPED — no contact email on file to derive a domain from.")
        data_gaps.append("No contact email on file — Read.ai meeting lookup skipped.")
        return []

    logger.info("Read.ai meeting lookup: participant domain=%r, since_days=180", domain)
    meetings = meetings_store.find_by_participant_domain(domain, since_days=180)
    logger.info("Read.ai meeting lookup found %d meeting(s) for domain %r", len(meetings), domain)
    if not meetings:
        return []

    items = []
    for meeting in meetings[:5]:
        summary = meeting.get("summary") or "(no summary recorded)"
        items.append(
            UnstructuredEvidenceItem(
                text=f"Meeting \"{meeting.get('title')}\" on {meeting.get('start_time')}: {summary}",
                source="Read.ai",
                source_type="meeting",
                date=meeting.get("start_time"),
                criterion_tags=["B1", "B3", "C1", "C2", "P2"],
            )
        )
    return items


def gather_mailbox_evidence(company_name: str) -> list[UnstructuredEvidenceItem]:
    logger.info("Outlook mail search: query=%r across configured mailboxes", company_name)
    messages = mail_client.search_configured_mailboxes(company_name)
    logger.info(
        "Outlook mail search found %d message(s): %s",
        len(messages), [m.get("subject") for m in messages][:10],
    )
    return [
        UnstructuredEvidenceItem(
            text=f"Email \"{m.get('subject')}\" from {(m.get('from') or {}).get('emailAddress', {}).get('address', 'unknown')}: {m.get('bodyPreview', '')}",
            source=f"Outlook ({m.get('_mailbox')})",
            source_type="outlook",
            date=m.get("receivedDateTime"),
        )
        for m in messages
    ]


def assemble(
    company_name: str,
    *,
    now: datetime | None = None,
    on_industry_hint: Callable[[str | None], None] | None = None,
    manual_info: ManualCompanyInfo | None = None,
) -> EvidenceBundle:
    """Ties every connector together for one company. Runs entity
    resolution first (everything else needs the confirmed deal/account
    set), then gathers each evidence source independently so one source
    failing doesn't block the rest.

    `now` is threaded down to `practus_history.classify()` for Gate 3's
    24-month lookback (scenario 17); orchestrator.run() passes the same
    `generated_at` it uses everywhere else so the whole run is anchored to
    one timestamp. Defaults to the current UTC time for callers that don't
    supply one.

    `on_industry_hint`, when given, is called once with `crm.industry_type`
    as soon as the CRM record is built — well before the slow web-search
    calls below run. This is how orchestrator.py kicks off Gate 4's
    `conflict_checker.identify_competitors()` as an early background future
    that overlaps with the rest of evidence assembly, instead of paying for
    it sequentially, on its own, after interpretation (live-confirmed real
    gap: on a real ~44-minute evidence-assembly run, this call's own ~30s
    only ran once everything else was already done, purely because nothing
    kicked it off any earlier — it doesn't depend on anything computed
    after the CRM record). `assemble()`'s own return contract (a plain
    `EvidenceBundle`, no `Future` inside it) is intentionally unchanged —
    the future this callback creates lives entirely in the caller's own
    scope, not on the bundle."""
    now = now or datetime.now(timezone.utc)
    data_gaps: list[str] = []

    logger.info("Entity resolution (§0.1 four-step lookup): querying %r", company_name)
    entity = entity_resolver.resolve(company_name)
    logger.info(
        "Entity resolution result: resolved=%s contracting_entity=%r matched_accounts=%s matched_deals=%s "
        "duplicates=%s",
        entity.entity_resolved, entity.contracting_entity_name, entity.matched_account_ids,
        entity.matched_deal_ids, [(d.account_name, d.confirmed) for d in entity.duplicate_accounts],
    )
    deals = fetch_resolved_deals(entity)
    most_recent = entity_resolver.most_recent_deal(deals)
    logger.info(
        "Scoring against Potential: %r (stage=%r) out of %d confirmed deal(s)",
        most_recent.get("deal_name") if most_recent else None,
        most_recent.get("stage") if most_recent else None, len(deals),
    )
    crm = build_crm_structured(most_recent)

    # Gate 1 fallback: a real Zoho record always wins (this only ever fires
    # when the resolver above still came back unresolved), so a manual entry
    # can never silently mask a findable CRM record. See ManualCompanyInfo's
    # own docstring for why only this small field subset is exposed.
    if not entity.entity_resolved and manual_info is not None and manual_info.has_content():
        note = MANUAL_ENTRY_NOTE
        if manual_info.website:
            # Surfaced in the report itself (not just used for search below)
            # so a reader can quickly confirm this is really the intended
            # company, not a same-named one -- the whole point of asking for
            # a website when the exact legal name isn't known.
            note = f"{note} Website provided by user: {manual_info.website}."
        entity = entity.model_copy(update={
            "entity_resolved": True,
            "contracting_entity_name": entity.contracting_entity_name or manual_info.legal_name or company_name,
            "contracting_entity_note": note,
        })
        crm = build_crm_structured_from_manual(manual_info)
        data_gaps.append("No Zoho CRM record for this company — manually entered evidence used instead of Gate 1's stop.")
        logger.info("Manual company-info override applied — entity_resolved forced True (no Zoho record found).")

    if on_industry_hint is not None:
        on_industry_hint(crm.industry_type)

    name_variants = [company_name] + [d.account_name for d in entity.duplicate_accounts]
    if entity.contracting_entity_name:
        name_variants.append(entity.contracting_entity_name)
    history = practus_history.classify(name_variants, entity.matched_account_ids, now=now, industry=crm.industry_type)
    if history.industry_confirmed_client_names:
        # Live-confirmed real gap (Manappuram Finance, 2026-09-09): the
        # CRM's own industry_type field is BD-entered per deal and
        # occasionally wrong for one specific client (a law firm, a
        # hospital deal, a nonprofit) -- confirmed_clients_in_industry()
        # stays pure/LLM-free by design, so this judgment call happens here
        # instead, on its result, same as the keyword-proxy-match call
        # right below.
        deduped_names = practus_history.deduplicate_client_names(history.industry_confirmed_client_names)
        filtered_names = practus_history.filter_sector_irrelevant_names(crm.industry_type, deduped_names)
        if filtered_names != history.industry_confirmed_client_names:
            history = history.model_copy(update={"industry_confirmed_client_names": filtered_names})
    logger.info(
        "Practus-history classification (Zoho Client Won + Client Names file): is_practus_client=%s "
        "client_lost_count=%s client_lost_unexplained_count=%s (Gate 3 fires at >=2) | P1 industry=%r "
        "confirmed clients in industry (%d): %s",
        history.is_practus_client, history.client_lost_count, history.client_lost_unexplained_count,
        crm.industry_type, len(history.industry_confirmed_client_names), history.industry_confirmed_client_names,
    )

    contracting_entity = entity.contracting_entity_name or company_name
    # A manually-supplied website is a much stronger disambiguator than a
    # possibly-approximate name for the web-search-based calls below (there
    # is no guarantee a manually-typed name is unique) -- fold it into the
    # string these searches key off of, without touching contracting_entity
    # itself, which stays the clean display name used everywhere else
    # (report header, Setu queries, P1 industry matching).
    search_entity = contracting_entity
    if manual_info is not None and manual_info.website:
        search_entity = f"{contracting_entity} ({manual_info.website})"

    logger.info("Ownership classification (web search): company=%r contracting_entity=%r", company_name, search_entity)
    try:
        ownership = ownership_classifier.classify(company_name, search_entity)
        logger.info(
            "Ownership classification result: control=%s disclosure=%r management=%r confidence=%s geography=%s",
            ownership.control, ownership.disclosure, ownership.management, ownership.confidence, ownership.geography,
        )
        entity = entity.model_copy(update={
            "ownership_control": ownership.control,
            "ownership_disclosure": ownership.disclosure,
            "ownership_management": ownership.management,
            "ownership_confidence": ownership.confidence,
            "ipo_track": ownership.ipo_track,
        })
    except Exception as exc:
        ownership = None
        logger.warning("Ownership classification call FAILED: %s", exc)
        data_gaps.append(f"Ownership classification call failed — control/disclosure/management are data gaps. ({exc})")

    unstructured: list[UnstructuredEvidenceItem] = []
    secondary_financials = SecondaryFinancials()
    secondary_research_items: list[UnstructuredEvidenceItem] = []
    if ownership is not None:
        # Live-confirmed real gap: these two calls are mutually independent
        # (both only need `ownership`'s geography/control, not each other's
        # output) but previously ran one after another -- on a real run
        # (Stanley Lifestyle, 2026-09-03) research() took ~12 min and
        # extract_revenue_figure() a further ~30 min RIGHT AFTER IT, back to
        # back, ~42 min combined for two calls that could overlap. Running
        # them concurrently (ThreadPoolExecutor -- these are blocking I/O-
        # bound network calls, so real wall-clock concurrency without an
        # asyncio rewrite) drops that combined wait to roughly the SLOWER of
        # the two alone. Both append to the shared `data_gaps` list on
        # failure -- safe from two threads, since CPython's `list.append()`
        # is a single atomic bytecode op; only the ORDER of any appended
        # messages becomes non-deterministic, which is harmless here.
        with ThreadPoolExecutor(max_workers=2) as pool:
            research_future = pool.submit(
                tracing.propagate_context(gather_secondary_research),
                company_name=company_name,
                contracting_entity=search_entity,
                ownership=ownership,
                data_gaps=data_gaps,
                now=now,
            )
            financials_future = pool.submit(
                tracing.propagate_context(gather_secondary_financials),
                contracting_entity=search_entity, ownership=ownership, data_gaps=data_gaps, now=now,
            )
            # Run before gather_setu_evidence() specifically so its findings
            # (real sector/peer/competitor names — icp-skill.md A3 already
            # asks this call to identify named peers) can widen the P3 team
            # keyword search below. Live-confirmed gap this fixes: the CRM's
            # own fields alone found a resume hit for "wine" but never the
            # real peer-set match (someone whose resume names actual
            # industry peers as clients) — those names only ever show up in
            # secondary research, never in any CRM field.
            secondary_research_items = research_future.result()
            secondary_financials = financials_future.result()
        unstructured.extend(secondary_research_items)
    extra_keyword_context = " ".join(item.text for item in secondary_research_items)

    # Live-confirmed real gap: a real confirmed client ("Theobroma Foods
    # Private Limited", tagged FMCG in the Client Names file) never
    # surfaced for a wine/beverage prospect because the CRM's own
    # industry_type field is the generic 'Others' -- confirmed_clients_in_
    # industry() can only ever compare against that broken field. Once
    # secondary research names the company's REAL sector (e.g. "FMCG"),
    # try a keyword-based proxy match instead of accepting the exact-match
    # zero outright.
    if not history.industry_confirmed_client_names and extra_keyword_context:
        keyword_industry, keyword_clients = practus_history.confirmed_clients_by_keyword_context(
            f"{crm.deal_name or ''} {extra_keyword_context}"
        )
        if keyword_clients and keyword_industry:
            keyword_clients = practus_history.deduplicate_client_names(keyword_clients)
            keyword_clients = practus_history.filter_sector_irrelevant_names(keyword_industry, keyword_clients)
        if keyword_clients:
            logger.info(
                "P1 industry proxy match (LLM classification, not keyword scoring): industry=%r client(s)=%s",
                keyword_industry, keyword_clients,
            )
            history = history.model_copy(update={
                "keyword_matched_industry": keyword_industry,
                "keyword_matched_client_names": keyword_clients,
            })

    # Live-confirmed real gap (Uniparts India, 2026-09-09): icp-skill.md P2
    # step 1's own priority order names 5 sources for the client's problem
    # (Potential's own fields, Read.ai, Outlook, then "disclosures"/
    # "inference") -- only the first three were ever actually wired up.
    # Only worth the extra LLM call when the CRM's own fields are genuinely
    # empty AND there's real research text to infer from; skip it outright
    # otherwise (the common CRM-populated case, and Gate 6's own genuinely-
    # unknown case, both need nothing more here).
    inferred_problem = None
    if not problem_statement_text(crm) and extra_keyword_context:
        inferred_problem = secondary_research.infer_problem_statement_from_research(
            extra_keyword_context, contracting_entity
        )

    unstructured.extend(
        gather_setu_evidence(
            crm, data_gaps, extra_keyword_context=extra_keyword_context,
            geography=ownership.geography if ownership is not None else None,
            inferred_problem_statement=inferred_problem,
        )
    )
    unstructured.extend(gather_meeting_evidence(crm, data_gaps))
    try:
        unstructured.extend(gather_mailbox_evidence(company_name))
    except Exception as exc:
        # Live-confirmed real gap: this call was the one exception to this
        # module's own "every external call degrades to a data gap, never
        # a crash" rule -- a fresh deploy with no Graph OAuth refresh token
        # on file yet (graph_oauth_setup.py never run there) took down the
        # entire run instead of just skipping Outlook evidence, exactly
        # the failure mode this module's docstring already promises never
        # happens.
        logger.warning("Outlook mail search FAILED: %s", exc)
        data_gaps.append(f"Outlook mail search failed — treated as data gap. ({exc})")

    if not entity.entity_resolved:
        data_gaps.append("Entity could not be resolved after the full four-step lookup — Gate 1 territory.")

    logger.info(
        "Evidence assembly done for %r: %d unstructured item(s) gathered, %d data gap(s): %s",
        company_name, len(unstructured), len(data_gaps), data_gaps,
    )

    return EvidenceBundle(
        entity=entity,
        secondary_financials=secondary_financials,
        crm=crm,
        practus_history=history,
        unstructured=unstructured,
        data_gaps=data_gaps,
    )
