# Copied from icp-bot/backend/tests/test_llm_interpreter.py; only import paths are adapted.
"""Tests that build_prompt/_criterion_block inject the skill's per-control-type
routing guidance (icp-skill.md's A2/A3/B1/C1 routing tables) into the prompt
for the entity's actual ownership control type. Before this, criteria_tables.py
defined A2_LIQUIDITY_ROUTING/A3_MEANINGFUL_MARGIN_PROSE/B1_TRIGGER_ROUTING/
C1_AUTHORITY_ROUTING but nothing read them -- the model scored these criteria
blind to control type."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import criteria_tables as ct
from api.icp import llm_interpreter as li
from api.icp.models import (
    CrmStructured,
    EntityResolution,
    EvidenceBundle,
    OwnershipControl,
    PractusHistory,
    ReachoutRow,
    UnstructuredEvidenceItem,
)


def _bundle(control: OwnershipControl) -> EvidenceBundle:
    return EvidenceBundle(
        entity=EntityResolution(
            company_name_queried="Test Co",
            contracting_entity_name="Test Co Limited",
            entity_resolved=True,
            ownership_control=control,
        ),
        crm=CrmStructured(deal_id=1, deal_name="Test Deal", stage="Qualified Prospect"),
        practus_history=PractusHistory(is_practus_client=False),
    )


def test_criterion_block_a2_includes_routing_for_control_type():
    block_cp = li._criterion_block("A2", OwnershipControl.CP)
    assert ct.A2_LIQUIDITY_ROUTING[OwnershipControl.CP] in block_cp

    block_wh = li._criterion_block("A2", OwnershipControl.WH)
    assert ct.A2_LIQUIDITY_ROUTING[OwnershipControl.WH] in block_wh
    assert ct.A2_LIQUIDITY_ROUTING[OwnershipControl.CP] not in block_wh


def test_criterion_block_b1_includes_routing_for_control_type():
    block_sc = li._criterion_block("B1", OwnershipControl.SC)
    assert ct.B1_TRIGGER_ROUTING[OwnershipControl.SC] in block_sc

    block_pf = li._criterion_block("B1", OwnershipControl.PF)
    assert ct.B1_TRIGGER_ROUTING[OwnershipControl.PF] in block_pf
    assert ct.B1_TRIGGER_ROUTING[OwnershipControl.SC] not in block_pf


def test_criterion_block_c1_includes_routing_for_control_type():
    block_pf = li._criterion_block("C1", OwnershipControl.PF)
    assert ct.C1_AUTHORITY_ROUTING[OwnershipControl.PF] in block_pf

    block_sc = li._criterion_block("C1", OwnershipControl.SC)
    assert ct.C1_AUTHORITY_ROUTING[OwnershipControl.SC] in block_sc
    assert ct.C1_AUTHORITY_ROUTING[OwnershipControl.PF] not in block_sc


def test_criterion_block_c1_tells_the_model_to_check_secondary_research_when_no_contacts_logged():
    """Real bug (Livpure Smart Homes, 2026-09-09, a no-CRM manual entry):
    C1 scored "nobody identified" (firing Gate 2, capping at Nurture)
    despite the SAME report elsewhere naming the company's MD from
    secondary research -- icp-skill.md's own C1 rule requires checking
    "the company leadership page and the individual's LinkedIn for title",
    not just the Reachout_tracker, which is empty by construction for any
    no-CRM/manual entry."""
    block = li._criterion_block("C1", OwnershipControl.PF)

    assert "secondary-research evidence" in block.lower()
    assert "information_gatherer_only_or_nobody_identified" in block


def test_criterion_block_a3_prose_differs_by_control_type():
    block_cp = li._criterion_block("A3", OwnershipControl.CP)
    block_sc = li._criterion_block("A3", OwnershipControl.SC)

    assert ct.A3_MEANINGFUL_MARGIN_PROSE[OwnershipControl.CP] in block_cp
    assert ct.A3_MEANINGFUL_MARGIN_PROSE[OwnershipControl.SC] in block_sc
    # Prose is control-specific, not shared boilerplate.
    assert ct.A3_MEANINGFUL_MARGIN_PROSE[OwnershipControl.CP] != ct.A3_MEANINGFUL_MARGIN_PROSE[OwnershipControl.SC]
    assert ct.A3_MEANINGFUL_MARGIN_PROSE[OwnershipControl.SC] not in block_cp


def test_criterion_block_unaffected_criteria_have_no_routing_line():
    # B3 has no *_ROUTING table -- block should be just the label list.
    block = li._criterion_block("B3", OwnershipControl.PF)
    assert "Routing for" not in block


def test_build_prompt_includes_cp_specific_routing_text():
    bundle = _bundle(OwnershipControl.CP)
    prompt = li.build_prompt(bundle, OwnershipControl.CP)

    assert ct.A2_LIQUIDITY_ROUTING[OwnershipControl.CP] in prompt
    assert ct.B1_TRIGGER_ROUTING[OwnershipControl.CP] in prompt
    assert ct.C1_AUTHORITY_ROUTING[OwnershipControl.CP] in prompt
    assert ct.A3_MEANINGFUL_MARGIN_PROSE[OwnershipControl.CP] in prompt

    # PF-specific text should not have leaked in.
    assert ct.A2_LIQUIDITY_ROUTING[OwnershipControl.PF] not in prompt
    assert ct.C1_AUTHORITY_ROUTING[OwnershipControl.PF] not in prompt


def test_build_prompt_includes_pf_specific_routing_text():
    bundle = _bundle(OwnershipControl.PF)
    prompt = li.build_prompt(bundle, OwnershipControl.PF)

    assert ct.A2_LIQUIDITY_ROUTING[OwnershipControl.PF] in prompt
    assert ct.B1_TRIGGER_ROUTING[OwnershipControl.PF] in prompt
    assert ct.C1_AUTHORITY_ROUTING[OwnershipControl.PF] in prompt
    assert ct.A3_MEANINGFUL_MARGIN_PROSE[OwnershipControl.PF] in prompt

    # CP-specific text should not have leaked in.
    assert ct.A2_LIQUIDITY_ROUTING[OwnershipControl.CP] not in prompt
    assert ct.C1_AUTHORITY_ROUTING[OwnershipControl.CP] not in prompt


def test_build_prompt_includes_reachout_tracker_contacts_for_c1():
    """Previously C1 (which drives Gate 2) never saw the Reachout_tracker at
    all -- the model scored authority with zero visibility into who was
    actually contacted. Confirmed live as the likely cause of Gate 2
    misfiring."""
    bundle = _bundle(OwnershipControl.PF)
    bundle.crm.designation = "Manager"  # deal-level field, BD-entered
    bundle.crm.reachout_tracker = [
        ReachoutRow(
            reachout_date="2026-07-01", client_contact_name="Yogesh Bansal",
            designation="CFO", contact_role="Sponsor", email="yogesh@test.com",
            reachout_medium="Teams", remarks="Discussed scope",
        )
    ]

    prompt = li.build_prompt(bundle, OwnershipControl.PF)

    assert "Yogesh Bansal" in prompt
    assert "CFO" in prompt
    assert "Sponsor" in prompt
    assert "prefer the tracker" in prompt.lower() or "prefer the contacts logged" in prompt.lower()


def test_build_prompt_includes_p1_industry_confirmed_client_count():
    """Previously P1 (industry experience) had zero real evidence --
    nothing counted confirmed Practus clients in the target's industry, so
    it silently defaulted almost every run."""
    bundle = _bundle(OwnershipControl.PF)
    bundle.practus_history = PractusHistory(
        is_practus_client=False,
        industry_confirmed_client_names=["Alpha Retail Ltd", "Beta Stores Ltd"],
    )

    prompt = li.build_prompt(bundle, OwnershipControl.PF)

    assert "Alpha Retail Ltd" in prompt
    assert "Beta Stores Ltd" in prompt
    assert "2 confirmed" in prompt


def test_build_prompt_falls_back_to_keyword_matched_clients_when_no_exact_match():
    """Live-confirmed real bug (Uniparts India, 2026-09-09): this evidence
    block only ever read industry_confirmed_client_names (the CRM-exact-
    match field), while narrative/practus_section.py's cred_cards prompt
    already had a fallback to keyword_matched_client_names (the real-
    sector proxy match used when the CRM's own industry field is generic).
    A real run scored P1 as "zero_confirmed" while its own narrative
    section, in the same report, listed 21 named confirmed clients found
    via this exact proxy-match field -- a direct self-contradiction in the
    finished report. Scoring must see the same clients the narrative does."""
    bundle = _bundle(OwnershipControl.PF)
    bundle.practus_history = PractusHistory(
        is_practus_client=False,
        industry_confirmed_client_names=[],
        keyword_matched_industry="Auto Component/ Ancilliary",
        keyword_matched_client_names=["RSB Transmission", "Rockman Automation"],
    )

    prompt = li.build_prompt(bundle, OwnershipControl.PF)

    assert "RSB Transmission" in prompt
    assert "Rockman Automation" in prompt
    assert "2 confirmed" in prompt
    assert "Auto Component/ Ancilliary" in prompt
    assert "proxy match" in prompt.lower()


def test_build_prompt_instructs_empty_label_not_lowest_label_when_silent():
    """The old instruction told the model to actively pick the LOWEST label
    when evidence was silent -- overriding every criterion's own correct
    'if silent' default (which only applies when the label is empty).
    Confirmed live as silently defeating A1/A2/A3/B1/C3's silent defaults."""
    bundle = _bundle(OwnershipControl.PF)
    prompt = li.build_prompt(bundle, OwnershipControl.PF)

    assert "empty string" in prompt.lower()
    assert "lowest option" not in prompt.lower()


def test_build_prompt_warns_against_ticket_size_as_a1_a3_evidence():
    bundle = _bundle(OwnershipControl.PF)
    prompt = li.build_prompt(bundle, OwnershipControl.PF)

    assert "never use the deal ticket size" in prompt.lower()


def test_criterion_block_p2_instructs_priority_order_and_no_invented_impact():
    block = li._criterion_block("P2", OwnershipControl.PF)

    assert "p2" in block.lower()
    assert "invent" in block.lower()


def test_criterion_block_p3_instructs_full_match_test_and_uplift_cap():
    block = li._criterion_block("P3", OwnershipControl.PF)

    assert "industry match and service-line match and credible named clients" in block.lower()
    assert "capped at 5" in block.lower()
    assert "at most once" in block.lower()


def test_build_prompt_surfaces_criterion_tags_on_evidence_lines():
    """Previously the model saw only `[setu/Setu] <text>` for a P2/P3 Setu
    answer with no indication which criterion it was gathered for, even
    though evidence_assembler.py carefully tags every item -- it had to
    infer from prose alone."""
    bundle = _bundle(OwnershipControl.PF)
    bundle.unstructured.append(
        UnstructuredEvidenceItem(text="A named case study.", source="Setu", source_type="setu", criterion_tags=["P2"])
    )

    prompt = li.build_prompt(bundle, OwnershipControl.PF)

    assert "tagged for P2" in prompt


def test_build_prompt_flags_a_closed_deal_problem_statement_as_historical():
    """Scenario 36: "Problem statement present on a CLOSED deal ->
    historical context only, never the live problem." """
    bundle = _bundle(OwnershipControl.PF)
    bundle.crm.stage = "Client Won"
    bundle.crm.client_problem_statement = "Cost visibility and control"

    prompt = li.build_prompt(bundle, OwnershipControl.PF)

    assert "CLOSED" in prompt
    assert "historical context" in prompt.lower()


def test_build_prompt_does_not_flag_a_live_deal_problem_statement():
    bundle = _bundle(OwnershipControl.PF)
    bundle.crm.stage = "Qualified Prospect"
    bundle.crm.client_problem_statement = "Cost visibility and control"

    prompt = li.build_prompt(bundle, OwnershipControl.PF)

    assert "historical context" not in prompt.lower()


def test_criterion_block_a2_states_mode_a_for_qp_plus_stage():
    block = li._criterion_block("A2", OwnershipControl.PF, stage="Qualified Prospect")

    assert "MODE A" in block
    assert "compute and state BOTH" in block


def test_criterion_block_a2_states_mode_b_for_early_stage():
    block = li._criterion_block("A2", OwnershipControl.PF, stage="Prospect")

    assert "MODE B" in block
    assert "NO real ticket yet" in block


def test_criterion_block_p4_instructs_named_referrer_counts_as_champion():
    block = li._criterion_block("P4", OwnershipControl.PF)

    assert "named referrer" in block.lower()
    assert "champion" in block.lower()
