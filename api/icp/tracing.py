# Copied from icp-bot/backend/domains/mahak/people/mahak/icp/tracing.py (read-only reference repo).
# Only imports and plumbing are adapted for Rasa Incanta; the logic is the ICP bot's.
"""Langfuse tracing for every real Anthropic call this pipeline makes —
the Practus multi-agent platform's shared Langfuse project (see
`langfuse_guide.md`'s org-wide convention: identical credentials across
every agent, separated by tag/session in *code*, not by per-agent keys).

Adapted from mono-repo's own `domains/pratham/qc/tracing.py`, but only its
non-LangChain `start_batch_trace()`/`record_generation()` half — this app
calls the Anthropic SDK directly with no LangChain/LangGraph anywhere, so
there's no `.ainvoke()`/`.astream_events()` config dict to attach a
CallbackHandler to (that's what the platform guide's own `add_tracing()`/
`classification_callbacks()` pattern is for, and it doesn't apply here).

One full ICP run makes many separate Anthropic calls, several of them
inside their own `ThreadPoolExecutor` worker threads (see
evidence_assembler.py's concurrent research()/extract_revenue_figure()
calls, and orchestrator.py's early competitor-check/persona futures).
Rather than thread one shared trace OBJECT across those thread boundaries
(a live SDK span isn't safe to share across threads without its own
locking), each call gets its own small trace — but they're still grouped
together in Langfuse's Sessions tab via a shared `session_id` (the run_id),
read automatically from `current_run_id` below rather than passed
explicitly at every call site (which would mean threading it through
every function this pipeline's ~15 different Anthropic-calling code paths
go through — P1/P2/P3/SME matchers, narrative sections, the persona
generator, etc.). `current_run_id` is a contextvar, not a plain module
global, specifically because this pipeline is NOT single-threaded:
contextvars don't propagate into `ThreadPoolExecutor.submit()`-scheduled
work automatically (unlike asyncio tasks), so every `.submit()` call site
in this pipeline must wrap its target with `propagate_context()` below —
grep this codebase for it to confirm every submit site has one before
assuming a new one doesn't need it too."""

from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

from api.icp.compat import get_settings

# Rasa Incanta: the ICP bot called load_dotenv() here so the Langfuse SDK could see keys that only
# lived in .env. Removed: it loaded the whole repo .env into os.environ as a side effect of an import
# (in tests that meant real credentials). On Railway the variables are the process environment.

logger = logging.getLogger(__name__)

_settings = get_settings()
TRACING_ENABLED = bool(_settings.langfuse_public_key and _settings.langfuse_secret_key)

# Per the platform's own Agent Identity Table convention (langfuse_guide.md)
# — a short, unique, lowercase slug so this agent's traces are filterable
# apart from every other agent sharing the same Langfuse project.
SERVICE_TAG = "rasa-incanta"  # CLAUDE.md: our Langfuse tag

# Set once, near the top of orchestrator.run(), to that run's run_id — every
# Anthropic call made afterward in the SAME thread (which is most of them:
# interpretation, narrative generation, and everything gather_setu_evidence()
# calls all run synchronously in orchestrator.run()'s own thread) picks this
# up automatically via record_generation()'s own default below. Calls made
# inside a ThreadPoolExecutor worker (research()/extract_revenue_figure(),
# the competitor-check and persona-deep-dive futures) do NOT see this
# automatically — see propagate_context() below, which every .submit() call
# site in this pipeline must use to carry it across that thread boundary.
current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("icp_current_run_id", default=None)

_F = TypeVar("_F", bound=Callable[..., Any])


def propagate_context(fn: _F) -> _F:
    """Wraps `fn` so that when it's later invoked (typically inside a
    ThreadPoolExecutor worker, via `pool.submit(propagate_context(fn), ...)`)
    it runs with a SNAPSHOT of the calling thread's context at wrap time —
    carrying `current_run_id` (and any other contextvar) across the thread
    boundary that contextvars don't cross on their own. Call this at the
    `.submit()` call site itself (not earlier), so the snapshot is taken
    after `current_run_id.set(...)` has actually run."""
    ctx = contextvars.copy_context()

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        return ctx.run(fn, *args, **kwargs)

    return _wrapped  # type: ignore[return-value]


def usage_details(usage: Any) -> dict[str, int] | None:
    """Maps an Anthropic `usage` object onto Langfuse's `usage_details`,
    INCLUDING the two cache counters.

    Those were previously dropped: `record_generation()` only ever forwarded
    `{"input": ..., "output": ...}`, even though `anthropic_client.log_usage()`
    was already computing cache_read/cache_write right next to it for its own
    console line. The consequence was not cosmetic -- Langfuse prices what it
    is told about, so every cost figure in the dashboard was a FLOOR, missing
    the ~19k-token cached rubric block that rides on 13 calls per run. A run
    reported at $1.07 was really costing more, and there was no way to see it
    from the product whose job is showing exactly that."""
    if usage is None:
        return None
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    if input_tokens is None or output_tokens is None:
        return None
    details = {"input": input_tokens, "output": output_tokens}
    # Anthropic reports these as separate counters; `input_tokens` already
    # EXCLUDES whatever was served from cache, so these are additive, not a
    # breakdown of the number above.
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    if cache_read:
        details["cache_read_input_tokens"] = cache_read
    if cache_write:
        details["cache_creation_input_tokens"] = cache_write
    # A breakdown of `output` above, not an additional charge on top of it
    # -- see anthropic_client.log_usage()'s own docstring, which already
    # computes this for its console line but never forwarded it here.
    # Previously only visible by grepping stdout for "[usage]"; this is
    # what makes "how much of our spend is invisible reasoning?" answerable
    # straight from the Langfuse dashboard instead.
    output_details = getattr(usage, "output_tokens_details", None)
    thinking_tokens = getattr(output_details, "thinking_tokens", None) or 0
    if thinking_tokens:
        details["thinking_tokens"] = thinking_tokens
    return details


@contextmanager
def observe_generation(
    name: str, *, model: str, input: Any, session_id: str | None = None, tags: list[str] | None = None,
) -> Iterator[Callable[..., None]]:
    """Wraps an Anthropic call so Langfuse records its REAL duration.

    `record_generation()` below creates and ends its span in the same
    instant, AFTER the call it describes has already returned -- so every
    observation landed in Langfuse with `latency = 0`. Confirmed against
    the live project: all 100 recorded generations across 8 real runs show
    zero duration, which made the one number needed to find a slow stage
    the one number the dashboard could not show. (The per-stage timings
    this pipeline was actually tuned against had to be reconstructed by
    differencing consecutive completion timestamps by hand.)

    Used as a context manager around the call itself, the span is open for
    exactly as long as the request is in flight:

        with tracing.observe_generation("brief_section", model=m, input=p) as finish:
            response = ...            # the real API call
            finish(output=text, usage=response.usage)

    `finish` is optional -- a call that raises still closes its span, and
    still reports the duration up to the failure, which is what you want
    when diagnosing a timeout. Never raises and never blocks the call it
    wraps: tracing is an observability nice-to-have, and a Langfuse outage
    must not be able to fail a real ICP run."""
    if not TRACING_ENABLED:
        yield lambda **_: None
        return

    root = generation = None
    try:
        from langfuse import get_client

        client = get_client()
        root = client.start_span(name=name, input=input)
        root.update_trace(
            name=name, session_id=session_id or current_run_id.get(), tags=[SERVICE_TAG, *(tags or [])],
        )
        generation = root.start_generation(name=name, input=input, model=model)
    except Exception as exc:
        logger.warning("Langfuse span could not be opened for %r — continuing untraced: %s", name, exc)
        yield lambda **_: None
        return

    captured: dict[str, Any] = {}

    def finish(*, output: Any = None, usage: Any = None) -> None:
        captured["output"] = output
        captured["usage"] = usage

    try:
        yield finish
    finally:
        try:
            generation.update(output=captured.get("output"), usage_details=usage_details(captured.get("usage")))
            generation.end()
            root.update_trace(output=captured.get("output"))
            root.end()
        except Exception as exc:
            logger.warning("Langfuse span could not be closed for %r: %s", name, exc)


def record_generation(
    name: str,
    *,
    input: Any,
    output: Any,
    model: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
) -> None:
    """Records one Anthropic call as its own Langfuse trace + child
    "generation" observation — model/input/output/token usage, so cost and
    latency show up per-call the same way a live LangChain call's would.
    No-ops entirely (and never raises) when Langfuse isn't configured or
    the SDK call itself fails — tracing is an observability nice-to-have,
    never something that should be able to fail a real ICP run."""
    if not TRACING_ENABLED:
        return

    try:
        from langfuse import get_client

        client = get_client()
        root = client.start_span(name=name)
        root.update_trace(
            name=name, session_id=session_id or current_run_id.get(), tags=[SERVICE_TAG, *(tags or [])],
        )
        usage_details = (
            {"input": input_tokens, "output": output_tokens}
            if input_tokens is not None and output_tokens is not None
            else None
        )
        # Live-confirmed against the actually-installed langfuse==3.0.0:
        # LangfuseSpan has a dedicated start_generation(), not the generic
        # start_observation(as_type="generation", ...) mono-repo's own
        # tracing.py uses — a version-drift difference from that reference,
        # not a design choice; verify again if this package is ever bumped.
        generation = root.start_generation(
            name=name, input=input, output=output, model=model, usage_details=usage_details,
        )
        generation.end()
        root.end()
    except Exception as exc:
        logger.warning("Langfuse tracing FAILED for %r — continuing without it: %s", name, exc)
