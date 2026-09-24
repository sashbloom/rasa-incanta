"""Stand-ins for the Zoho Postgres copy and the Claude API, fed from recorded fixtures."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from api.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)  # Thursday, 11:30 in Kolkata


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def settings(**overrides) -> Settings:
    values = dict(zoho_pg_host="zoho.example", zoho_pg_user="ri_reader", zoho_pg_password="x",
                  anthropic_api_key="", timezone="Asia/Kolkata")
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self._rows = self.conn.columns if "information_schema" in sql else self.conn.deals

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    """Answers the column listing and the deals query with recorded rows."""

    def __init__(self, columns=None, deals=None):
        self.columns = load("zoho/columns.json") if columns is None else columns
        self.deals = load("zoho/deals.json") if deals is None else deals
        self.executed = []
        self.read_only = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self)


def zoho_connect(conn: FakeConnection):
    return lambda _settings: conn


def claude_response(draft=None, stop_reason="end_turn"):
    from api.engine.nba import NbaDraft

    parsed = NbaDraft(**draft) if draft is not None else None
    content = [SimpleNamespace(type="text", text=json.dumps(draft))] if draft is not None else []
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, content=content)


class FakeClaude:
    """Returns queued responses from messages.parse and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
