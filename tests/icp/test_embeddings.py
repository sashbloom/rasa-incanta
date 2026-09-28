# New: Voyage AI embeddings client, following test_exa_search.py's own style.
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import embeddings as em


def _fake_response(*, status_code: int = 200, json_data: dict | None = None, text: str = ""):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.text = text
    return response


def test_headers_raise_a_clear_error_without_a_key(monkeypatch):
    from api.icp.compat import Settings

    monkeypatch.setattr(em, "get_settings", lambda: Settings(_env_file=None, voyage_api_key=None))

    with pytest.raises(em.EmbeddingError, match="VOYAGE_API_KEY"):
        em._headers()


def test_embed_many_returns_empty_list_for_no_input():
    with patch.object(em.httpx, "post") as mock_post:
        assert em.embed_many([], input_type="document") == []
    mock_post.assert_not_called()


def test_embed_many_sends_the_input_type_and_model(monkeypatch):
    from api.icp.compat import Settings

    monkeypatch.setattr(em, "get_settings", lambda: Settings(_env_file=None, voyage_api_key="k", embedding_model="voyage-4-lite"))
    fake = _fake_response(json_data={"embeddings": [[0.1, 0.2], [0.3, 0.4]], "total_tokens": 12})

    with patch.object(em.httpx, "post", return_value=fake) as mock_post:
        vectors = em.embed_many(["a case study", "another one"], input_type="document")

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    sent = mock_post.call_args.kwargs["json"]
    assert sent["input"] == ["a case study", "another one"]
    assert sent["model"] == "voyage-4-lite" and sent["input_type"] == "document"
    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer k"


def test_embed_many_raises_on_a_count_mismatch(monkeypatch):
    from api.icp.compat import Settings

    monkeypatch.setattr(em, "get_settings", lambda: Settings(_env_file=None, voyage_api_key="k"))
    fake = _fake_response(json_data={"embeddings": [[0.1, 0.2]], "total_tokens": 5})  # 1 vector for 2 inputs

    with patch.object(em.httpx, "post", return_value=fake):
        with pytest.raises(em.EmbeddingError, match="1 embedding"):
            em.embed_many(["a", "b"], input_type="query")


def test_embed_many_raises_on_non_200(monkeypatch):
    from api.icp.compat import Settings

    monkeypatch.setattr(em, "get_settings", lambda: Settings(_env_file=None, voyage_api_key="k"))
    with patch.object(em.httpx, "post", return_value=_fake_response(status_code=500, text="down")):
        with pytest.raises(em.EmbeddingError, match="500"):
            em.embed_many(["a"], input_type="query")


def test_embed_many_raises_on_connection_failure(monkeypatch):
    from api.icp.compat import Settings

    monkeypatch.setattr(em, "get_settings", lambda: Settings(_env_file=None, voyage_api_key="k"))
    with patch.object(em.httpx, "post", side_effect=em.httpx.ConnectError("boom")):
        with pytest.raises(em.EmbeddingError, match="Voyage request failed"):
            em.embed_many(["a"], input_type="query")


def test_cosine_similarity_of_identical_vectors_is_one():
    assert em.cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_similarity_of_orthogonal_vectors_is_zero():
    assert em.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_of_opposite_vectors_is_minus_one():
    assert em.cosine_similarity([1.0, 1.0], [-1.0, -1.0]) == pytest.approx(-1.0)


def test_cosine_similarity_is_zero_for_a_zero_vector_not_a_division_error():
    assert em.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
