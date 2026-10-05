"""When Microsoft answers with an error, say exactly what it said; one failing search must not lose the rest.
Every Microsoft call is a scripted httpx.MockTransport response."""
import httpx
import pytest

from api.db import get_sessionmaker
from api.engine.linking import deal_identity, mail_belongs
from api.sources import outlook
from api.sources.zoho import Reachout, row_to_deal
from tests.fakes import load
from tests.test_outlook import CALLBACK, MYRAH, NOW, Graph, http, message, ms_settings

REQUEST_ID = "b1c2d3e4-0000-1111-2222-333344445555"
GRAPH_500 = {"error": {"code": "ErrorInternalServerError", "message": "An internal server error occurred. Try again.\r\nSecond line.",
                       "innerError": {"request-id": REQUEST_ID, "date": "2026-10-05T10:00:00"}}}


class FailingGraph(Graph):
    """A Graph whose message search fails for the terms in `fail` (every attempt) with a Graph error body."""

    def __init__(self, fail=(), status=500, body=None, **kw):
        super().__init__(**kw)
        self.fail, self.status, self.body, self.paths = set(fail), status, body or GRAPH_500, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            self.paths.append(request.url.path)
            if request.url.params.get("$search", "").strip('"') in self.fail or "$search" not in request.url.params and "*" in self.fail:
                return httpx.Response(self.status, json=self.body, headers={"request-id": REQUEST_ID})
        return super().__call__(request)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(outlook.time, "sleep", lambda _: None)  # the retries on a 500 would otherwise wait


def connected_session(graph):
    s = get_sessionmaker()()
    with http(graph) as h:
        outlook.connect(s, ms_settings(), "code", h, CALLBACK, NOW)
    return s


def northwind():
    deal = row_to_deal(load("zoho/deals.json")[0])
    return deal, deal_identity(deal, [Reachout(deal_zoho_id=deal.zoho_id, email="priya@northwindfoods.example")])


def test_graph_error_reads_the_code_message_and_request_id():
    response = httpx.Response(500, json=GRAPH_500)
    text = outlook.graph_error(response)
    assert text == f"HTTP 500: ErrorInternalServerError: An internal server error occurred. Try again. (request-id {REQUEST_ID})"


def test_graph_error_copes_with_a_body_that_is_not_json():
    text = outlook.graph_error(httpx.Response(502, text="<html>Bad gateway</html>", headers={"request-id": "abc"}))
    assert text.startswith("HTTP 502: <html>Bad gateway</html>") and text.endswith("(request-id abc)")
    assert outlook.graph_error(httpx.Response(503)) == "HTTP 503"


def test_a_search_failure_says_what_microsoft_said(migrated):
    graph = FailingGraph(fail={"Acme"})
    with http(graph) as h:
        with pytest.raises(outlook.OutlookError, match="ErrorInternalServerError.*internal server error"):
            outlook.search_messages(h, "t", MYRAH, "Acme")
    assert len(graph.paths) == outlook.MAX_RETRIES + 1  # a 500 is retried before giving up


def test_search_uses_the_delegated_me_endpoint(migrated):
    graph = FailingGraph()
    with http(graph) as h:
        outlook.search_messages(h, "t", MYRAH, "Acme")
    assert graph.paths == ["/v1.0/me/messages"]


def test_the_run_status_carries_microsofts_error_not_just_a_status_code(migrated):
    deal, ident = northwind()
    graph = FailingGraph(fail={"Northwind Foods Pvt Ltd"})
    s = connected_session(graph)
    result = outlook.fetch_mail(s, ms_settings(), [(deal.zoho_id, deal.account_name, ident)], mail_belongs,
                                http=http(graph), now=NOW)
    s.close()
    assert not result.ok and result.by_deal == {}
    assert "1 of 1 mail searches failed" in result.error
    assert "'Northwind Foods Pvt Ltd'" in result.error  # which search failed, not only why
    assert "ErrorInternalServerError" in result.error and REQUEST_ID in result.error


def test_one_failed_search_keeps_the_mail_the_others_found(migrated):
    deal, ident = northwind()
    graph = FailingGraph(fail={"Acme"}, messages={
        "Northwind Foods Pvt Ltd": [message(1, "Northwind Foods: phasing", "priya@northwindfoods.example")]})
    s = connected_session(graph)
    deals = [(deal.zoho_id, deal.account_name, ident), ("other-deal", "Acme", ident)]
    result = outlook.fetch_mail(s, ms_settings(), deals, mail_belongs, http=http(graph), now=NOW)
    s.close()
    assert [m.subject for m in result.by_deal[deal.zoho_id]] == ["Northwind Foods: phasing"]
    assert "other-deal" not in result.by_deal
    assert "1 of 2 mail searches failed" in result.error  # still said, so it is not mistaken for a clean run


def test_no_search_uses_the_participants_syntax_graph_rejects(migrated):
    deal, ident = northwind()
    graph = FailingGraph()
    s = connected_session(graph)
    outlook.fetch_mail(s, ms_settings(), [(deal.zoho_id, deal.account_name, ident)], mail_belongs, http=http(graph), now=NOW)
    s.close()
    assert graph.searches and not any(":" in term for term in graph.searches)


def test_an_expired_token_mid_run_is_reported_with_the_graph_code(migrated):
    body = {"error": {"code": "InvalidAuthenticationToken", "message": "Access token has expired or is not yet valid."}}
    deal, ident = northwind()
    graph = FailingGraph(fail={"Northwind Foods Pvt Ltd"}, status=401, body=body)
    s = connected_session(graph)
    result = outlook.fetch_mail(s, ms_settings(), [(deal.zoho_id, deal.account_name, ident)], mail_belongs,
                                http=http(graph), now=NOW)
    s.close()
    assert "InvalidAuthenticationToken" in result.error and "HTTP 401" in result.error


# ---------------------------------------------------------------- the live check

def test_diagnose_reports_each_call_and_the_scopes(migrated):
    graph = FailingGraph(fail={"Practus"})
    s = connected_session(graph)
    with http(graph) as h:
        report = outlook.diagnose(s, ms_settings(), h, NOW)
    s.close()
    assert report["token"] == "ok" and report["me"] == MYRAH and report["me_matches_myrah"] is True
    assert report["mail_read_granted"] is True and "Mail.Read" in report["stored_scope"]
    assert report["read_one_message"]["ok"] is True
    assert report["search"]["ok"] is False and "ErrorInternalServerError" in report["search"]["detail"]
    assert report["endpoint"].endswith("/me/messages")


def test_diagnose_without_a_connection_says_so(migrated):
    with get_sessionmaker()() as s, http(Graph()) as h:
        report = outlook.diagnose(s, ms_settings(), h, NOW)
    assert report["mail_read_granted"] is None and "not connected" in report["token"].lower()


def test_the_debug_route_is_gated(client, debug_key):
    assert client.get("/api/debug/outlook").status_code == 404
    assert client.get("/api/debug/outlook", headers={"X-Debug-Key": "wrong"}).status_code == 404
    assert client.get("/api/debug/outlook", headers={"X-Debug-Key": debug_key}).status_code == 200  # not configured: a report


def test_the_sources_page_shows_the_granted_scopes(api_graph):
    client, _ = api_graph
    outlook_view = client.get("/api/sources").json()["outlook"]
    assert outlook_view["connected"] is False and outlook_view["scope"] is None and outlook_view["mail_read"] is None


from tests.test_outlook import api_graph  # noqa: E402,F401  (the fixture)
