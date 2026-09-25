# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/llm_interpreter.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Builds the LLM Interpretation prompt from an EvidenceBundle and validates
the response's condition_labels against the closed enum each criterion
allows (criteria_tables.allowed_labels) before anything downstream trusts
them. An invalid or missing label becomes a Data-gap entry, never a guess.
"""

from __future__ import annotations

from . import criteria_tables as ct
from . import rule_precompute as rp
from .anthropic_client import interpret_criteria
from .models import CLOSED_STAGES, CriterionInterpretation, EvidenceBundle, EvidenceLabel, OwnershipControl, Stage

# Criteria this stage asks the LLM to judge at all — C2 is pure rule-based
# (see rule_precompute/scorer), gates are resolved separately.
LLM_CRITERIA = ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C3", "P1", "P2", "P3", "P4"]


def _format_label(label: str) -> str:
    return label.replace("_", " ")


def _criterion_block(criterion_id: str, control: OwnershipControl, *, stage: str | None = None) -> str:
    labels = ct.allowed_labels(criterion_id, control)
    options = "\n".join(f"  - {label} — {_format_label(label)}" for label in labels)
    block = f"{criterion_id}:\n{options}"

    if criterion_id == "A2":
        # icp-skill.md §0.4: "two modes by stage" -- Mode A (QP+, compute
        # both ratios) vs Mode B (earlier stages, no ticket, no ratio,
        # absolute liquidity only). Previously the model saw both modes'
        # labels merged into one list with no indication which one applies
        # to THIS deal -- QC check #8 caught a wrong mode only after the
        # fact. State it up front instead so a mismatch is prevented, not
        # just detected.
        mode = rp.a2_mode(stage)
        if mode == "A":
            block += (
                "\n  This deal's stage puts it in MODE A (Qualified Prospect or later): only the "
                "Mode A labels below (ticket-ratio-based) apply — compute and state BOTH "
                "ticket÷revenue and ticket÷EBITDA using the ratio evidence given below, tagged "
                "'A2 Mode A ONLY'. Ignore the Mode B (absolute-liquidity) labels entirely."
            )
        else:
            block += (
                "\n  This deal's stage puts it in MODE B (Prospect / Need Identification / "
                "Walkthrough Scheduled): only the Mode B labels below (absolute-liquidity-based) "
                "apply — there is NO real ticket yet, so state NO ratio and ignore any ticket "
                "figure entirely. Ignore the Mode A (ratio-based) labels entirely."
            )

    # icp-skill.md gives per-control-type navigation guidance for these four
    # criteria (A2 lines 157-169, A3 lines 184-193, B1 lines 169-181, C1
    # lines 290-304) so the model knows where evidence for this criterion
    # actually lives for THIS entity's ownership type — e.g. a CP
    # subsidiary's local cash is swept to parent and near-zero by design, so
    # scoring A2 off local cash alone is exactly the misread the skill's
    # routing table exists to prevent. Surface it here, not just in
    # criteria_tables.py, or the model never sees it.
    routing = None
    if criterion_id == "A2":
        routing = ct.A2_LIQUIDITY_ROUTING.get(control)
    elif criterion_id == "A3":
        routing = ct.A3_MEANINGFUL_MARGIN_PROSE.get(control)
    elif criterion_id == "B1":
        routing = ct.B1_TRIGGER_ROUTING.get(control)
    elif criterion_id == "C1":
        routing = ct.C1_AUTHORITY_ROUTING.get(control)

    if routing:
        block += f"\n  Routing for {control.value}: {routing}"

    if criterion_id == "P4":
        # icp-skill.md P4: "The Potential's Specify_Reference and
        # Potential_Lead_Source fields name the referrer — a named referrer
        # counts as a champion." Previously this equivalence was never
        # stated -- the raw field values appeared in the prompt with no
        # instruction connecting them to the champion condition, unlike
        # every other control-routed criterion.
        block += (
            "\n  General rule: if 'Named referrer / lead source' below names a real person or "
            "organization, that counts as a champion for this criterion. A NAMED incumbent "
            "(not just 'likely' or 'probably Tier-1') is required to score below 3 — an assumed "
            "incumbent without a name is not evidence."
        )

    if criterion_id == "P1":
        # icp-skill.md P1: "Counts ONLY confirmed Practus clients — Zoho
        # Client Won + the Client Names file" -- the real count is
        # rule-computed (practus_history.confirmed_clients_in_industry) and
        # given below as a fact, not something to infer from prose evidence.
        block += (
            "\n  General rule: base this ONLY on the confirmed-clients-in-industry list given "
            "below in the evidence section (already counted from Zoho Client Won + the Client "
            "Names file) — never estimate or infer a client count from secondary research or "
            "general knowledge. 2+ of them need a citable case study (see the P2 evidence) to "
            "count toward the top band; otherwise treat the count alone as the ceiling."
        )

    if criterion_id == "P2":
        # icp-skill.md P2: step 1 establishes the client's problem in
        # priority order (the Potential's own Deal_Name/Problem_Area/
        # Client_s_Problem_Statement, then Read.ai, then Outlook, then
        # disclosures, then inference); step 2 is proof via Setu case-study
        # search, tagged [setu/...] with criterion_tags P2 in the evidence
        # below. Never invent an impact number.
        block += (
            "\n  General rule: use ONLY the evidence tagged for P2 below (the client's problem, "
            "sourced in priority order from the deal itself, then meetings, then mail, then "
            "disclosures, then inference) plus the Setu case-study evidence for proof. Score 1 "
            "if the problem itself cannot be established at all (Gate 6 territory) — never invent "
            "a specific impact number where none is stated. If the deal's own name points at a "
            "different topic than the stated Client problem statement (e.g. a deal named "
            "'...Operations' but a problem statement of 'Business growth') and nothing else in "
            "the evidence (meetings, Outlook, disclosures) reconciles the two, do NOT accept the "
            "stated problem at face value — treat it as not reliably established, score "
            "'problem_not_established_or_nothing_relevant' (Gate 6 territory), and say in "
            "supporting_facts that the deal name and stated problem disagree and neither is "
            "corroborated elsewhere."
        )

    if criterion_id == "P3":
        # icp-skill.md P3: the match test is industry AND service line AND
        # credible named clients, all three -- one or two of three is
        # adjacency only, named but not counted at the top band. The
        # knowledge-base Grade field (EP/EL/TL) is authoritative; prior-
        # career uplift is +1, capped at 5, applied once.
        block += (
            "\n  General rule: use ONLY the Setu team-search evidence tagged P3 below. A full "
            "match needs industry match AND service-line match AND credible named clients -- one "
            "or two of the three only is adjacency, name it but don't count it as a full match. "
            "The Setu-reported grade (EP/EL/TL) is authoritative over any other source. Apply the "
            "prior-career uplift (+1, capped at 5) at most once, only where the evidence names a "
            "specific person's pre-Practus experience directly relevant to this industry/problem."
        )

    if criterion_id == "C1":
        # icp-skill.md C1: "verification mandatory -- never score from the
        # Zoho Designation field alone... the Reachout_tracker sub-form is
        # usually MORE reliable than the deal-level Designation field --
        # carries the designation recorded at the actual meeting plus the
        # contact's email. Where the two disagree, prefer the tracker and
        # say so." Stated explicitly here, not left implicit, since this is
        # the general rule the per-control routing text above doesn't cover.
        block += (
            "\n  General rule: classify authority (Signatory / Sponsor / Influencer / "
            "Information-gatherer) from the CONTACTS LOGGED ON THIS DEAL below, and from "
            "company-leadership/LinkedIn context if present in the evidence -- never from the "
            "single deal-level 'Designation' field alone. If the deal-level Designation and a "
            "contact's tracker-logged designation disagree, prefer the tracker's and say in "
            "supporting_facts which one you used and that the other disagreed. If the CONTACTS "
            "LOGGED list below is empty (e.g. a no-CRM/manually-entered company with no Reachout_"
            "tracker history at all), you MUST still check the secondary-research evidence below "
            "for a named senior executive (MD/CEO/Chairman/Founder/Promoter) described as leading, "
            "representing, or speaking publicly for the company -- a real, named executive found "
            "there is legitimate C1 evidence (score it Sponsor or higher, per how authoritatively "
            "they're described) and is NOT the same thing as 'nobody identified'. Only score "
            "'information_gatherer_only_or_nobody_identified' when NEITHER the logged contacts NOR "
            "the secondary research names anyone with real authority."
        )

    return block


def _closed_deal_warning(crm) -> str:
    """icp-skill.md scenario 36: "Problem statement present on a CLOSED
    deal -> historical context only, never the live problem." Only ever
    matters when the scored Potential itself is Client Won/Lost (a repeat
    pursuit) -- flags this so the model doesn't score P2/B3 as if this were
    a freshly-stated current problem."""
    try:
        is_closed = crm.stage is not None and Stage(crm.stage) in CLOSED_STAGES
    except ValueError:
        is_closed = False
    if not is_closed:
        return ""
    return (
        f" [NOTE: this deal's stage is {crm.stage!r} — CLOSED. This problem statement is historical "
        "context from that past pursuit only, NOT evidence of a live/current problem for a new pitch — "
        "do not score it as freshly established for P2 or B3.]"
    )


def _format_reachout_tracker(crm) -> str:
    """C1/C2's primary evidence source (icp-skill.md: "the Reachout_tracker
    sub-form on the Potential is usually more reliable than the deal-level
    Designation field"). Rendered as its own block so the model can actually
    classify Signatory/Sponsor/Influencer/Information-gatherer instead of
    guessing from secondary-research prose alone."""
    if not crm.reachout_tracker:
        return "(none logged)"
    return "\n".join(
        f"  - {row.reachout_date or 'undated'}: {row.client_contact_name or 'unnamed contact'} "
        f"({row.designation or 'designation unknown'}, role: {row.contact_role or 'unknown'}) "
        f"via {row.reachout_medium or 'unknown medium'}"
        + (f" — {row.remarks}" if row.remarks else "")
        + (f" [{row.email}]" if row.email else "")
        for row in crm.reachout_tracker
    )


def _p1_confirmed_clients_block(bundle: EvidenceBundle) -> str:
    """Live-confirmed real bug (Uniparts India, 2026-09-09): this scoring
    evidence block only ever read `industry_confirmed_client_names` (the
    CRM-exact-match field), while `narrative/practus_section.py`'s
    cred_cards prompt already had a fallback to `keyword_matched_client_names`
    (the real-sector proxy match used when the CRM's own industry field is
    generic/unreliable — see practus_history.confirmed_clients_by_keyword_context()).
    Result: a real run scored P1 as "zero_confirmed" (1/5) while its OWN
    narrative section, two paragraphs later in the same report, listed 21
    named confirmed clients in the company's real sector — a direct,
    visible self-contradiction in the finished report. Mirrors
    practus_section.py's exact same fallback so scoring and narrative can
    never again disagree on which clients count."""
    history = bundle.practus_history
    crm = bundle.crm
    if history.industry_confirmed_client_names:
        names = history.industry_confirmed_client_names
        return (
            f"Confirmed Practus clients in this company's industry ({crm.industry_type or 'unknown'}) -- from "
            f"Zoho Client Won + the Client Names file, this is P1's primary evidence, already counted for you:\n"
            f"{len(names)} confirmed: {', '.join(names)}"
        )
    if history.keyword_matched_client_names:
        names = history.keyword_matched_client_names
        return (
            f"Confirmed Practus clients in this company's REAL sector, \"{history.keyword_matched_industry}\" -- "
            f"the CRM's own industry field is generic/unreliable for this deal, so this is a proxy match "
            f"identified from secondary research, NOT an exact-industry match. State this plainly (e.g. "
            f"\"no exact-industry match, but N confirmed clients exist in {history.keyword_matched_industry}, "
            f"the company's real sector\") -- never claim this is an exact target-industry match. This is "
            f"still P1's primary evidence, already counted for you:\n"
            f"{len(names)} confirmed: {', '.join(names)}"
        )
    return (
        f"Confirmed Practus clients in this company's industry ({crm.industry_type or 'unknown'}) -- from Zoho "
        f"Client Won + the Client Names file, this is P1's primary evidence, already counted for you:\n"
        f"0 confirmed: (none)"
    )


def build_prompt(bundle: EvidenceBundle, control: OwnershipControl) -> str:
    stage = bundle.crm.stage
    criteria_section = "\n\n".join(_criterion_block(cid, control, stage=stage) for cid in LLM_CRITERIA)
    evidence_lines = "\n".join(
        f"- [{item.source_type}/{item.source}"
        + (f", {item.date}" if item.date else "")
        + (f", tagged for {'/'.join(item.criterion_tags)}" if item.criterion_tags else "")
        + f"] {item.text}"
        for item in bundle.unstructured
    )
    crm = bundle.crm
    return f"""Score this prospect against the criteria below. For EACH criterion, choose exactly
one condition_label from its listed options — copy it verbatim, never invent a new one.
Never output a raw numeric score; the label is your only scoring output. Cite the
evidence you used in supporting_facts, labeling each Fact / Inference / Assumption /
Data gap. If NOTHING in the evidence supports a criterion, do NOT guess a label —
set condition_label to an EMPTY STRING and data_gap=true instead. Each criterion has
its own specific "if silent" scoring rule (e.g. some default to a neutral middle score,
never to the worst option) that is applied automatically downstream from an empty label
— picking a real label yourself when there is no evidence would override that correct
rule with a guess, which is exactly what must not happen.
Never use the deal ticket size / CRM Amount, or any ticket-to-revenue ratio, as evidence
for A1 (Scale) or A3 (Financial trajectory) — those score the COMPANY's own revenue and
margin trend, from secondary research, never the size of this one deal. A ticket-derived
figure is A2-only evidence, and only when explicitly labeled as such below.

Company: {bundle.entity.contracting_entity_name or bundle.entity.company_name_queried}
Ownership control type: {control.value}
Deal stage: {crm.stage or "unknown"}
Deal name: {crm.deal_name or "unknown"}
Problem area fields (CRM): {crm.problem_area_1 or ""} {crm.problem_area_2 or ""}
Client problem statement (CRM, may be empty on a live deal): {crm.client_problem_statement or ""}{_closed_deal_warning(crm)}
EP/EL already assigned: {crm.ep_involved or "none"} / {crm.el_involved or "none"}
Named referrer / lead source: {crm.specify_reference or "none"} / {crm.potential_lead_source or "none"}
Deal-level Designation field (CRM, BD-entered -- for C1, prefer the contacts logged below over this if they disagree): {crm.designation or "none"}
Contacts logged on this deal (Reachout_tracker -- this is C1/C2's primary evidence):
{_format_reachout_tracker(crm)}

{_p1_confirmed_clients_block(bundle)}

CRITERIA AND ALLOWED LABELS:

{criteria_section}

EVIDENCE GATHERED (Setu answers, secondary research, meeting transcripts, Outlook threads):

{evidence_lines or "(none gathered)"}
"""


def interpret(bundle: EvidenceBundle, control: OwnershipControl) -> dict[str, CriterionInterpretation]:
    prompt = build_prompt(bundle, control)
    result = interpret_criteria(prompt)

    validated: dict[str, CriterionInterpretation] = {}
    for interp in result.interpretations:
        if interp.criterion_id not in LLM_CRITERIA:
            continue
        allowed = set(ct.allowed_labels(interp.criterion_id, control))
        if interp.condition_label not in allowed:
            validated[interp.criterion_id] = CriterionInterpretation(
                criterion_id=interp.criterion_id,
                condition_label="",
                supporting_facts=[],
                evidence_label_overall=EvidenceLabel.DATA_GAP,
                rationale=f"Model returned an out-of-enum label ({interp.condition_label!r}) — treated as a data gap.",
                data_gap=True,
            )
        else:
            validated[interp.criterion_id] = interp

    return validated
