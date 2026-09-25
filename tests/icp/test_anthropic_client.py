# Copied from icp-bot/backend/tests/test_anthropic_client.py; only import paths are adapted.
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import anthropic_client as ac


def _event(type_, **kwargs):
    return SimpleNamespace(type=type_, **kwargs)


def test_log_stream_progress_logs_web_search_and_text_block_starts(caplog):
    events = [
        _event("content_block_start", index=0, content_block=SimpleNamespace(type="server_tool_use", name="web_search")),
        _event("content_block_stop", index=0),
        _event("content_block_start", index=1, content_block=SimpleNamespace(type="text")),
        _event("content_block_stop", index=1),
    ]

    with caplog.at_level("INFO"):
        ac.log_stream_progress(events, "test-label")

    messages = [r.message for r in caplog.records]
    assert any("web_search round started" in m for m in messages)
    assert any("model started writing its text response" in m for m in messages)
    assert sum("block 0 finished" in m or "block 1 finished" in m for m in messages) == 2


def test_log_stream_progress_ignores_unrecognized_event_shapes():
    """Best-effort: this is a logging aid, not load-bearing behavior -- an
    event missing an expected attribute must never crash the whole call."""
    events = [_event("message_start"), _event("message_stop")]

    ac.log_stream_progress(events, "test-label")  # must not raise


def test_log_stream_progress_raises_stream_timeout_error_past_the_budget(monkeypatch):
    """Confirmed live: a real research() call was still running with zero
    errors and zero visibility into whether it was alive or truly stuck --
    a per-read httpx timeout alone never bounds a stream that keeps
    trickling occasional bytes/events indefinitely. This is the hard
    wall-clock ceiling that does."""
    times = iter([0.0, 10.0, 9999.0])
    monkeypatch.setattr(ac.time, "monotonic", lambda: next(times))

    def infinite_events():
        while True:
            yield _event("content_block_start", index=0, content_block=SimpleNamespace(type="text"))

    with pytest.raises(ac.StreamTimeoutError, match="aborting"):
        ac.log_stream_progress(infinite_events(), "test-label", max_seconds=100.0)


def test_get_client_configures_a_bounded_timeout_not_the_sdk_600s_default(monkeypatch):
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, anthropic_api_key="test-key")
    monkeypatch.setattr(ac, "get_settings", lambda: settings)
    ac.get_client.cache_clear()

    client = ac.get_client()

    # The SDK's own default read timeout is 600s -- a single stalled call
    # (reproduced live: one web-search-enabled request hung past 90s with
    # zero response) must fail in a couple of minutes, not the better part
    # of half an hour with retries on top.
    assert client.timeout == ac._REQUEST_TIMEOUT_SECONDS
    assert client.timeout < 600

    ac.get_client.cache_clear()


def test_get_research_client_uses_a_longer_timeout_than_get_client(monkeypatch):
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, anthropic_api_key="test-key")
    monkeypatch.setattr(ac, "get_settings", lambda: settings)
    ac.get_client.cache_clear()

    client = ac.get_research_client()

    # Web-search-enabled calls (research(), ownership_classifier.classify(),
    # secondary_research.extract_revenue_figure()) can run several search
    # rounds in one turn -- confirmed live to sometimes exceed the 150s bound
    # tuned for single-shot calls, so they get their own longer timeout.
    assert client.timeout == ac._RESEARCH_TIMEOUT_SECONDS
    assert ac._RESEARCH_TIMEOUT_SECONDS > ac._REQUEST_TIMEOUT_SECONDS

    ac.get_client.cache_clear()


def test_get_client_raises_a_clear_error_without_an_api_key(monkeypatch):
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, anthropic_api_key=None)
    monkeypatch.setattr(ac, "get_settings", lambda: settings)
    ac.get_client.cache_clear()

    try:
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            ac.get_client()
    finally:
        ac.get_client.cache_clear()


def _fake_response(*, text: str, stop_reason: str = "end_turn"):
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.stop_reason = stop_reason
    return response


def test_research_iterates_the_stream_for_progress_and_returns_the_final_text(monkeypatch):
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, anthropic_api_key="k")
    monkeypatch.setattr(ac, "get_settings", lambda: settings)

    fake_client = MagicMock()
    stream_cm = fake_client.messages.stream.return_value
    fake_stream = stream_cm.__enter__.return_value
    fake_stream.__iter__.return_value = iter([])  # no events -- just proves iteration doesn't blow up
    fake_stream.get_final_message.return_value = _fake_response(text="Real findings here.")
    monkeypatch.setattr(ac, "get_research_client", lambda: fake_client)

    result = ac.research("some prompt")

    assert result == "Real findings here."
    fake_stream.__iter__.assert_called_once()


def test_research_salvages_partial_text_on_truncation_instead_of_discarding_it(monkeypatch, caplog):
    """Live-confirmed real case: a real run visited 57 sources across 8
    search rounds over 8 minutes, and the model had already started writing
    its synthesized answer, before hitting max_tokens -- raising
    unconditionally there discarded all of that real, already-found
    evidence. Unlike the structured-JSON calls (a truncated JSON document
    really is unusable), this call's output is free-text prose, where a
    partial answer is still real, usable content."""
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, anthropic_api_key="k")
    monkeypatch.setattr(ac, "get_settings", lambda: settings)

    fake_client = MagicMock()
    fake_stream = fake_client.messages.stream.return_value.__enter__.return_value
    fake_stream.__iter__.return_value = iter([])
    fake_stream.get_final_message.return_value = _fake_response(
        text="Sula Vineyards reported FY26 revenue of ₹596 Cr, down 3.75% YoY...", stop_reason="max_tokens",
    )
    monkeypatch.setattr(ac, "get_research_client", lambda: fake_client)

    with caplog.at_level("WARNING"):
        result = ac.research("some prompt")

    assert "Sula Vineyards reported FY26 revenue" in result
    assert "cut off by a token limit" in result
    assert any("returning the" in r.message for r in caplog.records)


def test_research_raises_when_truncated_with_no_usable_text_at_all(monkeypatch):
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, anthropic_api_key="k")
    monkeypatch.setattr(ac, "get_settings", lambda: settings)

    fake_client = MagicMock()
    fake_stream = fake_client.messages.stream.return_value.__enter__.return_value
    fake_stream.__iter__.return_value = iter([])
    fake_response = MagicMock()
    fake_response.content = []
    fake_response.stop_reason = "max_tokens"
    fake_stream.get_final_message.return_value = fake_response
    monkeypatch.setattr(ac, "get_research_client", lambda: fake_client)

    with pytest.raises(ac.TruncatedResponseError, match="no usable text"):
        ac.research("some prompt")


def test_extract_json_parses_a_complete_response():
    response = _fake_response(text='{"foo": "bar"}')

    assert ac._extract_json(response, max_tokens=1000) == {"foo": "bar"}


def test_extract_json_raises_a_clear_error_on_truncation_instead_of_a_bare_json_error():
    # Confirmed live: a brief_section.py call hit max_tokens mid-string,
    # and json.loads() raised a "Unterminated string..." error that gave
    # no hint of the actual cause -- this must be caught and explained
    # before ever reaching json.loads().
    response = _fake_response(text='{"foo": "this got cut off mid-str', stop_reason="max_tokens")

    with pytest.raises(ac.TruncatedResponseError, match="max_tokens=1000"):
        ac._extract_json(response, max_tokens=1000)


def test_extract_json_does_not_raise_truncation_error_for_a_complete_response_even_with_a_long_answer():
    response = _fake_response(text='{"foo": "a complete answer"}', stop_reason="end_turn")

    assert ac._extract_json(response, max_tokens=1000) == {"foo": "a complete answer"}


def test_fix_literal_unicode_escapes_repairs_a_double_escaped_rupee_sign():
    """Real bug (Sobha, 2026-09-11, confirmed live and intermittent across
    several earlier runs too): the model occasionally double-escapes a
    non-ASCII character in its JSON output -- the JSON text contains a
    literal backslash followed by "u20b9" as STRING CONTENT (valid JSON,
    since the backslash is itself escaped), rather than a real ₹ character
    or a single \\u20b9 escape. json.loads() never fails on this -- the
    rendered report just shows the raw "\\u20b9659 Cr" text instead of
    "₹659 Cr"."""
    raw_json_text = '{"stat": "\\\\u20b9659 Cr", "classify": "Qualified Prospect \\\\u2014 deal"}'

    result = ac.fix_literal_unicode_escapes(json.loads(raw_json_text))

    assert result == {"stat": "₹659 Cr", "classify": "Qualified Prospect — deal"}


def test_fix_literal_unicode_escapes_leaves_normal_strings_and_nesting_alone():
    value = {"a": "normal text", "b": ["₹100 Cr", {"c": "no escapes here"}], "d": 5, "e": None}

    assert ac.fix_literal_unicode_escapes(value) == value


def test_extract_json_repairs_a_double_escaped_unicode_sequence_in_the_response():
    response = _fake_response(text='{"foo": "\\\\u20b9659 Cr"}')

    assert ac._extract_json(response, max_tokens=1000) == {"foo": "₹659 Cr"}


def test_interpret_criteria_surfaces_a_truncated_response_clearly_after_the_automatic_retry(monkeypatch):
    """Both attempts truncate here (a MagicMock returns the same canned
    response regardless of the max_tokens actually passed), so this proves
    the retry doesn't loop forever or swallow the error -- it still
    surfaces TruncatedResponseError once the one automatic retry is also
    truncated."""
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, anthropic_api_key="k")
    monkeypatch.setattr(ac, "get_settings", lambda: settings)

    fake_client = MagicMock()
    fake_client.messages.stream.return_value.__enter__.return_value.get_final_message.return_value = _fake_response(
        text='{"interpretations": [{"criterion_id": "A1", "condition', stop_reason="max_tokens"
    )
    monkeypatch.setattr(ac, "get_client", lambda: fake_client)

    with pytest.raises(ac.TruncatedResponseError):
        ac.interpret_criteria("some prompt")

    assert fake_client.messages.stream.call_count == 2


def test_generate_structured_narrative_retries_once_at_1_5x_max_tokens_on_truncation(monkeypatch):
    """Live-confirmed (2026-09-01 spike): this model rejects assistant-
    message prefill continuation outright, so the only available recovery
    on a truncation is a full retry at a larger budget -- confirms that
    retry actually happens (a second call, at 1.5x the original
    max_tokens) rather than raising immediately, per _stream_structured_json()'s
    docstring."""
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, anthropic_api_key="k")
    monkeypatch.setattr(ac, "get_settings", lambda: settings)

    fake_client = MagicMock()
    fake_client.messages.stream.return_value.__enter__.return_value.get_final_message.return_value = _fake_response(
        text='{"meta_line": "truncated here...', stop_reason="max_tokens"
    )
    monkeypatch.setattr(ac, "get_client", lambda: fake_client)

    with pytest.raises(ac.TruncatedResponseError, match="max_tokens=9000"):
        ac.generate_structured_narrative("some prompt", {"type": "object"}, max_tokens=6000)

    assert fake_client.messages.stream.call_count == 2
    kwargs_by_call = [call.kwargs for call in fake_client.messages.stream.call_args_list]
    assert kwargs_by_call[0]["max_tokens"] == 6000
    assert kwargs_by_call[1]["max_tokens"] == 9000


def test_generate_structured_narrative_recovers_when_the_retry_succeeds(monkeypatch):
    """The common real case: the first attempt truncates, but the retry at
    a bigger budget finishes cleanly -- the caller should get back the
    parsed JSON, not an exception, with no second manual invocation
    needed."""
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, anthropic_api_key="k")
    monkeypatch.setattr(ac, "get_settings", lambda: settings)

    fake_client = MagicMock()
    truncated = _fake_response(text='{"meta_line": "cut off...', stop_reason="max_tokens")
    complete = _fake_response(text='{"meta_line": "a complete answer"}', stop_reason="end_turn")
    fake_client.messages.stream.return_value.__enter__.return_value.get_final_message.side_effect = [
        truncated, complete,
    ]
    monkeypatch.setattr(ac, "get_client", lambda: fake_client)

    result = ac.generate_structured_narrative("some prompt", {"type": "object"}, max_tokens=6000)

    assert result == {"meta_line": "a complete answer"}
    assert fake_client.messages.stream.call_count == 2


def test_log_usage_logs_token_counts_and_an_estimated_cost(caplog):
    usage = SimpleNamespace(
        input_tokens=10_000,
        output_tokens=2_000,
        cache_creation_input_tokens=5_000,
        cache_read_input_tokens=40_000,
        output_tokens_details=SimpleNamespace(thinking_tokens=1_500),
    )

    with caplog.at_level("INFO"):
        ac.log_usage("test-call()", usage)

    message = caplog.records[-1].message
    assert "test-call()" in message
    assert "input=10000" in message
    assert "output=2000" in message
    assert "thinking=1500" in message
    assert "cache_write=5000" in message
    assert "cache_read=40000" in message
    # (10000*2 + 2000*10 + 5000*2.5 + 40000*0.2) / 1e6 = 0.0605
    assert "0.0605" in message


def test_log_usage_tolerates_missing_cache_and_thinking_fields():
    """Not every real Usage object has cache/thinking fields populated
    (e.g. no cache breakpoint hit, or a non-thinking model) -- must not
    raise just because a call didn't exercise those features."""
    usage = SimpleNamespace(input_tokens=100, output_tokens=50)

    ac.log_usage("test-call()", usage)  # must not raise


def test_cached_system_blocks_can_drop_the_scoring_rubric():
    """Prompt caching is keyed on the whole request prefix INCLUDING the
    json_schema, and every call in a run uses a different schema -- so every
    call writes the cached block at 1.25x and none ever reads it back at
    0.10x. Measured on a real run: 342,705 cache-creation tokens, 0 cache
    reads, ~$0.86 of a $1.91 run. The schema can't be lifted out of the key,
    so the lever is not sending the 808-line rubric to calls that never use
    it."""
    full = ac._cached_system_blocks()
    lean = ac._cached_system_blocks(include_skill_reference=False)

    assert "PERSONA AND FIRM IDENTITY" in full[0]["text"]
    assert "FULL SCORING RUBRIC" in full[0]["text"]

    # The lean block keeps firm identity but drops the rubric entirely.
    assert "PERSONA AND FIRM IDENTITY" in lean[0]["text"]
    assert "FULL SCORING RUBRIC" not in lean[0]["text"]
    assert len(lean[0]["text"]) < len(full[0]["text"])
    # Still cached -- the breakpoint must survive, or the lean calls lose
    # even the within-retry cache hit they do get.
    assert lean[0]["cache_control"] == {"type": "ephemeral"}


def _capture_system(monkeypatch):
    """Records the `system` blocks generate_structured_narrative() builds,
    without touching the network."""
    captured = {}

    def _fake_stream(client, **kwargs):
        captured["system"] = kwargs["system"]
        return {}

    monkeypatch.setattr(ac, "_stream_structured_json", _fake_stream)
    monkeypatch.setattr(ac, "get_client", lambda: MagicMock())
    monkeypatch.setattr(ac, "get_settings", lambda: SimpleNamespace(anthropic_model_narrative="claude-sonnet-5"))
    return captured


def test_generate_structured_narrative_passes_the_lean_block_through(monkeypatch):
    captured = _capture_system(monkeypatch)

    ac.generate_structured_narrative("p", {"type": "object"}, include_skill_reference=False)

    assert "FULL SCORING RUBRIC" not in captured["system"][0]["text"]


def test_generate_structured_narrative_keeps_the_rubric_by_default(monkeypatch):
    """Scoring and narrative calls must keep it -- only the six relevance /
    name-cleanup utilities opt out."""
    captured = _capture_system(monkeypatch)

    ac.generate_structured_narrative("p", {"type": "object"})

    assert "FULL SCORING RUBRIC" in captured["system"][0]["text"]
