# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/models.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Pydantic models for the ICP pipeline.

Three layers, kept structurally separate end to end:

1. `EvidenceBundle` — everything gathered before any judgment is applied.
   `crm_structured` and `secondary_research` are separate fields on purpose:
   the skill (icp-skill.md, EVIDENCE DISCIPLINE) bans CRM financial fields
   (`Amount`, `revenue_parameter_last_fy`, ...) from ever feeding A1/A2/A3 —
   keeping them in different Pydantic models makes that structurally true
   rather than a prompt instruction that can be forgotten.
2. `CriterionInterpretation` — the LLM's ONLY allowed output shape per
   criterion: a condition_label copied from the skill's own table rows,
   plus cited supporting facts. Never a raw score — `scorer.py` derives the
   score from the label via `criteria_tables.py`, so arithmetic is never
   trusted to the model.
3. `ScoreResult` — the fully computed, code-owned output: subtotals, gates,
   verdicts, recommendation. Pure function of (1) and (2).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class OwnershipControl(StrEnum):
    PF = "PF"  # promoter / family
    SC = "SC"  # sponsor controlled
    SI = "SI"  # sponsor influenced
    WH = "WH"  # widely held
    CP = "CP"  # corporate parent
    JC = "JC"  # jointly controlled
    ST = "ST"  # state controlled
    NP = "NP"  # non-profit


class EvidenceLabel(StrEnum):
    FACT = "Fact"
    INFERENCE = "Inference"
    ASSUMPTION = "Assumption"
    DATA_GAP = "Data gap"


class Stage(StrEnum):
    POTENTIAL = "Potential"
    NEED_IDENTIFICATION = "Need Identification"
    WALKTHROUGH_SCHEDULED = "Walkthrough Scheduled"
    QUALIFIED_PROSPECT = "Qualified Prospect"
    PROPOSAL_SENT = "Proposal Sent"
    NEGOTIATED_PROPOSAL_SENT = "Negotiated Proposal Sent"
    CLIENT_WON = "Client Won"
    CLIENT_LOST = "Client Lost"


STAGES_WITH_REAL_TICKET = {
    Stage.QUALIFIED_PROSPECT,
    Stage.PROPOSAL_SENT,
    Stage.NEGOTIATED_PROPOSAL_SENT,
}

# icp-skill.md scenario 36: "Problem statement present on a CLOSED deal ->
# historical context only, never the live problem." Shared so every caller
# (llm_interpreter's prompt, conflict_checker's active-pursuit check) uses
# the same definition of "closed" rather than each hand-rolling it.
CLOSED_STAGES = {Stage.CLIENT_WON, Stage.CLIENT_LOST}


# ---------------------------------------------------------------------------
# 1. Evidence bundle
# ---------------------------------------------------------------------------


class DuplicateAccount(BaseModel):
    account_id: int
    account_name: str
    modified_time: str | None = None
    # True if this candidate was name-exact to the query or corroborated by
    # a matching contact email domain (icp-skill.md §0.1 step 4) -- False
    # means it only surfaced via fuzzy name similarity and was NOT pooled
    # into matched_account_ids/matched_deal_ids for scoring. See
    # entity_resolver.py: a merely-similar-sounding but unrelated company
    # (e.g. "Trident Limited" for a "Trent" query) must never have its
    # deals silently mixed into another company's evidence.
    confirmed: bool = False


class EntityResolution(BaseModel):
    """Output of Identity Resolution (icp-skill.md §0.1-0.3)."""

    company_name_queried: str
    matched_account_ids: list[int] = Field(default_factory=list)
    matched_deal_ids: list[int] = Field(default_factory=list)
    duplicate_accounts: list[DuplicateAccount] = Field(default_factory=list)
    contracting_entity_name: str | None = None
    contracting_entity_note: str | None = None
    entity_resolved: bool = False
    ownership_control: OwnershipControl | None = None
    ownership_disclosure: str | None = None  # "Listed" | "Unlisted"
    ownership_management: str | None = None  # "Owner-managed" | "Professionally managed"
    ownership_confidence: EvidenceLabel = EvidenceLabel.DATA_GAP
    ipo_track: bool = False


class ManualCompanyInfo(BaseModel):
    """Optional manual override for a company with zero Zoho footprint. Only
    ever consulted as a fallback — see evidence_assembler.assemble(): the
    real entity_resolver.resolve() lookup always runs first, and a real
    Zoho record always wins over these fields, so a manual entry can never
    silently mask a findable CRM record. Deliberately a small subset of
    CrmStructured's fields — only what a hands-on PM would plausibly know
    off-hand for a brand-new prospect (see CrmStructured's own docstring for
    why reachout rows, stage history, and financial fields are excluded)."""

    legal_name: str | None = None  # only if different from the company name searched
    # A user often won't know the exact legal entity name for a brand-new
    # prospect, but usually knows its website -- a much stronger
    # disambiguator for the web-search-based calls below (there's no
    # guarantee a manually-typed name isn't shared with an unrelated
    # company). See evidence_assembler.assemble()'s use of this alongside
    # entity.contracting_entity_note.
    website: str | None = None
    stage: str | None = None  # one of Stage's values, or left unset (a2_mode() defaults safely)
    industry: str | None = None
    problem_statement: str | None = None
    ep_name: str | None = None

    def has_content(self) -> bool:
        return any([self.legal_name, self.website, self.stage, self.industry, self.problem_statement, self.ep_name])


class ReachoutRow(BaseModel):
    reachout_date: str | None = None
    client_contact_name: str | None = None
    designation: str | None = None
    contact_role: str | None = None
    email: str | None = None
    reachout_medium: str | None = None
    remarks: str | None = None


class DealHistoryRow(BaseModel):
    stage: str
    modified_time: str


class CrmStructured(BaseModel):
    """Fields pulled directly from the zoho_data Postgres tables. Financial
    fields are retained ONLY as pursuit context (icp-skill.md line 516) —
    `evidence_assembler.py` must never copy these into anything A1/A2/A3
    reads; that ban is enforced by never wiring this model into the
    financial-interpretation prompt section, not by convention alone."""

    deal_id: int | None = None
    deal_name: str | None = None
    stage: str | None = None
    stage_modified_time: str | None = None
    created_time: str | None = None
    closing_date: str | None = None
    ep_involved: str | None = None
    el_involved: str | None = None
    # Deal-level Designation (icp-skill.md C1: "never from the Zoho
    # Designation field alone" -- kept so it can be compared against, and
    # explicitly deprioritized versus, reachout_tracker's per-contact
    # designation, never used on its own to classify authority).
    designation: str | None = None
    problem_area_1: str | None = None
    problem_area_2: str | None = None
    client_problem_statement: str | None = None
    specify_reference: str | None = None
    potential_lead_source: str | None = None
    reason_for_loss: str | None = None
    industry_type: str | None = None
    reachout_tracker: list[ReachoutRow] = Field(default_factory=list)
    stage_history: list[DealHistoryRow] = Field(default_factory=list)

    # Context-only financials (banned from A1/A2/A3 — see class docstring).
    amount_context_only: float | None = None
    mrr_context_only: float | None = None


class PractusHistory(BaseModel):
    is_practus_client: bool = False
    source: str | None = None  # "zoho_client_won" | "client_names_file" | "both"
    client_lost_count: int = 0  # all-time total, all deals -- narrative context only, not a gate input
    # Gate 3 input (icp-skill.md line 430, scenario 16/17): count of
    # `Client Lost` deals within the last 24 months that have no reason_for_loss
    # / specify_reason_for_lost captured. Counted per-loss, not all-or-nothing
    # across the whole history -- see practus_history.classify()'s docstring.
    client_lost_unexplained_count: int = 0
    # P1 (icp-skill.md STEP 3): confirmed Practus clients (Zoho Client Won +
    # Client Names file) in the target's own industry, across the whole
    # client base -- see practus_history.confirmed_clients_in_industry().
    industry_confirmed_client_names: list[str] = Field(default_factory=list)
    # Same two sources, NOT scoped to the target industry -- {industry:
    # count} across the whole confirmed-client base, for honest adjacent-
    # industry narrative context when the exact industry has zero confirmed
    # clients. See practus_history.confirmed_client_industry_breakdown().
    all_confirmed_clients_by_industry: dict[str, int] = Field(default_factory=dict)
    # Populated only when the exact-industry match above is empty AND a
    # real sector proxy was found via secondary-research keywords (e.g. a
    # wine company's own CRM industry field is generic, but its real
    # sector, 'FMCG', matches a real confirmed client's industry tag) --
    # see practus_history.confirmed_clients_by_keyword_context(). Kept
    # separate from industry_confirmed_client_names so the narrative can
    # honestly label this as a real-sector proxy match, not the CRM's own
    # (generic) industry field.
    keyword_matched_industry: str | None = None
    keyword_matched_client_names: list[str] = Field(default_factory=list)


class UnstructuredEvidenceItem(BaseModel):
    """One piece of free text evidence: a Setu chat answer, a secondary
    research finding, a meeting transcript excerpt, an Outlook thread, etc.
    These are what the LLM Interpretation stage actually reads."""

    text: str
    source: str
    source_type: str  # "setu" | "secondary_research" | "meeting" | "outlook"
    date: str | None = None
    criterion_tags: list[str] = Field(default_factory=list)


class SecondaryFinancials(BaseModel):
    """Revenue/EBITDA/post-money-cash, from secondary research ONLY (see
    CrmStructured's docstring — CRM's own financial fields never reach
    here). This is the real number rule_precompute.ticket_ratios() and the
    §0.5 scale-mismatch flag need; the free-text secondary-research finding
    in `unstructured` is prose the narrative quotes, not something a rule
    can divide by."""

    revenue_cr: float | None = None
    ebitda_cr: float | None = None
    post_money_cash_cr: float | None = None  # SI-only A1 uplift input
    source: str | None = None
    date: str | None = None


class EvidenceBundle(BaseModel):
    entity: EntityResolution
    crm: CrmStructured
    practus_history: PractusHistory
    secondary_financials: SecondaryFinancials = Field(default_factory=SecondaryFinancials)
    unstructured: list[UnstructuredEvidenceItem] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 2. LLM interpretation output
# ---------------------------------------------------------------------------


class SupportingFact(BaseModel):
    claim: str
    label: EvidenceLabel
    source: str | None = None
    date: str | None = None


class CriterionInterpretation(BaseModel):
    criterion_id: str  # "A1" | "A2" | ... | "P4" | "gate_1" | "gate_4"
    condition_label: str  # closed enum key from criteria_tables.py — validated by scorer.py
    supporting_facts: list[SupportingFact] = Field(default_factory=list)
    evidence_label_overall: EvidenceLabel = EvidenceLabel.DATA_GAP
    rationale: str = ""
    data_gap: bool = False


class LlmInterpretationResult(BaseModel):
    interpretations: list[CriterionInterpretation]


# ---------------------------------------------------------------------------
# 3. Scored output
# ---------------------------------------------------------------------------


class CriterionScore(BaseModel):
    criterion_id: str
    raw_score: int | None = None  # 1-5, or None if N/A
    is_na: bool = False
    weight: float
    weighted_contribution: float
    condition_label: str | None = None
    rationale: str = ""
    evidence_label: EvidenceLabel = EvidenceLabel.DATA_GAP
    data_gap: bool = False


class GroupScore(BaseModel):
    name: str  # "Ability to Pay" | "Willingness to Pay" | "Access" | ...
    criteria: list[CriterionScore]
    subtotal: int
    max_points: int
    sub_verdict: str  # "Strong" | "Moderate" | "Weak"
    renormalized: bool = False


class GateResult(BaseModel):
    gate_id: str
    name: str
    fired: bool
    detail: str = ""


class ScoreResult(BaseModel):
    client_groups: list[GroupScore]  # Ability, Willingness, Access
    practus_criteria: list[CriterionScore]  # P1-P4
    client_total: int
    client_verdict: str  # Strong | Moderate | Weak
    practus_total: int
    practus_verdict: str
    gates: list[GateResult]
    recommendation: str  # Pursue aggressively | Pursue selectively | Nurture | Park
    recommendation_reason: str = ""
    provisional: bool = False  # Gate 7 fired — suppress totals, show band only
    scale_mismatch_flag: bool = False
