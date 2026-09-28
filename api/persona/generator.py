# Adapted from icp-bot/backend/domains/mahak/people/mahak/persona/generator.py (read-only reference repo).
"""Persona deep-dive for one contact: public source material from Exa, then one structured Claude
call that fills the ICP bot's 8-section persona schema (schema.py, system_prompt.py, both copied).

What Rasa Incanta changes from the ICP bot:
- The contact comes from the deal's Zoho outreach log (name and designation), never from anywhere
  else, so the persona is always about someone the deal team has actually met.
- Source material is Exa search (api/icp/exa_search.search_many) only. The ICP bot's three-step
  source (an Apify LinkedIn scrape, a hard-coded profile store, then Anthropic's own web_search
  tool) is not ported: no LinkedIn scraping, no stored personal profiles, no web_search calls.
- No Exa material means no Claude call: the caller records `no_material` instead of paying for a
  report that could only say "gap" in every section.
The Claude call itself (forced tool use, one retry at 1.5x max_tokens on truncation, Langfuse span
around it) is the ICP bot's, unchanged.
"""
from __future__ import annotations

import logging

from api.icp import exa_search, tracing
from api.icp.anthropic_client import fix_literal_unicode_escapes, get_client, log_usage
from api.persona.schema import PERSONA_REPORT_SCHEMA
from api.persona.system_prompt import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_PERSONA_TOOL = {
    "name": "emit_persona_report",
    "description": (
        "Emit the complete structured persona 1-pager as JSON, following the Practus persona-analyst "
        "methodology exactly. Every bullet must be labelled observed, inferred, or gap."
    ),
    "input_schema": PERSONA_REPORT_SCHEMA,
}

RESULTS_PER_QUERY = 5


class NoSourceMaterial(RuntimeError):
    """Exa found nothing about this person: there is nothing to base a persona on."""


def exa_queries(full_name: str, organization: str, designation: str | None) -> list[str]:
    """Targeted queries for one person at one company. LinkedIn pages can come back as ordinary
    search results; nothing is scraped."""
    queries = [f'"{full_name}" {organization}', f"{full_name} {organization} profile career background"]
    if designation:
        queries.insert(1, f'"{full_name}" {designation} {organization}')
    queries.append(f'"{full_name}" {organization} interview OR article OR speaker OR podcast')
    return queries


def gather_from_exa(full_name: str, organization: str, designation: str | None) -> str:
    return exa_search.search_many(exa_queries(full_name, organization, designation),
                                  num_results_per_query=RESULTS_PER_QUERY)


def _generate_structured_report(client, *, model: str, system: list[dict], messages: list[dict],
                                max_tokens: int = 8000) -> dict:
    """The ICP bot's forced tool-use call, unchanged (its docstring explains why tool use rather
    than json_schema: this schema's grammar is too large for strict mode)."""
    tokens = max_tokens
    response = None
    for attempt in range(2):
        label = f"persona report (attempt {attempt + 1}/2)"
        with tracing.observe_generation(label, model=model, input=messages, tags=["persona"]) as finish, \
                client.messages.stream(
                    model=model,
                    max_tokens=tokens,
                    system=system,
                    tools=[_PERSONA_TOOL],
                    tool_choice={"type": "tool", "name": "emit_persona_report"},
                    messages=messages,
                ) as stream:
            response = stream.get_final_message()
            tool_use = next((b for b in response.content if b.type == "tool_use" and b.name == "emit_persona_report"), None)
            finish(output=tool_use.input if tool_use is not None else None, usage=response.usage)
        log_usage(label, response.usage)
        if response.stop_reason != "max_tokens" or attempt == 1:
            break
        logger.warning("Persona report hit max_tokens=%d; retrying once at %d.", tokens, int(tokens * 1.5))
        tokens = int(tokens * 1.5)

    tool_use = next((b for b in response.content if b.type == "tool_use" and b.name == "emit_persona_report"), None)
    if tool_use is None:
        raise RuntimeError("The model did not return a structured persona report.")
    return fix_literal_unicode_escapes(tool_use.input)


def generate_persona_data(full_name: str, organization: str, *, designation: str | None, model: str,
                          known_context: str | None = None, source_material: str | None = None) -> dict:
    """The structured persona dict (PERSONA_REPORT_SCHEMA). Raises NoSourceMaterial when Exa finds
    nothing, and lets Exa or Claude failures propagate for the caller to record."""
    material = source_material if source_material is not None else gather_from_exa(full_name, organization, designation)
    if not material.strip():
        raise NoSourceMaterial(f"No public material found for {full_name} at {organization}.")

    known = []
    if designation:
        known.append(f"The Zoho outreach log records this person as: {designation}.")
    if known_context and known_context.strip():
        known.append(known_context.strip())
    known_block = (
        "\n\nALREADY CONFIRMED BY INTERNAL RESEARCH (this deal's own records, separate from the web "
        "source material above; use it to corroborate or resolve role and seniority for this SAME person):"
        f'\n"""\n{chr(10).join(known)}\n"""' if known else ""
    )
    user_content = (
        f"TARGET INDIVIDUAL\nFull Name: {full_name}\nCurrent Organization: {organization}\n\n"
        f'SOURCE MATERIAL (web search results from Exa):\n"""\n{material}\n"""'
        f"{known_block}\n\n"
        "Call the emit_persona_report tool with the complete 8-section structure."
    )
    return _generate_structured_report(
        get_client(), model=model,
        # Tools render before system, so this one breakpoint caches the persona schema and prompt
        # together: identical for every contact (Rasa Incanta addition; the ICP bot did not cache it).
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
    )
