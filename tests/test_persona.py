"""Persona research on the person in each deal's outreach log: who is picked, what goes on the card,
the 8-week cache, and the Exa-only generator. Fakes only; no Exa or Claude call is made."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from api.db import get_sessionmaker
from api.engine import persona_signal
from api.engine.persona_signal import Contact, facts_from, primary_contact
from api.models import ContactPersona, ContextCard, Deal, Recommendation
from api.persona import generator
from api.persona.generator import NoSourceMaterial
from api.sources.zoho import Reachout
from tests.fakes import NOW, FakeClaude, claude_response, load, settings
from tests.test_run import zoho_fetch

NORTHWIND = "598723000011234001"


def touch(person, on, role="Customer", designation="CFO"):
    return Reachout(deal_zoho_id="d", on=on, person=person, role=role, designation=designation)


def persona(role="Chief Financial Officer, Northwind Foods"):
    return {
        "person": {"name": "Priya Shah", "roleTitle": role},
        "section1": {"factsLeft": [{"label": "observed", "text": "Joined Northwind Foods as CFO in 2022 (priya@northwindfoods.example)."},
                                   {"label": "inferred", "text": "Likely to favour phased programmes."}],
                     "factsRight": [{"label": "observed", "text": "Spoke on working capital at a 2025 CFO summit."},
                                    {"label": "observed", "text": "A third observed fact."}], "timeline": []},
        "section4": {"interestAreas": [], "hooks": [{"title": "CFO summit", "text": "Her 2025 talk on receivables."}]},
        "section8": {"overall": "Medium", "bars": [], "notes": []},
    }


# ---------------------------------------------------------------- who is researched

def test_the_contact_is_the_latest_customer_in_the_outreach_log():
    log = [touch("Arjun Menon", date(2026, 9, 10), role="Influencer"), touch("Priya Shah", date(2026, 9, 1)),
           touch("Old Customer", date(2026, 6, 1))]
    assert primary_contact(log, "Northwind Foods") == Contact("Priya Shah", "CFO", "Northwind Foods")
    only_influencer = [touch("Arjun Menon", date(2026, 9, 10), role="Influencer", designation=None)]
    assert primary_contact(only_influencer, "Blue Harbour").name == "Arjun Menon"


def test_no_name_no_research():
    junk = [touch("priya@northwindfoods.example", date(2026, 9, 1)), touch("NA", date(2026, 9, 2)),
            touch("", date(2026, 9, 3)), touch("+91 98765 43210", date(2026, 9, 4))]
    assert primary_contact(junk, "Northwind Foods") is None  # never an email or phone number sent to Exa
    assert primary_contact([touch("Priya Shah", date(2026, 9, 1))], "") is None
    assert primary_contact([], "Northwind Foods") is None


def test_the_cache_key_ignores_case_punctuation_and_company_suffixes():
    assert Contact("Priya  Shah", None, "Northwind Foods Pvt. Ltd.").key == Contact("priya shah", "CFO", "Northwind Foods").key


# ---------------------------------------------------------------- what goes on the card

def test_only_observed_material_becomes_facts_and_contact_details_are_scrubbed():
    facts = facts_from(persona(), "Priya Shah", NOW)
    values = {f["id"]: f["value"] for f in facts}
    assert values["persona.role"] == "Priya Shah: Chief Financial Officer, Northwind Foods (research confidence: Medium)"
    assert values["persona.fact_1"] == "Joined Northwind Foods as CFO in 2022 ([email removed])."
    assert values["persona.fact_2"] == "Spoke on working capital at a 2025 CFO summit."
    assert "persona.fact_3" not in values  # at most two observed facts
    assert values["persona.hook"] == "Conversation hook: CFO summit: Her 2025 talk on receivables."
    assert not any("phased" in v for v in values.values())  # inferred bullets are not facts
    assert {f["source"] for f in facts} == {"persona"} and facts[0]["date"] == NOW.date().isoformat()


def test_no_observed_material_means_no_hook():
    thin = persona()
    thin["section1"] = {"factsLeft": [{"label": "gap", "text": "Nothing public."}], "factsRight": []}
    assert [f["id"] for f in facts_from(thin, "Priya Shah", NOW)] == ["persona.role"]


# ---------------------------------------------------------------- the run and the 8-week cache

class Research:
    def __init__(self, outcome=None):
        self.calls, self.outcome = [], outcome

    def __call__(self, name, company, *, designation, model, known_context=None):
        self.calls.append({"name": name, "company": company, "designation": designation, "known": known_context})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return persona() if name == "Priya Shah" else persona(role="Head of Procurement, Blue Harbour")


def run(session, research, now=NOW, reply_ids=("zoho.stage",)):
    from api.engine.run import run_week

    reply = claude_response(dict(load("claude/nba_reply.json"), evidence_ids=list(reply_ids)))
    return run_week(session, settings(), now=now, fetch=zoho_fetch(), llm_client=FakeClaude(reply),
                    deal_zoho_id=NORTHWIND, persona_fn=research)


def test_a_run_researches_each_logged_contact_and_cites_it(migrated):
    research = Research()
    with get_sessionmaker()() as s:
        r = run(s, research, reply_ids=("zoho.stage", "persona.role"))
        assert r.status == "succeeded", r.stats
        assert sorted(c["name"] for c in research.calls) == ["Arjun Menon", "Priya Shah"]
        priya = next(c for c in research.calls if c["name"] == "Priya Shah")
        assert priya["designation"] == "Chief Financial Officer" and "@" not in str(research.calls)
        deal = s.scalar(select(Deal).where(Deal.zoho_id == NORTHWIND))
        card = s.scalar(select(ContextCard).where(ContextCard.deal_id == deal.id))
        assert card.stakeholder["persona_contact"] == "Priya Shah"
        assert "persona.role" in [f["id"] for f in card.stakeholder["facts"]]
        assert s.scalar(select(Recommendation)).evidence[1]["source"] == "persona"
        assert r.stats["sources"]["persona"] == "ok (2 researched, 0 cached)" and r.stats["persona_to_do"] == 2


def test_research_is_reused_for_eight_weeks_then_redone(migrated):
    research = Research()
    with get_sessionmaker()() as s:
        run(s, research)
        second = run(s, research, now=NOW + timedelta(days=55))
        assert len(research.calls) == 2 and second.stats["sources"]["persona"] == "ok (all cached)"
        run(s, research, now=NOW + timedelta(days=57))
        assert len(research.calls) == 4  # older than 56 days: researched again, as a new row
        assert len(s.scalars(select(ContactPersona)).all()) == 4


def test_nothing_found_is_remembered_but_a_failure_is_retried(migrated):
    with get_sessionmaker()() as s:
        empty = Research(NoSourceMaterial("nothing"))
        r = run(s, empty)
        assert r.stats["persona_failed"] == 0
        run(s, empty)
        assert len(empty.calls) == 2  # no_material is cached like a result: not searched again
        card = s.scalars(select(ContextCard)).first()
        assert "persona_contact" not in card.stakeholder

    with get_sessionmaker()() as s:
        for row in s.scalars(select(ContactPersona)):
            s.delete(row)
        s.commit()
        broken = Research(RuntimeError("Exa returned 500"))
        r = run(s, broken)
        assert r.status == "succeeded"  # one failing source never fails the run
        assert r.stats["sources"]["persona"].startswith("2 contacts could not be researched")
        run(s, broken)
        assert len(broken.calls) == 4  # failures are never reused


def test_without_exa_no_contact_is_researched_and_it_is_said(migrated):
    from api.engine.run import run_week

    with get_sessionmaker()() as s:
        r = run_week(s, settings(), now=NOW, fetch=zoho_fetch())
        assert r.stats["sources"]["persona"] == "skipped: EXA_API_KEY is not set"
        assert s.scalar(select(ContactPersona)) is None


# ---------------------------------------------------------------- the generator: Exa only

def test_no_exa_material_means_no_claude_call(monkeypatch):
    monkeypatch.setattr(generator.exa_search, "search_many", lambda queries, **kw: "")
    monkeypatch.setattr(generator, "get_client", lambda: pytest.fail("Claude must not be called"))
    with pytest.raises(NoSourceMaterial):
        generator.generate_persona_data("Priya Shah", "Northwind Foods", designation="CFO", model="m")


def test_exa_queries_name_the_person_and_company_only():
    queries = generator.exa_queries("Priya Shah", "Northwind Foods", "Chief Financial Officer")
    assert all("Priya Shah" in q and "Northwind Foods" in q for q in queries)
    assert not any("linkedin" in q.lower() for q in queries)  # no LinkedIn scraping: plain search only


class Stream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def test_the_report_call_is_grounded_in_exa_material_and_its_prompt_is_cached(monkeypatch):
    requests = []
    tool_use = SimpleNamespace(type="tool_use", name="emit_persona_report", input=persona())
    usage = SimpleNamespace(input_tokens=10, output_tokens=10, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    message = SimpleNamespace(content=[tool_use], stop_reason="tool_use", usage=usage)
    client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: requests.append(kw) or Stream(message)))
    monkeypatch.setattr(generator.exa_search, "search_many",
                        lambda queries, **kw: "- Priya Shah, CFO — https://example.com/priya — excerpt: CFO since 2022")
    monkeypatch.setattr(generator, "get_client", lambda: client)

    data = generator.generate_persona_data("Priya Shah", "Northwind Foods", designation="CFO", model="claude-sonnet-5",
                                           known_context="Authority: sponsor identified. CFO sponsors it.")
    assert data["person"]["name"] == "Priya Shah"
    [request] = requests
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}  # schema + prompt cached across contacts
    assert "tools" in request and "web_search" not in str(request["tools"])  # no Claude web search
    sent = request["messages"][0]["content"]
    assert "CFO since 2022" in sent and "records this person as: CFO" in sent and "CFO sponsors it." in sent
