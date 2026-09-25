# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/anthropic_client.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Thin wrapper around the Anthropic Messages API for the three LLM-touched
stages: scoring interpretation, secondary research (web search), and
narrative generation. Model: claude-sonnet-5 (not the stale
claude-sonnet-4-5 alias). Adaptive thinking is Sonnet 5's default when the
`thinking` param is omitted, so it's left unset here rather than configured
explicitly.
"""

from __future__ import annotations

import json
import logging
import re
import time
from functools import lru_cache
from typing import Any

import anthropic

from api.icp.compat import get_settings
from . import tracing
from .models import CriterionInterpretation, EvidenceLabel, LlmInterpretationResult
from .persona import load_persona, load_skill_reference

logger = logging.getLogger(__name__)


class StreamTimeoutError(RuntimeError):
    """A streaming call kept receiving individual bytes/events (so no
    single httpx read-timeout ever tripped) but ran past its overall
    wall-clock budget regardless. Confirmed live: a real research() call
    was still going with zero errors and zero indication of whether it was
    alive or truly stuck — a per-read timeout alone doesn't bound a stream
    that trickles data indefinitely; this does."""


def log_stream_progress(stream, label: str, *, max_seconds: float | None = None) -> None:
    """Live-confirmed real gap: a streaming call previously logged nothing
    between the initial request and the final assembled response — for a
    real multi-round web search (this exact call has genuinely taken 20+
    minutes and pulled dozens of source URLs on a well-documented public
    company), that made a slow-but-alive call indistinguishable from an
    actual hang from the outside, with nothing in the logs to tell the
    difference. Iterating the stream's events (instead of only calling
    `stream.get_final_message()` directly) still assembles the same final
    message underneath — this just also logs each event as it arrives, so
    real-time pacing is visible. Best-effort: unrecognized event shapes are
    silently skipped rather than raised, since this is a logging aid, not
    load-bearing behavior.

    `max_seconds`, when given, is a hard wall-clock ceiling independent of
    any per-read httpx timeout — a stream that keeps receiving occasional
    bytes/events never trips a per-read timeout no matter how long the
    total call runs, so this is the only thing that actually bounds worst-
    case duration for a pathologically slow-but-technically-alive stream."""
    start = time.monotonic()
    for event in stream:
        if max_seconds is not None and (elapsed := time.monotonic() - start) > max_seconds:
            raise StreamTimeoutError(
                f"[{label}] stream still running after {elapsed:.0f}s (budget {max_seconds:.0f}s) — aborting."
            )
        try:
            if event.type == "content_block_start":
                block = event.content_block
                block_type = getattr(block, "type", None)
                if block_type == "server_tool_use" and getattr(block, "name", None) == "web_search":
                    logger.info("[%s] web_search round started (block %d)", label, event.index)
                elif block_type == "text":
                    logger.info("[%s] model started writing its text response (block %d)", label, event.index)
            elif event.type == "content_block_stop":
                logger.info("[%s] block %d finished", label, event.index)
        except Exception:
            continue


# Sonnet 5 first-party rates (per token, i.e. list-price-per-MTok / 1e6).
# Cache write/read multipliers are Anthropic's standard ephemeral (5-minute
# TTL) rates, not specific to this account -- treat the logged dollar
# figure as a real-time estimate to catch regressions/surprises, not as a
# substitute for the actual Console/Admin API bill if exact numbers matter.
_INPUT_PRICE_PER_MTOK = 2.00
_OUTPUT_PRICE_PER_MTOK = 10.00
_CACHE_WRITE_PRICE_PER_MTOK = _INPUT_PRICE_PER_MTOK * 1.25
_CACHE_READ_PRICE_PER_MTOK = _INPUT_PRICE_PER_MTOK * 0.10


def log_usage(
    label: str, usage, *, model: str | None = None, input: Any = None, output: Any = None,
) -> None:
    """Every real Anthropic call this client makes now logs its actual
    billed token counts -- confirmed live this was previously completely
    invisible (no call site anywhere read `.usage`), which is exactly why
    an earlier back-of-envelope cost-per-run estimate had no real numbers
    to check itself against. `output_tokens` is the billed total and
    already INCLUDES adaptive thinking's token spend (Sonnet 5 runs
    thinking by default whenever the `thinking` param is omitted -- see
    this module's own top docstring) -- `usage.output_tokens_details.
    thinking_tokens`, logged separately below, is a breakdown of how much
    of that total was invisible reasoning versus actual visible output, not
    an additional charge on top of `output_tokens`.

    `model`/`input`/`output`, when given, also send this same call to
    Langfuse (icp/tracing.py) -- optional because a couple of call sites
    (the truncated first attempt inside _stream_structured_json's retry
    loop) only have `usage` to report at that point. Silent no-op when
    Langfuse isn't configured, same as every other tracing call site."""
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    details = getattr(usage, "output_tokens_details", None)
    thinking_tokens = getattr(details, "thinking_tokens", None) or 0
    cost = (
        input_tokens * _INPUT_PRICE_PER_MTOK
        + output_tokens * _OUTPUT_PRICE_PER_MTOK
        + cache_write * _CACHE_WRITE_PRICE_PER_MTOK
        + cache_read * _CACHE_READ_PRICE_PER_MTOK
    ) / 1_000_000
    logger.info(
        "[usage] %s: input=%d output=%d (of which thinking=%d) cache_write=%d cache_read=%d "
        "(~$%.4f, approx Sonnet 5 list rates)",
        label, input_tokens, output_tokens, thinking_tokens, cache_write, cache_read, cost,
    )
    if model is not None:
        tracing.record_generation(
            label, input=input, output=output, model=model, input_tokens=input_tokens, output_tokens=output_tokens,
        )


class TruncatedResponseError(RuntimeError):
    """A structured-output call hit `max_tokens` before finishing its JSON.
    Confirmed live: a `brief_section.py` call cut off mid-string, and the
    resulting `json.loads()` failure ("Unterminated string...") gave no
    hint of the actual cause. Adaptive thinking can consume a meaningful
    chunk of the `max_tokens` budget before any visible output, so this
    isn't just "raise max_tokens once and forget it" -- it needs a clear,
    specific error so a future occurrence isn't debugged from scratch."""


_LITERAL_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def fix_literal_unicode_escapes(value: Any) -> Any:
    """Real bug (Sobha, 2026-09-11, live-confirmed intermittent across
    several earlier runs too — not something introduced by any recent
    change): the model occasionally double-escapes a non-ASCII character
    inside its own JSON string output — writing the literal JSON text
    `\\\\u20b9` (an escaped backslash followed by the literal characters
    "u20b9") instead of either a real UTF-8 ₹ character or a single
    `\\u20b9` escape. `json.loads()` (and the SDK's own tool-use input
    parsing) treats this as perfectly valid JSON — a string that happens
    to CONTAIN a literal backslash — so it never fails or even warns; the
    rendered report just shows the raw escape-sequence TEXT ("\\u20b9659
    Cr", "\\u2014") instead of the intended character (₹, —). Recursively
    walks an already-parsed JSON value and repairs this exact pattern.
    Safe by construction: a genuine, intentional "\\uXXXX" text sequence
    is not realistic content anywhere in this pipeline's financial/
    consulting prose, so there's no real string this could wrongly alter."""
    if isinstance(value, str):
        if "\\u" not in value:
            return value
        return _LITERAL_UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
    if isinstance(value, list):
        return [fix_literal_unicode_escapes(v) for v in value]
    if isinstance(value, dict):
        return {k: fix_literal_unicode_escapes(v) for k, v in value.items()}
    return value


def _extract_json(response, *, max_tokens: int) -> dict:
    if response.stop_reason == "max_tokens":
        raise TruncatedResponseError(
            f"Response hit max_tokens={max_tokens} before finishing its JSON output — "
            "raise this call's max_tokens."
        )
    text = next(b.text for b in response.content if b.type == "text")
    return fix_literal_unicode_escapes(json.loads(text))


def _stream_structured_json(
    client: anthropic.Anthropic, *, model: str, max_tokens: int, system: list[dict] | None,
    messages: list[dict], schema: dict, label: str,
) -> dict:
    """Runs one `output_config: json_schema`-constrained streamed call and
    parses its JSON, with ONE automatic retry at 1.5x max_tokens if the
    first attempt hits `max_tokens` before finishing.

    Live-confirmed (2026-09-01 spike, this model): Anthropic's assistant-
    message "prefill" continuation trick — append the truncated partial
    text as a trailing assistant-role message and ask the model to keep
    writing from exactly that point, salvaging the tokens already spent —
    is NOT available here. The API rejects it outright regardless of
    json_schema mode: "This model does not support assistant message
    prefill. The conversation must end with a user message." A full retry
    at a larger budget is therefore the only available recovery (the same
    "raise max_tokens and try again" every truncation on this project has
    been fixed with so far, just automated instead of requiring a human to
    notice a live failure first) — the partial text is still logged in
    full so a truncation remains diagnosable from the server log even
    though it can't be carried forward token-for-token."""
    tokens = max_tokens
    response = None
    for attempt in range(2):
        # Span opened AROUND the call, not after it, so Langfuse records the
        # real in-flight duration -- see tracing.observe_generation().
        with tracing.observe_generation(
            f"{label} (attempt {attempt + 1}/2)", model=model, input=messages
        ) as finish, client.messages.stream(
            model=model,
            max_tokens=tokens,
            **({"system": system} if system is not None else {}),
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
            finish(
                output=next((b.text for b in response.content if b.type == "text"), ""),
                usage=response.usage,
            )
        partial_text = next((b.text for b in response.content if b.type == "text"), "")
        # Logged even for a truncated attempt that gets thrown away below --
        # a wasted attempt is still fully billed, and that cost is exactly
        # the kind of thing this logging exists to surface (see
        # generate_persona_report()'s near-identical retry, which was
        # silently eating a full wasted call on every contact-included run
        # before its own default max_tokens was raised).
        # Console line only -- `model=` is deliberately NOT passed, because
        # the surrounding observe_generation() already sent this call to
        # Langfuse WITH its real duration and cache-token counts. Passing it
        # here too would double-record every generation as a second,
        # zero-latency trace.
        log_usage(f"{label} (attempt {attempt + 1}/2)", response.usage)
        if response.stop_reason != "max_tokens" or attempt == 1:
            break
        retry_tokens = int(tokens * 1.5)
        logger.warning(
            "%s hit max_tokens=%d — retrying once at max_tokens=%d instead of failing the call outright. "
            "Partial output produced before the retry (%d chars, logged here since it can't be carried "
            "into the retry — see _stream_structured_json's docstring): %s",
            label, tokens, retry_tokens, len(partial_text), partial_text,
        )
        tokens = retry_tokens
    return _extract_json(response, max_tokens=tokens)

INTERPRETATION_SCHEMA = {
    "type": "object",
    "properties": {
        "interpretations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "string"},
                    "condition_label": {
                        "type": "string",
                        "description": "Must be one of the allowed labels given for this criterion in the prompt — copy it verbatim.",
                    },
                    "supporting_facts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim": {"type": "string"},
                                "label": {"type": "string", "enum": ["Fact", "Inference", "Assumption", "Data gap"]},
                                "source": {"type": ["string", "null"]},
                                "date": {"type": ["string", "null"]},
                            },
                            "required": ["claim", "label"],
                            "additionalProperties": False,
                        },
                    },
                    "evidence_label_overall": {"type": "string", "enum": ["Fact", "Inference", "Assumption", "Data gap"]},
                    "rationale": {"type": "string"},
                    "data_gap": {"type": "boolean"},
                },
                "required": ["criterion_id", "condition_label", "supporting_facts", "evidence_label_overall", "rationale", "data_gap"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["interpretations"],
    "additionalProperties": False,
}


# The SDK's own default read timeout is 600s (confirmed: anthropic 0.122.0,
# `anthropic.Anthropic(...).timeout`) — with up to 2 retries on top of that,
# a single stalled call (a real network stall was reproduced live: one
# ownership_classifier.classify() call hung past 90s with zero response,
# another identical call succeeded in 41s) could block a whole pipeline run
# for the better part of half an hour with no way to tell "still working"
# from "will never return." Every call from this client goes through here,
# so this one number bounds all of them. 150s comfortably covers the
# slowest legitimate web-search call observed (~41s) with headroom for a
# multi-round search, while still failing in a couple of minutes instead of
# ten-plus — a timeout surfaces as a normal exception, which every caller
# already either catches (evidence_assembler.py's connector calls degrade
# to a data gap) or lets propagate to orchestrator.py's background-task
# wrapper, which turns it into a clean "Failed" status instead of an
# invisible hang.
_REQUEST_TIMEOUT_SECONDS = 150.0

# Originally tuned for this module's own web-search-enabled calls (which
# could run several search rounds within a single turn, a fundamentally
# different shape than the single-shot interpretation/narrative calls
# _REQUEST_TIMEOUT_SECONDS above was tuned for). `ownership_classifier.classify()`
# and `secondary_research.extract_revenue_figure()`/`identify_competitors()`
# have since migrated off Anthropic's own web_search tool onto Exa (see
# exa_search.py) and no longer call this client at all -- research() (this
# module's own free-text synthesis call, now tools-free but still
# comparatively slow/large) remains the one real user of the longer timeout.
_RESEARCH_TIMEOUT_SECONDS = 280.0


@lru_cache
def get_client() -> anthropic.Anthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured — set it in .env.")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=_REQUEST_TIMEOUT_SECONDS)


def get_research_client() -> anthropic.Anthropic:
    """Same client, longer timeout -- see _RESEARCH_TIMEOUT_SECONDS above.
    Use this instead of get_client() for research()."""
    return get_client().with_options(timeout=_RESEARCH_TIMEOUT_SECONDS)


def _cached_system_blocks(extra: str = "", *, include_skill_reference: bool = True) -> list[dict]:
    """Persona (+ the full skill text, unless the caller opts out) first, then
    per-run instructions after the cache breakpoint.

    `include_skill_reference=False` drops the 808-line scoring rubric, leaving
    only the firm-identity persona. Six call sites take that option: the three
    Setu rerankers and the three client-name cleanups. None of them scores
    anything -- they rank relevance or tidy a list of names, from prompts that
    are entirely self-contained -- so the rubric was pure ballast.

    Why that matters more than it looks: prompt caching is keyed on the WHOLE
    request prefix INCLUDING the json_schema, which is different for every one
    of this pipeline's calls. Measured directly against the live API:

        call 1, schema A -> cache_creation 30481, cache_read 0
        call 2, schema A -> cache_creation 0,     cache_read 30481   (hit)
        call 3, schema B -> cache_creation 30481, cache_read 0       (miss)
        call 4, schema A -> cache_creation 0,     cache_read 30481   (hit)

    So caching works fine per schema -- but since every call in a run uses its
    own schema, every call is a first-of-its-kind WRITE at 1.25x and nothing
    is ever read back at 0.10x. A real run (20260911-083448-manappuram) shows
    342,705 cache-creation tokens and exactly 0 cache-read tokens: ~$0.86 of a
    $1.91 run spent re-writing the same text twelve times. That was invisible
    until tracing started forwarding the cache counters, so it had been
    happening on every run since caching was introduced.

    The schema cannot be lifted out of the cache key, so the lever that IS
    available is not sending the expensive block to calls that never use it."""
    skill_text = (
        f"\n\nFULL SCORING RUBRIC (source of truth for every table and rule):\n\n{load_skill_reference()}"
        if include_skill_reference else ""
    )
    blocks = [
        {
            "type": "text",
            "text": f"PERSONA AND FIRM IDENTITY:\n\n{load_persona()}{skill_text}",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if extra:
        blocks.append({"type": "text", "text": extra})
    return blocks


def interpret_criteria(prompt: str) -> LlmInterpretationResult:
    """One call per company. `prompt` is built by llm_interpreter.py from the
    assembled EvidenceBundle plus the per-criterion allowed condition_label
    enums — see criteria_tables.allowed_labels(). The model may only choose
    from those labels; scorer.py does the label -> score lookup, never this
    call's own arithmetic.

    Streamed -- same "long request" infra cutoff research() hit; see that
    function's docstring. Confirmed live on this call specifically too
    (a large cached system prompt + a now-larger per-run prompt, after
    adding the Reachout_tracker/P1-evidence/mode-statement content), so the
    same fix applies here, not just to the web-search calls."""
    client = get_client()
    settings = get_settings()
    # Raised 16000->24000: confirmed live this still truncated after the
    # prompt grew (Reachout_tracker/P1-evidence/A2-mode/P4-champion/
    # closed-deal additions) -- 12 criteria's worth of supporting_facts +
    # rationale, plus adaptive thinking overhead, needs real headroom.
    max_tokens = 24000
    data = _stream_structured_json(
        client,
        model=settings.anthropic_model_interpretation,
        max_tokens=max_tokens,
        system=_cached_system_blocks(),
        messages=[{"role": "user", "content": prompt}],
        schema=INTERPRETATION_SCHEMA,
        label="interpret_criteria()",
    )
    interpretations = [
        CriterionInterpretation(
            criterion_id=i["criterion_id"],
            condition_label=i["condition_label"],
            supporting_facts=i.get("supporting_facts", []),
            evidence_label_overall=EvidenceLabel(i.get("evidence_label_overall", "Data gap")),
            rationale=i.get("rationale", ""),
            data_gap=i.get("data_gap", False),
        )
        for i in data["interpretations"]
    ]
    return LlmInterpretationResult(interpretations=interpretations)


def research(prompt: str) -> str:
    """Secondary-research SYNTHESIS call — plain, tools-free text
    completion. Live evidence-gathering itself now happens beforehand via
    `exa_search.search_many()` (see evidence_assembler.gather_secondary_research(),
    which builds `prompt` with that evidence already embedded) rather than
    Anthropic's own agentic web_search tool — that migration is what
    actually fixed this call's own multi-minute cost (see the plan's "run
    time" investigation); this function itself only ever had to write up
    evidence already in the prompt, never fetch anything itself.

    Still streamed: the prompt can be large (a full Exa evidence block
    appended) and the response budget is generous (up to 24000 tokens), so
    this can still take a real amount of wall-clock time to generate even
    with zero tool round-trips."""
    client = get_research_client()
    settings = get_settings()
    # Raised 8000->16000->24000: confirmed live -- once streaming fixed an
    # earlier infra-level "long request" cutoff (from back when this call
    # ran Anthropic's own agentic web_search tool), a real heavily-covered
    # company's worth of findings genuinely needed more than 8000 tokens to
    # write up. Kept at 24000 post-migration since the synthesis task
    # itself (write up everything in a large evidence block) is unchanged.
    max_tokens = 24000
    with tracing.observe_generation(
        "research()", model=settings.anthropic_model_research, input=prompt
    ) as finish, client.messages.stream(
        model=settings.anthropic_model_research,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        # 300s (5 min): this call no longer waits on any web-search round-
        # trip (that's all done beforehand via Exa), so its worst-case
        # duration is now bounded by generation speed alone -- a much
        # smaller ceiling than the 1200s this needed back when it ran its
        # own multi-round agentic search internally.
        log_stream_progress(stream, "research()", max_seconds=300.0)
        response = stream.get_final_message()
        text = "\n\n".join(b.text for b in response.content if b.type == "text")
        finish(output=text, usage=response.usage)
    log_usage("research()", response.usage)
    if response.stop_reason == "max_tokens":
        # Unlike interpret_criteria()/generate_structured_narrative()'s
        # structured JSON output (where a truncated response is genuinely
        # unusable — you can't json.loads() half a document), this call
        # returns free-text prose: confirmed live, a real truncated run had
        # already written real synthesized findings (the model was mid-
        # sentence in its own answer, not empty) after 8 minutes and 57
        # real sources visited — raising unconditionally here discarded all
        # of that paid-for, already-found evidence instead of using
        # whatever prose it did manage to write. Salvage it, flagged as
        # incomplete, rather than lose it outright; only raise when there's
        # truly nothing to salvage.
        if text:
            logger.warning(
                "research() hit max_tokens=%d — returning the %d-char partial synthesis instead of "
                "discarding it (real search work already happened; see the search trace above).",
                max_tokens, len(text),
            )
            return text + (
                "\n\n[NOTE: this research was cut off by a token limit before the model finished writing "
                "— treat anything not explicitly stated above as unconfirmed, not as a negative finding.]"
            )
        raise TruncatedResponseError(
            f"research() hit max_tokens={max_tokens} with no usable text produced at all — raise this call's max_tokens."
        )
    return text


def generate_structured_narrative(
    prompt: str, schema: dict, *, max_tokens: int = 8000, label: str = "generate_structured_narrative()",
    include_skill_reference: bool = True,
) -> dict:
    """Same call as `generate_narrative`, but forced into `schema` — used
    by `narrative_generator.py` to get render_models-shaped JSON directly
    instead of parsing free text. No web-search tool: this stage only
    writes up evidence already gathered by evidence_assembler.py, it
    doesn't research anything new.

    Streamed -- same "long request" infra cutoff research()/interpret_criteria()
    hit; see research()'s docstring. Automatically retries once at 1.5x
    max_tokens on truncation instead of always raising — see
    _stream_structured_json()'s docstring for why that's a full retry
    rather than a salvage-and-continue.

    `label` names the CALL SITE, and every caller should pass its own. This
    function has ten of them (five narrative sections, three Setu rerankers,
    two client-name cleanups), and until now they all reported to Langfuse
    under this function's own name: 76 of the 100 generations recorded
    across 8 real runs were the single string
    "generate_structured_narrative() (attempt 1/2)", which made per-section
    cost impossible to read off the dashboard -- attributing the $4.46 those
    76 calls cost meant counting positions within a session by hand. The
    default is kept only so an un-updated caller still works."""
    client = get_client()
    settings = get_settings()
    return _stream_structured_json(
        client,
        model=settings.anthropic_model_narrative,
        max_tokens=max_tokens,
        system=_cached_system_blocks(include_skill_reference=include_skill_reference),
        messages=[{"role": "user", "content": prompt}],
        schema=schema,
        label=label,
    )
