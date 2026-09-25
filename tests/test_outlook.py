"""Outlook through delegated Graph: the one-time sign-in, token rotation, and mail per deal.
Every Microsoft call is answered by httpx.MockTransport; nothing leaves the machine."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from api.db import get_sessionmaker
from api.engine.linking import deal_identity, mail_belongs
from api.models import OAuthToken
from api.sources import outlook
from api.sources.zoho import Reachout, row_to_deal
from tests.fakes import load, settings

NOW = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)
MYRAH = "myrah.a@roibypractus.com"


def ms_settings(**overrides):
    return settings(ms_tenant_id="tenant", ms_client_id="client", ms_client_secret="secret-value",
                    myrah_mailbox=MYRAH, **overrides)


def message(i, subject, sender, to=(), days_ago=1, preview="Following up on the plan."):
    return {"id": f"m{i}", "subject": subject, "bodyPreview": preview, "webLink": f"https://outlook/{i}",
            "receivedDateTime": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
            "from": {"emailAddress": {"name": sender.split("@")[0].title(), "address": sender}},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to], "ccRecipients": []}


class Graph:
    """A scripted Microsoft: token endpoint, /me, and message search."""

    def __init__(self, me=MYRAH, messages=None, throttle=0, token_status=200):
        self.me, self.messages, self.throttle, self.token_status = me, messages or {}, throttle, token_status
        self.token_requests, self.searches = [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method in ("GET", "POST")
        if request.url.path.endswith("/oauth2/v2.0/token"):
            form = parse_qs(request.content.decode())
            self.token_requests.append({k: v[0] for k, v in form.items()})
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client",
                                                               "error_description": "AADSTS7000215: Invalid client secret."})
            n = len(self.token_requests)
            return httpx.Response(200, json={"access_token": f"access-{n}", "refresh_token": f"refresh-{n}",
                                             "expires_in": 3600, "scope": outlook.SCOPE})
        if request.url.path == "/v1.0/me":
            return httpx.Response(200, json={"mail": self.me, "userPrincipalName": self.me})
        if request.url.path.endswith("/messages"):
            assert request.method == "GET"  # read-only
            if self.throttle:
                self.throttle -= 1
                return httpx.Response(429, headers={"Retry-After": "0"})
            term = request.url.params["$search"].strip('"')
            self.searches.append(term)
            return httpx.Response(200, json={"value": self.messages.get(term, [])})
        return httpx.Response(404)


def http(graph: Graph) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(graph))


# ---------------------------------------------------------------- sign-in

def test_authorization_url_asks_for_delegated_read_only_mail():
    url = urlparse(outlook.authorization_url(ms_settings(), "state-1"))
    q = parse_qs(url.query)
    assert url.path == "/tenant/oauth2/v2.0/authorize"
    assert q["scope"] == ["Mail.Read User.Read offline_access"] and q["state"] == ["state-1"]
    assert q["redirect_uri"] == ["http://localhost/callback"] and q["login_hint"] == [MYRAH]
    with pytest.raises(outlook.OutlookError):
        outlook.authorization_url(settings(), "s")  # not configured


def test_the_redirect_must_carry_our_state_and_a_code():
    ok = lambda s: s == "good"  # noqa: E731
    assert outlook.code_from_redirect("http://localhost/callback?code=abc&state=good", ok) == "abc"
    for url in ("http://localhost/callback?code=abc&state=forged", "http://localhost/callback?code=abc",
                "http://localhost/callback?state=good", "http://localhost/callback?error=access_denied&state=good"):
        with pytest.raises(outlook.OutlookError):
            outlook.code_from_redirect(url, ok)


def test_connect_stores_tokens_only_for_myrah(migrated):
    graph = Graph()
    with get_sessionmaker()() as s, http(graph) as h:
        assert outlook.connect(s, ms_settings(), "code-1", h, NOW) == MYRAH
        row = s.scalar(select(OAuthToken))
        assert (row.account, row.refresh_token, row.access_token) == (MYRAH, "refresh-1", "access-1")
    assert graph.token_requests[0]["grant_type"] == "authorization_code"
    assert graph.token_requests[0]["client_secret"] == "secret-value"  # confidential client


def test_someone_else_signing_in_is_refused_and_nothing_is_saved(migrated):
    with get_sessionmaker()() as s, http(Graph(me="intruder@example.com")) as h:
        with pytest.raises(outlook.OutlookError, match="not myrah"):
            outlook.connect(s, ms_settings(), "code", h, NOW)
        assert s.scalar(select(OAuthToken)) is None


def test_a_bad_client_secret_is_reported_plainly(migrated):
    with get_sessionmaker()() as s, http(Graph(token_status=401)) as h:
        with pytest.raises(outlook.OutlookError, match="Invalid client secret"):
            outlook.connect(s, ms_settings(), "code", h, NOW)


def test_access_tokens_refresh_and_the_rotated_refresh_token_is_saved(migrated):
    graph = Graph()
    with get_sessionmaker()() as s, http(graph) as h:
        outlook.connect(s, ms_settings(), "code", h, NOW)
        assert outlook.access_token(s, ms_settings(), h, NOW + timedelta(minutes=30)) == "access-1"  # still valid
        assert outlook.access_token(s, ms_settings(), h, NOW + timedelta(minutes=59, seconds=30)) == "access-2"
        row = s.scalar(select(OAuthToken))
        assert row.refresh_token == "refresh-2"
    assert graph.token_requests[1] == {**graph.token_requests[1], "grant_type": "refresh_token", "refresh_token": "refresh-1"}


def test_not_connected_is_a_clear_error(migrated):
    with get_sessionmaker()() as s, http(Graph()) as h:
        with pytest.raises(outlook.NotConnected):
            outlook.access_token(s, ms_settings(), h, NOW)


# ---------------------------------------------------------------- mail per deal

def connected(graph):
    s = get_sessionmaker()()
    with http(Graph()) as h:
        outlook.connect(s, ms_settings(), "code", h, NOW)
    return s


def test_mail_is_found_by_name_and_domain_filtered_and_newest_first(migrated):
    deal = row_to_deal(load("zoho/deals.json")[0])
    ident = deal_identity(deal, [Reachout(deal_zoho_id=deal.zoho_id, email="priya@northwindfoods.example")])
    graph = Graph(messages={
        "Northwind Foods Pvt Ltd": [
            message(1, "Northwind Foods: phasing", "priya@northwindfoods.example", days_ago=3),
            message(2, "Lunch?", "friend@gmail.com", preview="Nothing about the client"),  # search noise
        ],
        "participants:northwindfoods.example": [
            message(1, "Northwind Foods: phasing", "priya@northwindfoods.example", days_ago=3),  # duplicate
            message(3, "Board pack", "cfo.office@northwindfoods.example", to=[MYRAH], days_ago=1),
        ],
    })
    s = connected(graph)
    result = outlook.fetch_mail(s, ms_settings(), [(deal.zoho_id, deal.account_name, ident)], mail_belongs,
                                http=http(graph), now=NOW)
    s.close()
    assert result.ok
    mails = result.by_deal[deal.zoho_id]
    assert [m.subject for m in mails] == ["Board pack", "Northwind Foods: phasing"]  # dedupe, filter, newest first
    assert graph.searches == ["Northwind Foods Pvt Ltd", "participants:northwindfoods.example"]


def test_throttling_is_retried(migrated):
    graph = Graph(messages={"Acme": [message(1, "Acme update", "a@acme.example")]}, throttle=2)
    with http(graph) as h:
        assert len(outlook.search_messages(h, "t", MYRAH, "Acme")) == 1


def test_fetch_mail_reports_instead_of_raising(migrated):
    with get_sessionmaker()() as s:
        assert "not configured" in outlook.fetch_mail(s, settings(), [], mail_belongs).error
        assert "not connected" in outlook.fetch_mail(s, ms_settings(), [], mail_belongs, http=http(Graph())).error.lower()


# ---------------------------------------------------------------- the API

def test_connect_start_and_finish_through_the_api(client, monkeypatch):
    from api.config import get_settings
    import api.main as main

    for k, v in {"MS_TENANT_ID": "tenant", "MS_CLIENT_ID": "client", "MS_CLIENT_SECRET": "secret-value",
                 "MYRAH_MAILBOX": MYRAH}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    graph = Graph()
    real_client = httpx.Client
    monkeypatch.setattr(main.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(graph)))

    assert client.get("/api/sources").json()["outlook"] == {"configured": True, "connected": False,
                                                            "account": None, "mailbox": MYRAH}
    url = client.post("/api/outlook/connect/start").json()["authorize_url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    forged = client.post("/api/outlook/connect/finish", json={"redirect_url": "http://localhost/callback?code=c&state=nope"})
    assert forged.status_code == 400
    done = client.post("/api/outlook/connect/finish", json={"redirect_url": f"http://localhost/callback?code=c&state={state}"})
    assert done.json() == {"connected": True, "account": MYRAH}
    again = client.post("/api/outlook/connect/finish", json={"redirect_url": f"http://localhost/callback?code=c&state={state}"})
    assert again.status_code == 400  # a state works once
    assert client.get("/api/sources").json()["outlook"]["connected"] is True
    assert "access-" not in json.dumps(client.get("/api/sources").json())  # tokens are never returned
