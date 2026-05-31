"""Decide whether the office is open, from editable CSV files.

data/hours.csv (per weekday; blank open/close = closed all day):
    day,open,close
    mon,09:00,17:00
    sat,,

data/holidays.csv (whole days the office is closed):
    date,note
    2026-12-25,Christmas

If hours.csv is absent, falls back to the single-window BUSINESS_* env vars.
The master switch is ENFORCE_BUSINESS_HOURS — off means always open.
"""

import csv
import functools
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.services.datafiles import load_cached

log = logging.getLogger("ivr")

HOURS_FILE = "hours.csv"
HOLIDAYS_FILE = "holidays.csv"
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _to_minutes(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    hh, mm = value.split(":")
    return int(hh) * 60 + int(mm)


def _parse_hours(path: Path) -> dict[int, tuple[int, int] | None]:
    """{weekday_index: (open_minutes, close_minutes)} or None for closed days."""
    schedule: dict[int, tuple[int, int] | None] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            day = DAYS.get((row.get("day") or "").strip().lower()[:3])
            if day is None:
                continue
            opens, closes = _to_minutes(row.get("open", "")), _to_minutes(row.get("close", ""))
            schedule[day] = (opens, closes) if opens is not None and closes is not None else None
    return schedule


def _parse_holidays(path: Path) -> set[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        dates = set()
        for row in csv.DictReader(fh):
            value = (row.get("date") or "").strip()
            if value and not value.startswith("#"):
                dates.add(value)
        return dates


@functools.lru_cache(maxsize=4)
def _holiday_calendar(country: str, subdiv: str | None):
    """A `holidays` calendar that lazily computes dates for any year on lookup."""
    import holidays  # imported lazily so the dep is only needed when auto_holidays is on

    return holidays.country_holidays(country, subdiv=subdiv or None)


def _is_public_holiday(day: date) -> bool:
    """True if `day` is an auto-computed public holiday (correct floating/observed dates)."""
    if not settings.auto_holidays or not settings.holiday_country:
        return False
    try:
        return day in _holiday_calendar(settings.holiday_country, settings.holiday_subdiv)
    except Exception as exc:  # noqa: BLE001 — never let holiday calc break a call
        log.warning("Holiday lookup failed: %s", exc)
        return False


def is_open(now: datetime | None = None) -> bool:
    """True if the office is currently open."""
    if not settings.enforce_business_hours:
        return True

    now = now or datetime.now(ZoneInfo(settings.business_timezone))

    # Closed on auto-computed public holidays...
    if _is_public_holiday(now.date()):
        return False
    # ...and on any company-specific closures listed in data/holidays.csv.
    manual = load_cached(HOLIDAYS_FILE, _parse_holidays) or set()
    if now.strftime("%Y-%m-%d") in manual:
        return False

    schedule = load_cached(HOURS_FILE, _parse_hours)
    if schedule is not None:
        window = schedule.get(now.weekday())
        if window is None:
            return False
        minutes = now.hour * 60 + now.minute
        return window[0] <= minutes < window[1]

    # No hours.csv -> fall back to the single-window env config.
    open_days = _env_days()
    if now.weekday() not in open_days:
        return False
    return settings.business_open_hour <= now.hour < settings.business_close_hour


def _env_days() -> set[int]:
    days: set[int] = set()
    for part in settings.business_days.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-")
            days.update(range(int(start), int(end) + 1))
        elif part:
            days.add(int(part))
    return days
