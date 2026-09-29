"""GET /api/setu/case-studies and /api/setu/enriched: raw and enriched Setu case studies, no auth."""
from unittest.mock import patch

from api.icp import setu_embeddings
from api.models import SetuCaseStudyContext


def row(name, content="", industry="Food Processing", service_line="SFT"):
    return {"entity_name": name, "content": content, "industry": industry, "service_line": service_line}


def test_case_studies_returns_the_raw_setu_fields(client):
    rows = [row("Patisserie & Bakes", content="A real, detailed write-up well over fifty characters long.")]
    with patch("api.icp.setu_db.fetch_case_studies", return_value=rows):
        response = client.get("/api/setu/case-studies")

    assert response.status_code == 200
    [cs] = response.json()
    assert cs == {"entity_name": "Patisserie & Bakes", "content": rows[0]["content"],
                 "industry": "Food Processing", "service_line": "SFT"}


def test_case_studies_returns_503_when_setu_is_unreachable(client):
    with patch("api.icp.setu_db.fetch_case_studies", side_effect=RuntimeError("Setu Postgres is not configured")):
        response = client.get("/api/setu/case-studies")

    assert response.status_code == 503 and "Setu" in response.json()["detail"]


def test_enriched_labels_real_content_enriched_and_pending(client, migrated):
    from api.db import get_sessionmaker

    real = row("Patisserie & Bakes", content="A real, detailed write-up well over fifty characters long.")
    thin_done = row("Thin Co", content="")
    thin_pending = row("Pending Co", content="short")

    context_hash = setu_embeddings.source_hash(thin_done["entity_name"], thin_done["industry"],
                                               thin_done["service_line"], thin_done["content"])
    with get_sessionmaker()() as s:
        s.add(SetuCaseStudyContext(source_hash=context_hash, case_study_name="Thin Co",
                                   generated_context="A generated context for Thin Co.",
                                   model="claude-haiku-4-5-20251001"))
        s.commit()

    with patch("api.icp.setu_db.fetch_case_studies", return_value=[real, thin_done, thin_pending]):
        response = client.get("/api/setu/enriched")

    assert response.status_code == 200
    by_name = {cs["entity_name"]: cs for cs in response.json()}
    assert by_name["Patisserie & Bakes"]["status"] == "real_content"
    assert by_name["Patisserie & Bakes"]["generated_context"] is None
    assert by_name["Thin Co"]["status"] == "enriched"
    assert by_name["Thin Co"]["generated_context"] == "A generated context for Thin Co."
    assert by_name["Pending Co"]["status"] == "pending"
    assert by_name["Pending Co"]["generated_context"] is None


def test_enriched_returns_503_when_setu_is_unreachable(client):
    with patch("api.icp.setu_db.fetch_case_studies", side_effect=RuntimeError("down")):
        response = client.get("/api/setu/enriched")

    assert response.status_code == 503
