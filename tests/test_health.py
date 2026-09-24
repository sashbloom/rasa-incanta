def test_health_is_at_the_root(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"] == "0.1.0"


def test_health_also_answers_under_the_report_prefix(client):
    assert client.get("/reports/rasa-incanta/health").json()["status"] == "ok"


def test_old_health_path_is_gone(client):
    assert client.get("/api/health").status_code == 404
