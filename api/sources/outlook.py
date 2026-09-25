"""Outlook mail in Myrah's mailbox, through Microsoft Graph with DELEGATED permissions.

Adapted from the ICP bot (`graph_oauth.py`, `mail_client.py`). Practus policy allows delegated
Graph permissions only, so Myrah signs in once (OAuth authorization code, confidential client
with MS_CLIENT_ID / MS_CLIENT_SECRET) and the refresh token then mints access tokens unattended.
Microsoft rotates the refresh token on every use, so it is saved back to `oauth_tokens` each time.

Differences from the ICP bot:
- Tokens live in our Postgres (`oauth_tokens`), not a file on disk.
- A sign-in is accepted only for MYRAH_MAILBOX: after the code exchange we ask Graph who signed
  in, and anyone else is refused and nothing is stored. The board is open, so this is what stops
  a stranger connecting their own mailbox and feeding its mail into the actions.
- The sign-in carries a one-time `state`, checked on the way back.

Read-only: every Graph call here is a GET on messages or on /me. Nothing sends, moves or deletes.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import Settings
from api.models import OAuthToken

logger = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
PROVIDER = "microsoft_graph"
SCOPE = "Mail.Read User.Read offline_access"  # User.Read lets us confirm who signed in
REFRESH_MARGIN = timedelta(seconds=60)
MAX_RETRIES = 3
SEARCH_TOP = 10
MAX_MAILS_PER_DEAL = 5
SEARCH_WORKERS = 6


class OutlookError(RuntimeError):
    """Anything that stops mail being read; the run turns it into a gap, never a crash."""


class NotConnected(OutlookError):
    pass


def _authority(settings: Settings) -> str:
    return f"https://login.microsoftonline.com/{settings.ms_tenant_id}/oauth2/v2.0"


def configured(settings: Settings) -> bool:
    return bool(settings.ms_tenant_id and settings.ms_client_id and settings.ms_client_secret and settings.myrah_mailbox)


# ---------------------------------------------------------------- one-time sign-in

def authorization_url(settings: Settings, state: str) -> str:
    if not configured(settings):
        raise OutlookError("Outlook is not configured: set MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET and MYRAH_MAILBOX.")
    params = {"client_id": settings.ms_client_id, "response_type": "code", "redirect_uri": settings.ms_redirect_uri,
              "response_mode": "query", "scope": SCOPE, "state": state, "login_hint": settings.myrah_mailbox,
              "prompt": "select_account"}
    return f"{_authority(settings)}/authorize?{urlencode(params)}"


def code_from_redirect(redirect_url: str, expected_state: Callable[[str], bool]) -> str:
    """The `code` from the URL the browser landed on after sign-in, once its `state` checks out."""
    query = parse_qs(urlparse(redirect_url.strip()).query)
    if "error" in query:
        raise OutlookError(f"Sign-in did not complete: {query['error'][0]}. {query.get('error_description', [''])[0]}")
    state = (query.get("state") or [""])[0]
    if not state or not expected_state(state):
        raise OutlookError("That sign-in link has expired or was not started here. Start the connection again.")
    if "code" not in query:
        raise OutlookError("No sign-in code in that URL. Paste the full address your browser landed on.")
    return query["code"][0]


def _token_request(settings: Settings, http: httpx.Client, data: dict) -> dict:
    response = http.post(f"{_authority(settings)}/token", data={
        "client_id": settings.ms_client_id, "client_secret": settings.ms_client_secret, "scope": SCOPE, **data})
    if response.status_code >= 300:
        try:
            body = response.json()
            reason = f"{body.get('error')}: {(body.get('error_description') or '').splitlines()[0][:200]}"
        except ValueError:
            reason = f"HTTP {response.status_code}"
        raise OutlookError(f"Microsoft refused the token request ({reason})")
    return response.json()


def signed_in_mailbox(http: httpx.Client, access_token: str) -> str:
    response = http.get(f"{GRAPH}/me", params={"$select": "mail,userPrincipalName"},
                        headers={"Authorization": f"Bearer {access_token}"})
    if response.status_code != 200:
        raise OutlookError(f"Could not confirm who signed in (HTTP {response.status_code}).")
    me = response.json()
    return (me.get("mail") or me.get("userPrincipalName") or "").strip().lower()


def _store(session: Session, account: str, token: dict, now: datetime) -> OAuthToken:
    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
    if row is None:
        row = OAuthToken(provider=PROVIDER, account=account)
        session.add(row)
    row.account = account
    row.access_token = token["access_token"]
    row.refresh_token = token.get("refresh_token") or row.refresh_token
    row.expires_at = now + timedelta(seconds=int(token.get("expires_in", 3600)))
    row.scope = token.get("scope")
    session.commit()
    return row


def connect(session: Session, settings: Settings, code: str, http: httpx.Client, now: datetime | None = None) -> str:
    """Finish the sign-in: exchange the code, refuse anyone but Myrah, store the tokens."""
    now = now or datetime.now(timezone.utc)
    token = _token_request(settings, http, {"grant_type": "authorization_code", "code": code,
                                            "redirect_uri": settings.ms_redirect_uri})
    if not token.get("refresh_token"):
        raise OutlookError("Microsoft returned no refresh token, so mail could not be read unattended.")
    account = signed_in_mailbox(http, token["access_token"])
    if account != settings.myrah_mailbox.strip().lower():
        raise OutlookError(f"Signed in as {account or 'an unknown account'}, not {settings.myrah_mailbox}. Nothing was saved.")
    _store(session, account, token, now)
    return account


def status(session: Session, settings: Settings) -> dict:
    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
    return {"configured": configured(settings), "connected": row is not None,
            "account": row.account if row else None, "mailbox": settings.myrah_mailbox or None}


def access_token(session: Session, settings: Settings, http: httpx.Client, now: datetime | None = None) -> str:
    """A valid access token, refreshing (and saving the rotated refresh token) when needed."""
    now = now or datetime.now(timezone.utc)
    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
    if row is None:
        raise NotConnected("Outlook is not connected yet: Myrah needs to sign in once from the Sources page.")
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if now < expires - REFRESH_MARGIN:
        return row.access_token
    token = _token_request(settings, http, {"grant_type": "refresh_token", "refresh_token": row.refresh_token})
    _store(session, row.account, token, now)  # the old refresh token is already dead on Microsoft's side
    return token["access_token"]


# ---------------------------------------------------------------- mail search

@dataclass(frozen=True)
class Mail:
    id: str
    subject: str
    sender_name: str | None
    sender_domain: str | None
    domains: frozenset[str]  # sender and recipient domains, for matching to a deal
    received: datetime | None
    preview: str
    web_link: str | None = None


def _domain(address: str | None) -> str | None:
    return address.rsplit("@", 1)[1].strip().lower() if address and "@" in address else None


def message_to_mail(m: dict) -> Mail:
    sender = (m.get("from") or {}).get("emailAddress") or {}
    people = [sender] + [(r or {}).get("emailAddress") or {} for r in (m.get("toRecipients") or []) + (m.get("ccRecipients") or [])]
    received = m.get("receivedDateTime")
    try:
        when = datetime.fromisoformat(received.replace("Z", "+00:00")) if received else None
    except ValueError:
        when = None
    return Mail(
        id=m.get("id") or "", subject=(m.get("subject") or "(no subject)").strip(), sender_name=sender.get("name"),
        sender_domain=_domain(sender.get("address")),
        domains=frozenset(d for d in (_domain(p.get("address")) for p in people) if d),
        received=when, preview=" ".join((m.get("bodyPreview") or "").split()), web_link=m.get("webLink"),
    )


def search_messages(http: httpx.Client, token: str, mailbox: str, query: str, top: int = SEARCH_TOP) -> list[dict]:
    """Graph `$search` over subject, body, sender and recipients. Retries on 429 and 5xx,
    honouring Retry-After. `$search` cannot be combined with `$filter`/`$orderby` on messages."""
    params = {"$search": f'"{query}"', "$top": str(top),
              "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,webLink"}
    for attempt in range(MAX_RETRIES + 1):
        response = http.get(f"{GRAPH}/users/{mailbox}/messages", params=params,
                            headers={"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"})
        if response.status_code == 200:
            return response.json().get("value", [])
        if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            time.sleep(min(float(response.headers.get("Retry-After", 2 ** attempt)), 30))
            continue
        raise OutlookError(f"Mail search failed (HTTP {response.status_code}).")
    return []


def search_terms(company_name: str | None, domains) -> list[str]:
    """What to search a deal's mail for: the company name, and each company domain."""
    terms = [company_name.strip()] if company_name and company_name.strip() else []
    terms += [f"participants:{d}" for d in sorted(domains)]
    return [t.replace('"', "") for t in terms]


@dataclass
class MailResult:
    by_deal: dict[str, list[Mail]] = field(default_factory=dict)  # zoho_id -> newest first
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fetch_mail(session: Session, settings: Settings, deals: list[tuple[str, str | None, object]],
               belongs: Callable[[object, Mail], bool], http: httpx.Client | None = None,
               now: datetime | None = None) -> MailResult:
    """Mail for each deal. `deals` is (zoho_id, company name, identity); `belongs(identity, mail)`
    decides whether a hit really concerns that deal. Never raises: failures come back as `error`."""
    if not configured(settings):
        return MailResult(error="Outlook is not configured: set MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET and MYRAH_MAILBOX.")
    owns_http = http is None
    http = http or httpx.Client(timeout=30.0)
    try:
        token = access_token(session, settings, http, now)
    except OutlookError as exc:
        if owns_http:
            http.close()
        return MailResult(error=str(exc))
    except Exception as exc:
        logger.exception("Outlook token refresh failed")
        if owns_http:
            http.close()
        return MailResult(error=f"Outlook sign-in could not be refreshed ({type(exc).__name__}).")

    def one(item):
        zoho_id, company, identity = item
        seen, mails = set(), []
        for term in search_terms(company, getattr(identity, "domains", ())):
            for message in search_messages(http, token, settings.myrah_mailbox, term):
                mail = message_to_mail(message)
                if mail.id in seen or not belongs(identity, mail):
                    continue
                seen.add(mail.id)
                mails.append(mail)
        mails.sort(key=lambda m: m.received or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return zoho_id, mails[:MAX_MAILS_PER_DEAL]

    result = MailResult()
    try:
        with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
            for zoho_id, mails in pool.map(one, deals):
                if mails:
                    result.by_deal[zoho_id] = mails
    except OutlookError as exc:
        result = MailResult(error=str(exc))
    except Exception as exc:
        logger.exception("Outlook mail search failed")
        result = MailResult(error=f"Outlook mail search failed ({type(exc).__name__}). Details are in the server log.")
    finally:
        if owns_http:
            http.close()
    return result
