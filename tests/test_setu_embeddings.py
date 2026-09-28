"""Embedding-based narrowing of Setu case studies: enrichment of thin case studies, the append-
only embedding cache (keyed on exact source text, and on the embedding model), and cosine-ranked
retrieval. No real Voyage or Claude call is made; both are mocked."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from api.db import get_sessionmaker
from api.icp import setu_embeddings as se
from api.icp.compat import Settings as IcpSettings
from api.models import SetuCaseStudyContext, SetuCaseStudyEmbedding


def case_study(name="Patisserie & Bakes", industry="Food Processing", service_line="SFT", content="", **overrides):
    return {"name": name, "industry": industry, "service_line": service_line, "content": content,
            "industry_match": False, "geography_match": False, "keyword_hits": [], "score": 1.0, **overrides}


@pytest.fixture(autouse=True)
def voyage_settings(monkeypatch):
    monkeypatch.setattr(se, "get_settings", lambda: IcpSettings(
        _env_file=None, voyage_api_key="k", embedding_model="voyage-4-lite",
        anthropic_api_key="k", anthropic_model_extraction="claude-haiku-4-5-20251001",
    ))


def claude_text(text: str):
    usage = SimpleNamespace(input_tokens=20, output_tokens=10)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage)


# ---------------------------------------------------------------- source_hash

def test_source_hash_is_deterministic_and_changes_with_any_field():
    a = se.source_hash("Patisserie & Bakes", "Food Processing", "SFT", "Working capital controls.")
    same = se.source_hash("Patisserie & Bakes", "Food Processing", "SFT", "Working capital controls.")
    assert a == same
    assert a != se.source_hash("Patisserie & Bakes", "Food Processing", "SFT", "A different summary.")
    assert a != se.source_hash("F-Mart Mobiles", "Food Processing", "SFT", "Working capital controls.")


# ---------------------------------------------------------------- enrichment + embedding cache

def test_real_content_is_embedded_directly_without_a_haiku_call(migrated):
    cs = case_study(content="A detailed real case study about working capital transformation at a bakery chain.")
    get_client = MagicMock()

    with patch.object(se, "_generate_context") as mock_generate, \
         patch("api.icp.embeddings.embed_many", return_value=[[0.1, 0.2, 0.3]]) as mock_embed:
        embeddings = se._ensure_embeddings([cs])

    mock_generate.assert_not_called()  # real content IS the context; nothing to generate
    sent_text = mock_embed.call_args.args[0][0]
    assert "A detailed real case study" in sent_text and "Patisserie & Bakes" in sent_text
    h = se.source_hash(cs["name"], cs["industry"], cs["service_line"], cs["content"])
    assert embeddings[h] == [0.1, 0.2, 0.3]

    with get_sessionmaker()() as s:
        assert s.scalar(select(SetuCaseStudyContext)) is None  # nothing generated, nothing stored
        row = s.scalar(select(SetuCaseStudyEmbedding).where(SetuCaseStudyEmbedding.source_hash == h))
        assert row.model == "voyage-4-lite" and row.embedding == [0.1, 0.2, 0.3]


def test_thin_content_is_enriched_once_then_reused_across_calls(migrated):
    cs = case_study(name="Thin Co", content="")  # no content at all: below MIN_CONTENT_CHARS

    with patch("api.icp.anthropic_client.get_client", return_value=MagicMock(
               messages=MagicMock(create=MagicMock(return_value=claude_text("A short generated context."))))) as mock_client, \
         patch("api.icp.embeddings.embed_many", return_value=[[0.5, 0.5]]):
        se._ensure_embeddings([cs])

    assert mock_client.return_value.messages.create.call_count == 1
    with get_sessionmaker()() as s:
        ctx = s.scalar(select(SetuCaseStudyContext))
        assert ctx.generated_context == "A short generated context." and ctx.case_study_name == "Thin Co"

    # Second call, same exact source: the cached EMBEDDING is found first, so neither Haiku nor
    # Voyage is even reached.
    with patch("api.icp.anthropic_client.get_client") as mock_client2, \
         patch("api.icp.embeddings.embed_many") as mock_embed2:
        embeddings = se._ensure_embeddings([cs])

    mock_client2.assert_not_called()
    mock_embed2.assert_not_called()
    h = se.source_hash(cs["name"], cs["industry"], cs["service_line"], "")
    assert embeddings[h] == [0.5, 0.5]

    # Third call, embedding model switched: the cached embedding no longer counts (a different
    # model's vector), so a fresh Voyage call is needed -- but the source TEXT hasn't changed, so
    # the cached Haiku context IS reused; only regenerate a context if the source data changes.
    with patch("api.icp.setu_embeddings.get_settings", return_value=IcpSettings(
               _env_file=None, voyage_api_key="k", embedding_model="voyage-4", anthropic_api_key="k")), \
         patch("api.icp.anthropic_client.get_client") as mock_client3, \
         patch("api.icp.embeddings.embed_many", return_value=[[0.6, 0.6]]) as mock_embed3:
        se._ensure_embeddings([cs])

    mock_client3.assert_not_called()  # no new Haiku call
    sent_text = mock_embed3.call_args.args[0][0]
    assert "A short generated context." in sent_text  # the cached context text, reused verbatim
    with get_sessionmaker()() as s:
        assert s.scalar(select(SetuCaseStudyContext.id)) is not None and \
               len(s.scalars(select(SetuCaseStudyContext)).all()) == 1  # still only the one context row


def test_a_changed_source_is_a_new_row_not_an_overwrite(migrated):
    v1 = case_study(name="Thin Co", content="")
    v2 = case_study(name="Thin Co", content="Now has real content that is definitely fifty characters or more.")

    with patch("api.icp.anthropic_client.get_client", return_value=MagicMock(
               messages=MagicMock(create=MagicMock(return_value=claude_text("Generated."))))), \
         patch("api.icp.embeddings.embed_many", return_value=[[0.1, 0.1]]):
        se._ensure_embeddings([v1])
    with patch("api.icp.embeddings.embed_many", return_value=[[0.2, 0.2]]) as mock_embed:
        se._ensure_embeddings([v2])

    mock_embed.assert_called_once()  # v2's real content skips Haiku but still needs its own embedding
    with get_sessionmaker()() as s:
        rows = s.scalars(select(SetuCaseStudyEmbedding)).all()
        assert len(rows) == 2 and {tuple(r.embedding) for r in rows} == {(0.1, 0.1), (0.2, 0.2)}  # both kept


def test_switching_the_embedding_model_re_embeds_instead_of_reusing_a_stale_vector(migrated):
    cs = case_study(content="Plenty of real content here, well over fifty characters long indeed.")

    with patch("api.icp.embeddings.embed_many", return_value=[[0.1, 0.1]]):
        se._ensure_embeddings([cs])

    with patch("api.icp.setu_embeddings.get_settings", return_value=IcpSettings(
               _env_file=None, voyage_api_key="k", embedding_model="voyage-4", anthropic_api_key="k")), \
         patch("api.icp.embeddings.embed_many", return_value=[[0.9, 0.9]]) as mock_embed_v4:
        embeddings = se._ensure_embeddings([cs])

    mock_embed_v4.assert_called_once()  # a different model can't reuse the old model's vector
    assert list(embeddings.values())[0] == [0.9, 0.9]
    with get_sessionmaker()() as s:
        rows = s.scalars(select(SetuCaseStudyEmbedding)).all()
        assert {r.model for r in rows} == {"voyage-4-lite", "voyage-4"} and len(rows) == 2  # both kept, append-only


# ---------------------------------------------------------------- top_by_similarity

def test_top_by_similarity_returns_empty_for_no_candidates():
    assert se.top_by_similarity([], problem_context="anything") == []


def test_top_by_similarity_ranks_by_cosine_and_narrows_to_limit(migrated):
    close = case_study(name="Close Match", industry_match=True)
    far = case_study(name="Far Match")
    unrelated = case_study(name="Unrelated")

    with get_sessionmaker()() as s:
        for cs, vector in ((close, [1.0, 0.0]), (far, [0.7, 0.3]), (unrelated, [0.0, 1.0])):
            h = se.source_hash(cs["name"], cs["industry"], cs["service_line"], cs["content"])
            s.add(SetuCaseStudyEmbedding(source_hash=h, case_study_name=cs["name"], model="voyage-4-lite",
                                         embedding=vector))
        s.commit()

    with patch("api.icp.embeddings.embed_many", return_value=[[1.0, 0.0]]):  # the query embeds identically to "close"
        result = se.top_by_similarity([far, unrelated, close], problem_context="wine industry growth", limit=2)

    assert [cs["name"] for cs in result] == ["Close Match", "Far Match"]  # ranked, unrelated dropped
    assert result[0]["industry_match"] is True  # the original dict's other fields ride through unchanged


def test_top_by_similarity_falls_back_to_keyword_order_when_voyage_is_unset(migrated):
    from api.icp.compat import Settings

    candidates = [case_study(name="A"), case_study(name="B"), case_study(name="C")]
    with patch("api.icp.setu_embeddings.get_settings", return_value=Settings(_env_file=None, voyage_api_key=None)):
        result = se.top_by_similarity(candidates, problem_context="anything", limit=2)

    assert result == candidates[:2]  # order untouched, just truncated


def test_top_by_similarity_falls_back_when_voyage_fails(migrated):
    from api.icp.embeddings import EmbeddingError

    candidates = [case_study(name="A"), case_study(name="B")]
    with patch("api.icp.embeddings.embed_many", side_effect=EmbeddingError("Voyage returned 500")):
        result = se.top_by_similarity(candidates, problem_context="anything", limit=5)

    assert result == candidates  # never raises, degrades to the existing order
