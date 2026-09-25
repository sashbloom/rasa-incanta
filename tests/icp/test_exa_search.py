# Copied from icp-bot/backend/tests/test_exa_search.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import exa_search as ex


def _fake_response(*, status_code: int = 200, json_data: dict | None = None, text: str = ""):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.text = text
    return response


def test_headers_raise_a_clear_error_without_an_api_key(monkeypatch):
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, exa_api_key=None)
    monkeypatch.setattr(ex, "get_settings", lambda: settings)

    with pytest.raises(ex.ExaError, match="EXA_API_KEY"):
        ex._headers()


def test_headers_use_x_api_key(monkeypatch):
    from api.icp.compat import Settings

    settings = Settings(_env_file=None, exa_api_key="test-key")
    monkeypatch.setattr(ex, "get_settings", lambda: settings)

    headers = ex._headers()

    assert headers["x-api-key"] == "test-key"


def test_search_and_synthesize_returns_content_and_grounding(monkeypatch):
    from api.icp.compat import Settings

    monkeypatch.setattr(ex, "get_settings", lambda: Settings(_env_file=None, exa_api_key="k"))
    fake_response = _fake_response(json_data={
        "output": {
            "content": {"revenue_cr": 596.19, "ebitda_cr": 103.44},
            "grounding": [{"field": "revenue_cr", "citations": [{"url": "https://example.com", "title": "Filing"}], "confidence": "high"}],
        },
        "costDollars": {"total": 0.012},
    })

    with patch.object(ex.httpx, "post", return_value=fake_response) as mock_post:
        content, grounding = ex.search_and_synthesize(
            "Sula Vineyards FY26 revenue", output_schema={"type": "object"}, system_prompt="Prefer official sources.",
        )

    assert content == {"revenue_cr": 596.19, "ebitda_cr": 103.44}
    assert grounding[0]["field"] == "revenue_cr"
    sent_body = mock_post.call_args.kwargs["json"]
    assert sent_body["query"] == "Sula Vineyards FY26 revenue"
    assert sent_body["type"] == "deep-lite"
    assert sent_body["outputSchema"] == {"type": "object"}


def test_search_and_synthesize_raises_when_no_structured_content_returned(monkeypatch):
    from api.icp.compat import Settings

    monkeypatch.setattr(ex, "get_settings", lambda: Settings(_env_file=None, exa_api_key="k"))
    fake_response = _fake_response(json_data={"output": {}})

    with patch.object(ex.httpx, "post", return_value=fake_response):
        with pytest.raises(ex.ExaError, match="did not return structured output"):
            ex.search_and_synthesize("some query", output_schema={"type": "object"}, system_prompt="p")


def test_post_raises_exa_error_on_non_200(monkeypatch):
    from api.icp.compat import Settings

    monkeypatch.setattr(ex, "get_settings", lambda: Settings(_env_file=None, exa_api_key="k"))
    fake_response = _fake_response(status_code=429, text="rate limited")

    with patch.object(ex.httpx, "post", return_value=fake_response):
        with pytest.raises(ex.ExaError, match="429"):
            ex._post({"query": "x"}, timeout=10)


def test_post_raises_exa_error_on_connection_failure(monkeypatch):
    from api.icp.compat import Settings
    import httpx

    monkeypatch.setattr(ex, "get_settings", lambda: Settings(_env_file=None, exa_api_key="k"))

    with patch.object(ex.httpx, "post", side_effect=httpx.ConnectError("boom")):
        with pytest.raises(ex.ExaError, match="Exa request failed"):
            ex._post({"query": "x"}, timeout=10)


def test_search_many_issues_every_query_and_formats_results(monkeypatch):
    from api.icp.compat import Settings

    monkeypatch.setattr(ex, "get_settings", lambda: Settings(_env_file=None, exa_api_key="k"))

    def fake_post(url, headers, json, timeout):
        query = json["query"]
        slug = query.replace(" ", "-")
        return _fake_response(json_data={
            "results": [
                {"title": f"Result for {query}", "url": f"https://example.com/{slug}", "highlights": ["a relevant excerpt"]},
            ]
        })

    with patch.object(ex.httpx, "post", side_effect=fake_post):
        text = ex.search_many(["query one", "query two"])

    assert "Query: query one" in text
    assert "Query: query two" in text
    assert "a relevant excerpt" in text


def test_search_many_skips_a_failed_query_without_failing_the_batch():
    from api.icp.compat import Settings

    with patch.object(ex, "get_settings", return_value=Settings(_env_file=None, exa_api_key="k")):
        def fake_post(url, headers, json, timeout):
            if json["query"] == "bad query":
                raise ex.httpx.ConnectError("boom")
            return _fake_response(json_data={"results": [{"title": "Good result", "url": "https://good.example", "highlights": ["fine"]}]})

        with patch.object(ex.httpx, "post", side_effect=fake_post):
            text = ex.search_many(["bad query", "good query"])

    assert "Good result" in text
    assert "bad query" not in text


def test_search_many_deduplicates_urls_seen_across_queries():
    from api.icp.compat import Settings

    with patch.object(ex, "get_settings", return_value=Settings(_env_file=None, exa_api_key="k")):
        def fake_post(url, headers, json, timeout):
            return _fake_response(json_data={"results": [{"title": "Same page", "url": "https://same.example", "highlights": ["dup"]}]})

        with patch.object(ex.httpx, "post", side_effect=fake_post):
            text = ex.search_many(["query a", "query b"])

    assert text.count("https://same.example") == 1


def test_search_many_returns_empty_string_for_no_results():
    from api.icp.compat import Settings

    with patch.object(ex, "get_settings", return_value=Settings(_env_file=None, exa_api_key="k")):
        with patch.object(ex.httpx, "post", return_value=_fake_response(json_data={"results": []})):
            text = ex.search_many(["a query"])

    assert text == ""
