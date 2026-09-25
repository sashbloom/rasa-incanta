# Copied from icp-bot/backend/tests/test_tracing.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import tracing


def test_record_generation_is_a_no_op_when_tracing_disabled(monkeypatch):
    monkeypatch.setattr(tracing, "TRACING_ENABLED", False)

    with patch("langfuse.get_client") as mock_get_client:
        tracing.record_generation("some call", input="in", output="out", model="claude-sonnet-5")

    mock_get_client.assert_not_called()


def test_record_generation_creates_a_trace_and_generation_when_enabled(monkeypatch):
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)

    mock_root = MagicMock()
    mock_generation = MagicMock()
    mock_root.start_generation.return_value = mock_generation
    mock_client = MagicMock()
    mock_client.start_span.return_value = mock_root

    with patch("langfuse.get_client", return_value=mock_client):
        tracing.record_generation(
            "some call", input="in", output="out", model="claude-sonnet-5",
            input_tokens=10, output_tokens=20, session_id="run-123", tags=["extra"],
        )

    mock_client.start_span.assert_called_once_with(name="some call")
    mock_root.update_trace.assert_called_once_with(name="some call", session_id="run-123", tags=["rasa-incanta", "extra"])  # Rasa Incanta: our Langfuse tag, not "icp"
    mock_root.start_generation.assert_called_once_with(
        name="some call", input="in", output="out", model="claude-sonnet-5",
        usage_details={"input": 10, "output": 20},
    )
    mock_generation.end.assert_called_once()
    mock_root.end.assert_called_once()


def test_record_generation_omits_usage_details_when_token_counts_missing(monkeypatch):
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)

    mock_root = MagicMock()
    mock_client = MagicMock()
    mock_client.start_span.return_value = mock_root

    with patch("langfuse.get_client", return_value=mock_client):
        tracing.record_generation("some call", input="in", output="out", model="claude-sonnet-5")

    assert mock_root.start_generation.call_args.kwargs["usage_details"] is None


def test_record_generation_never_raises_on_sdk_failure(monkeypatch):
    """Tracing is an observability nice-to-have -- it must never be able to
    fail a real ICP run just because Langfuse itself is unreachable or the
    SDK's API shape drifted."""
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)

    with patch("langfuse.get_client", side_effect=RuntimeError("Langfuse is down")):
        tracing.record_generation("some call", input="in", output="out", model="claude-sonnet-5")  # must not raise


def test_record_generation_defaults_session_id_to_current_run_id(monkeypatch):
    """orchestrator.run() sets current_run_id once per run rather than
    threading run_id through every one of this pipeline's ~15 Anthropic
    call paths -- record_generation() must pick it up automatically when
    no explicit session_id is given."""
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)
    token = tracing.current_run_id.set("20260909-run-abc")
    try:
        mock_root = MagicMock()
        mock_client = MagicMock()
        mock_client.start_span.return_value = mock_root

        with patch("langfuse.get_client", return_value=mock_client):
            tracing.record_generation("some call", input="in", output="out", model="claude-sonnet-5")

        assert mock_root.update_trace.call_args.kwargs["session_id"] == "20260909-run-abc"
    finally:
        tracing.current_run_id.reset(token)


def test_record_generation_explicit_session_id_overrides_current_run_id(monkeypatch):
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)
    token = tracing.current_run_id.set("ambient-run-id")
    try:
        mock_root = MagicMock()
        mock_client = MagicMock()
        mock_client.start_span.return_value = mock_root

        with patch("langfuse.get_client", return_value=mock_client):
            tracing.record_generation(
                "some call", input="in", output="out", model="claude-sonnet-5", session_id="explicit-id",
            )

        assert mock_root.update_trace.call_args.kwargs["session_id"] == "explicit-id"
    finally:
        tracing.current_run_id.reset(token)


def test_propagate_context_carries_a_contextvar_into_a_thread_pool_worker():
    """Live-confirmed real gap this exists to fix: contextvars do NOT
    propagate into ThreadPoolExecutor.submit()-scheduled work on their own
    (unlike asyncio tasks) -- several real Anthropic call sites in this
    pipeline (research()/extract_revenue_figure(), the competitor-check and
    persona-deep-dive futures) run inside exactly such a worker thread."""
    from concurrent.futures import ThreadPoolExecutor

    token = tracing.current_run_id.set("propagation-test-run")
    try:
        def read_current_run_id() -> str | None:
            return tracing.current_run_id.get()

        with ThreadPoolExecutor(max_workers=1) as pool:
            unwrapped = pool.submit(read_current_run_id).result()
            wrapped = pool.submit(tracing.propagate_context(read_current_run_id)).result()
    finally:
        tracing.current_run_id.reset(token)

    assert unwrapped is None
    assert wrapped == "propagation-test-run"


def test_propagate_context_forwards_args_kwargs_and_return_value():
    def add(a, *, b):
        return a + b

    result = tracing.propagate_context(add)(2, b=3)

    assert result == 5


def test_usage_details_forwards_the_cache_counters():
    """These were dropped before: only input/output reached Langfuse, so every
    cost figure in the dashboard was a floor that silently omitted the ~19k
    cached rubric block riding on 13 calls per run."""
    usage = MagicMock(
        input_tokens=921, output_tokens=2000,
        cache_read_input_tokens=19000, cache_creation_input_tokens=0,
        # Explicit None rather than leaving this to MagicMock's own
        # auto-vivification -- an un-set attribute on a MagicMock returns
        # another (truthy) MagicMock, not a real None/missing value, which
        # would otherwise make this test pass for the wrong reason once
        # thinking-token forwarding was added below.
        output_tokens_details=None,
    )

    details = tracing.usage_details(usage)

    assert details["input"] == 921
    assert details["output"] == 2000
    assert details["cache_read_input_tokens"] == 19000
    # A zero cache-write is omitted rather than sent as a misleading 0.
    assert "cache_creation_input_tokens" not in details
    assert "thinking_tokens" not in details


def test_usage_details_forwards_thinking_tokens_as_a_breakdown_of_output():
    """Not an additional charge on top of `output` -- see
    anthropic_client.log_usage()'s own docstring -- but previously only
    visible by grepping stdout for "[usage]"; this is what makes "how much
    of our spend is invisible reasoning?" answerable from the Langfuse
    dashboard directly."""
    usage = MagicMock(
        input_tokens=500, output_tokens=2000, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        output_tokens_details=MagicMock(thinking_tokens=1750),
    )

    details = tracing.usage_details(usage)

    assert details["thinking_tokens"] == 1750


def test_usage_details_is_none_without_real_counts():
    assert tracing.usage_details(None) is None
    assert tracing.usage_details(MagicMock(input_tokens=None, output_tokens=5)) is None


def test_observe_generation_is_a_no_op_when_tracing_disabled(monkeypatch):
    monkeypatch.setattr(tracing, "TRACING_ENABLED", False)

    with patch("langfuse.get_client") as mock_get_client:
        with tracing.observe_generation("call", model="m", input="in") as finish:
            finish(output="out", usage=MagicMock(input_tokens=1, output_tokens=2))

    mock_get_client.assert_not_called()


def test_observe_generation_keeps_the_span_open_across_the_call(monkeypatch):
    """The whole point of this helper: the span must still be OPEN while the
    wrapped call runs, so Langfuse records a real duration. record_generation()
    opened and closed it after the fact, which is why all 100 recorded
    generations across 8 real runs showed latency 0."""
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)
    mock_root, mock_generation = MagicMock(), MagicMock()
    mock_root.start_generation.return_value = mock_generation
    mock_client = MagicMock()
    mock_client.start_span.return_value = mock_root

    with patch("langfuse.get_client", return_value=mock_client):
        with tracing.observe_generation("brief_section", model="claude-sonnet-5", input="p") as finish:
            # Mid-call: span started, nothing ended yet.
            mock_root.start_generation.assert_called_once()
            mock_generation.end.assert_not_called()
            mock_root.end.assert_not_called()
            finish(output="text", usage=MagicMock(
                input_tokens=10, output_tokens=20, cache_read_input_tokens=19000,
                cache_creation_input_tokens=0,
            ))

    mock_generation.end.assert_called_once()
    mock_root.end.assert_called_once()
    sent = mock_generation.update.call_args.kwargs
    assert sent["output"] == "text"
    assert sent["usage_details"]["cache_read_input_tokens"] == 19000


def test_observe_generation_closes_its_span_even_when_the_call_raises(monkeypatch):
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)
    mock_root, mock_generation = MagicMock(), MagicMock()
    mock_root.start_generation.return_value = mock_generation
    mock_client = MagicMock()
    mock_client.start_span.return_value = mock_root

    with patch("langfuse.get_client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="boom"):
            with tracing.observe_generation("call", model="m", input="p"):
                raise RuntimeError("boom")

    mock_generation.end.assert_called_once()
    mock_root.end.assert_called_once()


def test_observe_generation_never_breaks_the_call_it_wraps(monkeypatch):
    """Tracing is observability, never load-bearing -- a Langfuse outage must
    not be able to fail a real ICP run."""
    monkeypatch.setattr(tracing, "TRACING_ENABLED", True)

    with patch("langfuse.get_client", side_effect=RuntimeError("langfuse down")):
        with tracing.observe_generation("call", model="m", input="p") as finish:
            finish(output="out", usage=None)  # must not raise
