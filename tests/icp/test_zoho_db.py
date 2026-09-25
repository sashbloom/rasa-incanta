# Copied from icp-bot/backend/tests/test_zoho_db.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp.zoho_db import _with_fallback_hostaddr


def test_with_fallback_hostaddr_appends_resolved_ip():
    dsn = "postgresql://user:pass@example.com:5432/db?sslmode=require"

    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("1.2.3.4", 5432))]):
        result = _with_fallback_hostaddr(dsn)

    assert "hostaddr=1.2.3.4" in result
    assert "example.com" in result  # host kept for TLS SNI/cert check


def test_with_fallback_hostaddr_leaves_dsn_unchanged_on_resolution_failure():
    dsn = "postgresql://user:pass@example.com:5432/db?sslmode=require"

    with patch("socket.getaddrinfo", side_effect=OSError("name or service not known")):
        result = _with_fallback_hostaddr(dsn)

    assert result == dsn


def test_with_fallback_hostaddr_does_not_override_existing_hostaddr():
    dsn = "postgresql://user:pass@example.com:5432/db?sslmode=require&hostaddr=9.9.9.9"

    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("1.2.3.4", 5432))]):
        result = _with_fallback_hostaddr(dsn)

    assert "hostaddr=9.9.9.9" in result
    assert "1.2.3.4" not in result
