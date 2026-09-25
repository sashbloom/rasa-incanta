# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/persona.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Loads the two static reference docs the pipeline needs at runtime:
Who am I.md (persona/firm-identity) and icp-skill.md (the rubric itself,
kept as the source of truth for tables — see criteria_tables.py). Both are
read once and meant to sit at the front of the LLM system prompt, ahead of
per-run evidence, so they're eligible for prompt caching (icp-skill.md is
~54KB / well over the 512-token minimum on claude-sonnet-5)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from api.icp.compat import get_settings


def _read(path_str: str) -> str:
    # Resolved relative to the process's working directory, same as the
    # `.env` file itself (SettingsConfigDict(env_file=".env")) — i.e.
    # `backend/`, so the default "../icp-skill.md" lands on the icp-bot root.
    return Path(path_str).read_text(encoding="utf-8")


@lru_cache
def load_persona() -> str:
    return _read(get_settings().icp_persona_file)


@lru_cache
def load_skill_reference() -> str:
    return _read(get_settings().icp_skill_file)
