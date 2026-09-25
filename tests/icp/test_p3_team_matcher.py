# Copied from icp-bot/backend/tests/test_p3_team_matcher.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import p3_team_matcher as tm


def _roster(*people):
    return list(people)


def _person(name, grade, status="Active", employee_code=None):
    return {"name": name, "grade_label": grade, "status": status, "employee_code": employee_code}


def test_extract_query_keywords_ignores_generic_prose_but_keeps_proper_nouns():
    """Live-confirmed real gap: once the query text grew from a short deal
    name into full secondary-research prose (thousands of characters of
    financial reporting language), plain word-tokenization drowned a
    genuinely distinctive hit under 30-40 incidental matches on generic
    finance/accounting vocabulary shared by every candidate's resume (every
    EP/EL/TL here is a finance professional). Extracting capitalized
    phrases first is a much cleaner signal -- company/peer names are
    reliably capitalized even mid-sentence, generic reporting prose isn't."""
    text = (
        "Sula Wines reported consolidated revenue of Rs 596 crore, down 3.75% year on year. "
        "Its listed peers include United Spirits and Radico Khaitan. The company disclosed "
        "compliance filings and quarterly performance figures to the exchange."
    )

    keywords = tm._extract_query_keywords(text)

    assert "sula" in keywords
    assert "wine" in keywords
    assert "spirit" in keywords or "spirits" in keywords
    assert "radico" in keywords
    # Generic reporting prose, lowercase mid-sentence, must NOT show up.
    assert "revenue" not in keywords
    assert "compliance" not in keywords
    assert "quarterly" not in keywords
    assert "performance" not in keywords


def test_normalize_label_strips_invisible_chars_and_trailing_s():
    assert tm.normalize_label("Metals") == "metal"
    assert tm.normalize_label("Metal") == "metal"
    assert tm.normalize_label("Glass​") == "glass"


def test_resolve_industry_alias_maps_crm_fmcg_to_setu_consumer_goods():
    """Confirmed-with-user mapping ported from the sibling ep-el-exception-
    report project, which resolves this exact CRM/Setu taxonomy mismatch
    against the same Setu roster."""
    assert tm._resolve_industry_alias("FMCG") == "Consumer Goods"
    assert tm._resolve_industry_alias("Real Estate") == "Real Estate"  # no alias needed


def test_find_team_matches_scores_industry_match_via_alias():
    with patch.object(tm.setu_db, "fetch_ep_el_tl_roster", return_value=_roster(_person("Ameya Waingankar", "EP"))), \
         patch.object(tm.setu_db, "fetch_skill_profiles", return_value={
             "ameya waingankar": {"industries": ["Consumer Goods", "Real Estate"], "service_lines": []}
         }), \
         patch.object(tm.setu_db, "fetch_resume_text", return_value={}), \
         patch.object(tm.setu_db, "fetch_named_clients", return_value={}):
        matches = tm.find_team_matches(industry="FMCG", service_line=None, keyword_context="Some Co")

    assert len(matches) == 1
    assert matches[0]["name"] == "Ameya Waingankar"
    assert matches[0]["industry_match"] is True


def test_find_team_matches_finds_resume_keyword_hit_for_prior_career():
    """Live-confirmed real case: a resume mentioning "Wine-yard" experience
    should surface as a match for a wine-company deal even with no
    documented skill_profile industry match at all."""
    with patch.object(tm.setu_db, "fetch_ep_el_tl_roster", return_value=_roster(_person("Fenil Bhimani", "EL"))), \
         patch.object(tm.setu_db, "fetch_skill_profiles", return_value={}), \
         patch.object(tm.setu_db, "fetch_resume_text", return_value={
             "fenil bhimani": "Have consultancy experience in Manufacturing, Hospitality, Wine-yard and Pharmacy chain."
         }), \
         patch.object(tm.setu_db, "fetch_named_clients", return_value={}):
        matches = tm.find_team_matches(industry=None, service_line=None, keyword_context="Sula Wines - Growth & Expansion")

    assert len(matches) == 1
    assert matches[0]["name"] == "Fenil Bhimani"
    assert "wine" in matches[0]["keyword_hits"] or "winery" in " ".join(matches[0]["keyword_hits"])
    assert "Wine-yard" in matches[0]["resume_snippet"]


def test_find_team_matches_ranks_a_rare_keyword_above_a_shared_common_one():
    """Live-confirmed real case: after tightening query-keyword extraction
    to multi-word capitalized phrases, a FRESH secondary-research run still
    introduced its own new incidental shared word ("department") that tied
    every single candidate at the same flat score -- hand-tuning stopwords
    per run doesn't scale. A person matching only a genuinely rare word
    ("wine", found in nobody else's resume) must outrank someone whose only
    hit is a word several other resumes also happen to share ("department"),
    without needing that specific word blocklisted."""
    with patch.object(tm.setu_db, "fetch_ep_el_tl_roster", return_value=_roster(
             _person("Fenil Bhimani", "EL"),
             _person("Common Word Person", "EP"),
             _person("Another Common Match", "TL"),
             _person("Third Common Match", "EP"),
         )), \
         patch.object(tm.setu_db, "fetch_skill_profiles", return_value={}), \
         patch.object(tm.setu_db, "fetch_resume_text", return_value={
             "fenil bhimani": "Have consultancy experience in Manufacturing, Hospitality, Wine-yard and Pharmacy chain.",
             "common word person": "Worked closely with the finance department on reporting.",
             "another common match": "Led the audit department across three regions.",
             "third common match": "Coordinated with the department heads on budgeting.",
         }), \
         patch.object(tm.setu_db, "fetch_named_clients", return_value={}):
        matches = tm.find_team_matches(
            industry=None, service_line=None,
            keyword_context="Sula Wines - Growth & Expansion mentions the Finance Department too", limit=4,
        )

    assert matches[0]["name"] == "Fenil Bhimani"
    assert matches[0]["score"] > matches[1]["score"]


def test_find_team_matches_excludes_people_with_zero_score():
    with patch.object(tm.setu_db, "fetch_ep_el_tl_roster", return_value=_roster(_person("Irrelevant Person", "EP"))), \
         patch.object(tm.setu_db, "fetch_skill_profiles", return_value={
             "irrelevant person": {"industries": ["Automobile"], "service_lines": ["Others"]}
         }), \
         patch.object(tm.setu_db, "fetch_resume_text", return_value={}), \
         patch.object(tm.setu_db, "fetch_named_clients", return_value={}):
        matches = tm.find_team_matches(industry="Real Estate", service_line=None, keyword_context="Sula Wines")

    assert matches == []


def test_find_team_matches_prefers_active_over_inactive_when_both_score():
    with patch.object(tm.setu_db, "fetch_ep_el_tl_roster", return_value=_roster(
             _person("Active Person", "EP", status="Active"),
             _person("Inactive Person", "EP", status="Inactive"),
         )), \
         patch.object(tm.setu_db, "fetch_skill_profiles", return_value={
             "active person": {"industries": ["Real Estate"], "service_lines": []},
             "inactive person": {"industries": ["Real Estate"], "service_lines": []},
         }), \
         patch.object(tm.setu_db, "fetch_resume_text", return_value={}), \
         patch.object(tm.setu_db, "fetch_named_clients", return_value={}):
        matches = tm.find_team_matches(industry="Real Estate", service_line=None, keyword_context="Sobha")

    names = [m["name"] for m in matches]
    assert "Active Person" in names
    assert "Inactive Person" not in names


def test_find_team_matches_falls_back_to_inactive_when_no_active_person_scores():
    with patch.object(tm.setu_db, "fetch_ep_el_tl_roster", return_value=_roster(
             _person("Active No Match", "EP", status="Active"),
             _person("Inactive Match", "EP", status="Inactive"),
         )), \
         patch.object(tm.setu_db, "fetch_skill_profiles", return_value={
             "active no match": {"industries": ["Automobile"], "service_lines": []},
             "inactive match": {"industries": ["Real Estate"], "service_lines": []},
         }), \
         patch.object(tm.setu_db, "fetch_resume_text", return_value={}), \
         patch.object(tm.setu_db, "fetch_named_clients", return_value={}):
        matches = tm.find_team_matches(industry="Real Estate", service_line=None, keyword_context="Sobha")

    assert len(matches) == 1
    assert matches[0]["name"] == "Inactive Match"


def test_find_team_matches_includes_named_clients_from_staffing_history():
    with patch.object(tm.setu_db, "fetch_ep_el_tl_roster", return_value=_roster(
             _person("Ameya Waingankar", "EP", employee_code="E001")
         )), \
         patch.object(tm.setu_db, "fetch_skill_profiles", return_value={
             "ameya waingankar": {"industries": ["Real Estate"], "service_lines": []}
         }), \
         patch.object(tm.setu_db, "fetch_resume_text", return_value={}), \
         patch.object(tm.setu_db, "fetch_named_clients", return_value={"E001": ["Casa Grande", "Vianaar"]}):
        matches = tm.find_team_matches(industry="Real Estate", service_line=None, keyword_context="Sobha")

    assert matches[0]["named_clients"] == ["Casa Grande", "Vianaar"]


def test_format_matches_as_evidence_text_handles_empty_list_honestly():
    text = tm.format_matches_as_evidence_text([], industry="Real Estate", service_line=None)

    assert "genuine zero-match" in text.lower()
    assert "not a search failure" in text.lower()


def test_find_assigned_person_profile_returns_none_for_blank_name():
    assert tm.find_assigned_person_profile(None) is None
    assert tm.find_assigned_person_profile("   ") is None


def test_find_assigned_person_profile_returns_none_when_not_in_roster():
    with patch.object(tm.setu_db, "fetch_ep_el_tl_roster", return_value=_roster(_person("Someone Else", "EP"))):
        assert tm.find_assigned_person_profile("Shashank Silhare") is None


def test_find_assigned_person_profile_finds_a_real_record_even_with_no_matching_industry():
    """Real bug (Manappuram Finance, 2026-09-09): a documented EP with
    real industries/a real resume on file should never come back as "not
    found" just because none of it happens to match this specific deal."""
    with patch.object(tm.setu_db, "fetch_ep_el_tl_roster",
                       return_value=_roster(_person("Shashank Silhare", "EP", employee_code="E1"))), \
         patch.object(tm.setu_db, "fetch_skill_profiles", return_value={
             "shashank silhare": {"industries": ["Pharmaceuticals", "Metals"], "service_lines": ["Operations Transformation"]},
         }), \
         patch.object(tm.setu_db, "fetch_resume_text", return_value={"shashank silhare": "Led a pharma BPR engagement."}), \
         patch.object(tm.setu_db, "fetch_named_clients", return_value={"E1": ["Some Pharma Co"]}):
        profile = tm.find_assigned_person_profile("Shashank Silhare")

    assert profile["name"] == "Shashank Silhare"
    assert profile["grade"] == "EP"
    assert profile["industries"] == ["Pharmaceuticals", "Metals"]
    assert profile["service_lines"] == ["Operations Transformation"]
    assert profile["named_clients"] == ["Some Pharma Co"]
    assert profile["has_resume_on_file"] is True


def test_format_matches_as_evidence_text_always_describes_an_assigned_person_even_with_zero_matches():
    """This is the exact fix for the false claim: a report must never say
    "no record found" for someone the direct by-name lookup DOES have a
    profile for, even when the fuzzy search found nobody at all."""
    assigned = [{
        "name": "Shashank Silhare", "grade": "EP", "status": "Active",
        "industries": ["Pharmaceuticals", "Metals"], "service_lines": ["Operations Transformation"],
        "named_clients": [], "has_resume_on_file": True,
    }]

    text = tm.format_matches_as_evidence_text([], industry="BFSI", service_line=None, assigned_profiles=assigned)

    assert "genuine zero-match" not in text.lower()
    assert "Shashank Silhare" in text
    assert "Pharmaceuticals" in text
    assert "already assigned to this deal" in text.lower()


def test_format_matches_as_evidence_text_notes_when_assigned_person_has_no_documentation_at_all():
    assigned = [{
        "name": "Nobody Documented", "grade": "EL", "status": "Active",
        "industries": [], "service_lines": [], "named_clients": [], "has_resume_on_file": False,
    }]

    text = tm.format_matches_as_evidence_text([], industry="BFSI", service_line=None, assigned_profiles=assigned)

    assert "no resume, skill profile, or staffing history is on file" in text.lower()


def test_format_matches_as_evidence_text_does_not_duplicate_an_assigned_person_already_in_matches():
    matches = [{
        "name": "Shashank Silhare", "grade": "EP", "status": "Active",
        "industries": ["BFSI"], "service_lines": [], "named_clients": [],
        "industry_match": True, "service_line_match": False,
        "keyword_hits": [], "resume_snippet": "", "score": 2,
    }]
    assigned = [{
        "name": "Shashank Silhare", "grade": "EP", "status": "Active",
        "industries": ["BFSI"], "service_lines": [], "named_clients": [], "has_resume_on_file": True,
    }]

    text = tm.format_matches_as_evidence_text(matches, industry="BFSI", service_line=None, assigned_profiles=assigned)

    assert text.count("Shashank Silhare") == 1


def test_format_matches_as_evidence_text_includes_named_clients_and_resume_snippet():
    matches = [{
        "name": "Fenil Bhimani", "grade": "EL", "status": "Active",
        "industries": [], "service_lines": [], "named_clients": ["Integrated Spaces"],
        "industry_match": False, "service_line_match": False,
        "keyword_hits": ["winery"], "resume_snippet": "Wine-yard and Pharmacy chain.",
        "score": 2,
    }]

    text = tm.format_matches_as_evidence_text(matches, industry=None, service_line=None)

    assert "Fenil Bhimani" in text
    assert "Integrated Spaces" in text
    assert "Wine-yard" in text


def test_format_matches_as_evidence_text_includes_llm_rationale_when_present():
    matches = [{
        "name": "Fenil Bhimani", "grade": "EL", "status": "Active",
        "industries": [], "service_lines": [], "named_clients": [],
        "industry_match": False, "service_line_match": False,
        "keyword_hits": ["wine"], "resume_snippet": "Wine-yard experience.",
        "score": 1.0, "llm_rationale": "Directly relevant prior-career winery experience.",
    }]

    text = tm.format_matches_as_evidence_text(matches, industry=None, service_line=None)

    assert "Directly relevant prior-career winery experience." in text


def _match(name, **overrides):
    base = {
        "name": name, "grade": "EP", "status": "Active", "industries": [], "service_lines": [],
        "named_clients": [], "industry_match": False, "service_line_match": False,
        "keyword_hits": [], "resume_snippet": "", "score": 1.0,
    }
    base.update(overrides)
    return base


def test_rerank_matches_with_llm_returns_empty_immediately_for_empty_input():
    """No candidates means no LLM call is even worth making."""
    with patch("api.icp.anthropic_client.generate_structured_narrative") as mock_generate:
        result = tm.rerank_matches_with_llm([], keyword_context="anything", industry=None, service_line=None)

    assert result == []
    mock_generate.assert_not_called()


def test_rerank_matches_with_llm_selects_and_reorders_per_the_llm_response():
    matches = [_match("Coincidental Match", score=1.5), _match("Fenil Bhimani", keyword_hits=["wine"], score=1.0)]

    with patch(
        "api.icp.anthropic_client.generate_structured_narrative",
        return_value={"selected": [
            {"name": "Fenil Bhimani", "rationale": "Genuine prior-career winery experience."},
        ]},
    ):
        result = tm.rerank_matches_with_llm(matches, keyword_context="Sula Wines", industry=None, service_line=None)

    assert len(result) == 1
    assert result[0]["name"] == "Fenil Bhimani"
    assert result[0]["llm_rationale"] == "Genuine prior-career winery experience."


def test_rerank_matches_with_llm_falls_back_to_deterministic_ranking_on_failure():
    matches = [_match("Fenil Bhimani", score=1.0)]

    with patch(
        "api.icp.anthropic_client.generate_structured_narrative",
        side_effect=RuntimeError("truncated"),
    ):
        result = tm.rerank_matches_with_llm(matches, keyword_context="Sula Wines", industry=None, service_line=None)

    assert result == matches[:4]


def test_rerank_matches_with_llm_falls_back_when_it_hallucinates_a_name_not_in_the_list():
    """Never trust a name the LLM invents -- only names that are actually in
    the candidate list may be returned."""
    matches = [_match("Fenil Bhimani", score=1.0)]

    with patch(
        "api.icp.anthropic_client.generate_structured_narrative",
        return_value={"selected": [{"name": "Someone Not In The List", "rationale": "made up"}]},
    ):
        result = tm.rerank_matches_with_llm(matches, keyword_context="Sula Wines", industry=None, service_line=None)

    assert result == matches[:4]
