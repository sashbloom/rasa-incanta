# Copied from icp-bot/backend/tests/test_setu_db.py; only import paths are adapted.
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.icp import setu_db


def test_normalize_person_name_strips_trailing_digit_suffix():
    """Live-confirmed: resume source files are frequently named after the
    person plus a trailing digit ("Fenil Bhimani2", "Divakar Gupta2") that
    doesn't match `employees.employee_name` exactly."""
    assert setu_db.normalize_person_name("Fenil Bhimani2") == "fenil bhimani"
    assert setu_db.normalize_person_name("Fenil Bhimani") == "fenil bhimani"


def test_normalize_person_name_strips_parenthetical_counter():
    assert setu_db.normalize_person_name("Bhoomi Vaghani (2)") == "bhoomi vaghani"


def test_normalize_person_name_strips_cv_resume_prefix():
    assert setu_db.normalize_person_name("CV_Sonal Goel") == "sonal goel"
    assert setu_db.normalize_person_name("Resume Anjali Modi") == "anjali modi"


def test_normalize_person_name_is_case_insensitive_and_whitespace_normalized():
    assert setu_db.normalize_person_name("  ANJALI   Modi  ") == "anjali modi"
