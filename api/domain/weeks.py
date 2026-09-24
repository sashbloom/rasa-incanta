"""Weeks run Monday to Sunday in the business timezone (Asia/Kolkata by default)."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def local_today(now: datetime, tz: str) -> date:
    return now.astimezone(ZoneInfo(tz)).date()


def week_start(now: datetime, tz: str) -> date:
    """Monday of the week that `now` falls in, in timezone `tz`."""
    today = local_today(now, tz)
    return today - timedelta(days=today.weekday())
