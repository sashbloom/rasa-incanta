"""One process, two addresses: `/` and `/reports/rasa-incanta/` serve the same app."""
import pytest

from api.main import resolve_static

PREFIX = "/reports/rasa-incanta"


@pytest.mark.parametrize("base", ["", PREFIX])
def test_index_is_identical_at_root_and_prefix(client, base):
    response = client.get(base + "/")
    assert response.status_code == 200
    assert response.text == client.get("/").text
    assert 'src="/reports/rasa-incanta/assets/index-abc.js"' in response.text
    assert response.headers["cache-control"] == "no-cache"


def test_bare_prefix_redirects_to_the_slash(client):
    response = client.get(PREFIX + "?x=1", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == PREFIX + "/?x=1"


@pytest.mark.parametrize("base", ["", PREFIX])
def test_assets_are_served_under_both(client, base):
    response = client.get(base + "/assets/index-abc.js")
    assert response.status_code == 200 and "console.log" in response.text
    assert "immutable" in response.headers["cache-control"]


@pytest.mark.parametrize("base", ["", PREFIX])
def test_client_routes_get_index(client, base):
    response = client.get(base + "/deals/123")
    assert response.status_code == 200 and '<div id="root">' in response.text


def test_missing_asset_is_a_404_not_the_index(client):
    assert client.get(PREFIX + "/assets/missing.js").status_code == 404
    assert client.get("/favicon.ico").status_code == 404


@pytest.mark.parametrize("base", ["", PREFIX])
def test_unknown_api_path_is_json_404(client, base):
    response = client.get(base + "/api/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def raw_get(path: str, headers=()) -> tuple[int, bytes]:
    """Call the ASGI app with a path exactly as the server would decode it: no client-side
    normalisation of ../ and no refusal of non-ASCII header bytes, unlike httpx."""
    import anyio

    from api.main import app

    async def call():
        sent = []
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": path, "raw_path": path.encode("latin-1"), "root_path": "",
                 "query_string": b"", "headers": [(b"host", b"test"), *headers],
                 "client": ("test", 1), "server": ("test", 80)}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)
        return sent

    messages = anyio.run(call)
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return messages[0]["status"], body


# Decoded paths as they reach scope["path"], three or more levels deep (two levels would read
# as safe against vulnerable code, because they land short of the file).
TRAVERSALS = [
    "/../../../secret.txt",
    "/assets/../../../../secret.txt",
    PREFIX + "/../../../secret.txt",
    PREFIX + "/assets/../../../../secret.txt",
    "/..\\..\\..\\secret.txt",
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_path_traversal_never_leaves_the_build(client, path):
    _, body = raw_get(path)
    assert b"outside the build" not in body


@pytest.mark.parametrize("path", ["/%2e%2e%2f%2e%2e%2f%2e%2e%2fsecret.txt", "/..%2f..%2f..%2fsecret.txt",
                                  "/%252e%252e%252fsecret.txt"])
def test_encoded_traversal_through_http_never_leaves_the_build(client, path):
    assert "outside the build" not in client.get(path).text


def test_the_traversal_check_can_fail(client, monkeypatch):
    """Watch it go red: with containment removed, the same vector reads the secret."""
    import os

    import api.main as main

    def naive(static_dir, full_path):
        candidate = os.path.join(static_dir, full_path)
        return candidate if os.path.isfile(candidate) else None

    monkeypatch.setattr(main, "resolve_static", naive)
    _, body = raw_get("/../../../secret.txt")
    assert b"outside the build" in body


def test_resolve_static_is_contained(static_dir):
    assert resolve_static(str(static_dir), "index.html")
    assert resolve_static(str(static_dir), "../../../secret.txt") is None
    assert resolve_static(str(static_dir), str(static_dir.parent / "secret.txt")) is None
    assert resolve_static(str(static_dir), "assets") is None  # directories are not files


def test_no_build_means_503_not_a_crash(client, static_dir):
    (static_dir / "index.html").unlink()
    response = client.get("/")
    assert response.status_code == 503 and "npm run build" in response.json()["detail"]
    assert client.get("/health").status_code == 200
