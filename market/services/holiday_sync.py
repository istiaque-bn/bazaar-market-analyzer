"""
Refreshes market.models.MarketHoliday from DSE's own published holiday
notice (https://www.dsebd.org/hts.php) — there's no API, this page is the
only real source. Scraped rather than hand-typed on purpose: the initial
hand-seeded list (market/migrations/0009_seed_market_holidays.py, sourced
from a third-party calendar site) missed a real closure — a 2-day election
holiday — that only showed up on DSE's own page, and disagreed with DSE on
a couple of exact dates within the multi-day Eid breaks.

Runs monthly (market/tasks.py::sync_holiday_calendar_task, scheduled near
month-end) so next month's holidays are on record before they're needed by
the price-history table. Also runnable by hand: `manage.py sync_holidays`.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DSE_HOLIDAYS_URL = "https://www.dsebd.org/hts.php"

_YEAR_RE = re.compile(r"Calendar\s+Year\s+(\d{4})", re.IGNORECASE)
# DSE marks "date might shift with moon sighting" with a trailing "(*)" or
# "*" after the holiday name — strip only that marker, not any trailing
# parenthetical, since names like "Trading Holiday (Bank Holiday)" and
# "Durgapuja (Dashami)" need their parens kept.
_TRAILING_MOON_MARKER_RE = re.compile(r"\s*\(\*\)\s*$")
_TRAILING_STAR_RE = re.compile(r"\*\s*$")
_SINGLE_DATE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)$")
_SAME_MONTH_RANGE_RE = re.compile(r"^(\d{1,2})\s*-\s*(\d{1,2})\s+([A-Za-z]+)$")
_CROSS_MONTH_RANGE_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\s*-\s*(\d{1,2})\s+([A-Za-z]+)$")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


class HolidayPageParseError(Exception):
    """DSE's page structure didn't match what this parser expects (schema
    drift). Kept distinct from network errors so callers/logs can tell
    "site unreachable" apart from "site changed, parser needs an update"."""


def _month_num(name: str) -> int:
    key = name.strip().lower()
    if key not in _MONTHS:
        raise HolidayPageParseError(f"unrecognized month name: {name!r}")
    return _MONTHS[key]


def _parse_date_cell(cell_text: str, year: int) -> list[date]:
    text = " ".join(cell_text.split())

    m = _SINGLE_DATE_RE.match(text)
    if m:
        day, month = m.groups()
        return [date(year, _month_num(month), int(day))]

    m = _SAME_MONTH_RANGE_RE.match(text)
    if m:
        start_day, end_day, month = m.groups()
        mo = _month_num(month)
        return [date(year, mo, d) for d in range(int(start_day), int(end_day) + 1)]

    m = _CROSS_MONTH_RANGE_RE.match(text)
    if m:
        start_day, start_month, end_day, end_month = m.groups()
        start = date(year, _month_num(start_month), int(start_day))
        end = date(year, _month_num(end_month), int(end_day))
        if end < start:
            raise HolidayPageParseError(f"date range end before start: {text!r}")
        days = []
        d = start
        while d <= end:
            days.append(d)
            d += timedelta(days=1)
        return days

    raise HolidayPageParseError(f"unrecognized date cell format: {text!r}")


def fetch_dse_holidays(html: str | None = None) -> tuple[int, list[tuple[date, str]]]:
    """Parse DSE's holiday notice page. Pass `html` to parse pre-fetched
    content (tests); otherwise fetches live. Returns (year, [(date, name)])
    — the same date can appear more than once if two overlapping holidays
    share it (e.g. Eid-ul-Fitr's composite range vs. its named sub-holidays
    like Jumat-ul-Bida); sync_holiday_calendar() picks one name per date."""
    if html is None:
        resp = requests.get(DSE_HOLIDAYS_URL, timeout=20)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    year_match = _YEAR_RE.search(soup.get_text(" ", strip=True))
    if not year_match:
        raise HolidayPageParseError("could not find 'Calendar Year YYYY' text on the holidays page")
    year = int(year_match.group(1))

    holiday_table = None
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row and "Name of Holidays" in header_row.get_text(" ", strip=True):
            holiday_table = table
            break
    if holiday_table is None:
        raise HolidayPageParseError("could not find the holiday table (header 'Name of Holidays' not found)")

    entries: list[tuple[date, str]] = []
    rows = holiday_table.find_all("tr")[1:]  # skip header
    for row in rows:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        name, date_cell = cells[0], cells[1]
        name = _TRAILING_MOON_MARKER_RE.sub("", name)
        name = _TRAILING_STAR_RE.sub("", name).strip()
        try:
            for d in _parse_date_cell(date_cell, year):
                entries.append((d, name))
        except HolidayPageParseError:
            logger.warning("holiday_sync: could not parse row %r — skipping", cells)
            continue

    if not entries:
        raise HolidayPageParseError("holiday table found but no rows parsed")

    return year, entries


def sync_holiday_calendar() -> dict:
    """Fetch + upsert into MarketHoliday. Never deletes existing rows — a
    parse hiccup on DSE's end shouldn't wipe out known data — only adds new
    dates or renames already-known ones. Weekend dates are skipped on
    purpose: trading_calendar.is_weekend() already covers Fri/Sat, and
    including them would just clutter /admin with redundant rows."""
    from market.models import MarketHoliday
    from market.services.trading_calendar import is_weekend

    try:
        year, entries = fetch_dse_holidays()
    except (requests.exceptions.RequestException, HolidayPageParseError) as exc:
        logger.error("holiday_sync: failed to refresh DSE holiday calendar: %s", exc)
        return {"ok": False, "error": str(exc)}

    by_date: dict[date, str] = {}
    for d, name in entries:
        if is_weekend(d):
            continue
        # Table order lists specific single-day holidays (e.g. Jumat-ul-Bida)
        # ahead of the umbrella multi-day entry they fall inside (e.g. the
        # 7-day Eid-ul-Fitr range) — setdefault keeps whichever is seen
        # first, so specific names win and only the unnamed remainder of a
        # range gets the umbrella name. Cosmetic (display label) either way.
        by_date.setdefault(d, name)

    created = 0
    updated = 0
    for d, name in by_date.items():
        obj, was_created = MarketHoliday.objects.get_or_create(date=d, defaults={"name": name})
        if was_created:
            created += 1
        elif obj.name != name:
            obj.name = name
            obj.save(update_fields=["name"])
            updated += 1

    return {
        "ok": True,
        "year": year,
        "parsed_entries": len(entries),
        "created": created,
        "updated": updated,
        "unchanged": len(by_date) - created - updated,
    }
