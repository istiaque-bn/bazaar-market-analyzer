"""
Syncs Stock.sector / Stock.company_name from DSE's own published sector
directory (https://www.dsebd.org/by_industrylisting.php + one
companylistbyindustry.php?industryno=N page per sector).

Why this exists: market.services.close_learn._attach_market_features()
gates all sector-relative features behind next_close_research's
sector_data_is_usable(), which requires >=90% of active stocks to have a
non-blank Stock.sector. As of 2026-08-20 that coverage was 0% in
production — market.services.dse_fetcher's real (non-demo) fetch path
only ever writes last_price/last_change_pct/last_volume, never
sector/company_name — so every sector-relative feature (sector_ret_1d,
sector_index_rel_1d, stock_sector_rel_1d) was silently a constant/proxy
value rather than real signal, despite sector_index_rel_1d topping
feature-importance rankings in that same research. This is the fix: a
real, live, scrapeable source for both fields.

Same "scrape the official page, never delete, only add/update" philosophy
as market.services.dse_events / holiday_sync. Only UPDATES existing Stock
rows matched by (exchange=DSE, trading_code) — never creates new ones;
Stock creation stays owned by dse_fetcher's live quote sync, so a stock
this sync doesn't recognize (e.g. delisted, or not yet fetched) is simply
skipped and counted, not fabricated.
"""
from __future__ import annotations

import logging
import re
import time

import requests
from bs4 import BeautifulSoup
from django.conf import settings

logger = logging.getLogger(__name__)

DSE_INDUSTRY_LISTING_URL = "https://www.dsebd.org/by_industrylisting.php"
DSE_INDUSTRY_COMPANIES_URL = "https://www.dsebd.org/companylistbyindustry.php"
USER_AGENT = "BazaarMarketAnalyzer/1.0 (+educational; respectful rate limits)"

# Polite gap between the 20-odd per-sector page fetches -- this endpoint
# has no documented rate limit, but there's no reason to hammer it either.
_REQUEST_DELAY_SECONDS = 1.0

_INDUSTRYNO_RE = re.compile(r"industryno=(\d+)")


class SectorPageParseError(Exception):
    """DSE's page structure didn't match what this parser expects (schema
    drift) — kept distinct from network errors, matching dse_events'
    AGMPageParseError split."""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    verify = True
    if not getattr(settings, "DSE_SSL_VERIFY", True):
        verify = False
    else:
        try:
            import certifi

            verify = certifi.where()
        except Exception:
            verify = True
    s.verify = verify
    if verify is False:
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
    return s


def fetch_industry_numbers(session: requests.Session | None = None, html: str | None = None) -> list[str]:
    """Discovers the current set of industryno values from DSE's own
    directory page rather than hardcoding them, so a sector DSE adds or
    removes later doesn't silently go stale here. Pass `html` to parse
    pre-fetched content (tests); otherwise fetches live."""
    if html is None:
        session = session or _session()
        resp = session.get(DSE_INDUSTRY_LISTING_URL, timeout=30)
        resp.raise_for_status()
        html = resp.text
    soup = BeautifulSoup(html, "lxml")
    numbers: list[str] = []
    seen = set()
    for a in soup.find_all("a", href=_INDUSTRYNO_RE.search):
        match = _INDUSTRYNO_RE.search(a["href"])
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            numbers.append(match.group(1))
    if not numbers:
        raise SectorPageParseError("no industryno links found on by_industrylisting.php — layout may have changed")
    return numbers


def fetch_sector_companies(
    industryno: str, session: requests.Session | None = None, html: str | None = None
) -> tuple[str, list[tuple[str, str]]]:
    """Returns (sector_label, [(trading_code, company_name), ...]) for one
    industryno. sector_label is read from the page's own "Selected
    Industry:" heading rather than a hardcoded map, so it always matches
    exactly what DSE calls that sector today. Pass `html` to parse
    pre-fetched content (tests); otherwise fetches live."""
    if html is None:
        session = session or _session()
        resp = session.get(DSE_INDUSTRY_COMPANIES_URL, params={"industryno": industryno}, timeout=30)
        resp.raise_for_status()
        html = resp.text
    soup = BeautifulSoup(html, "lxml")

    heading = soup.find(string=re.compile(r"Selected Industry\s*:", re.IGNORECASE))
    sector_label = ""
    if heading:
        sector_label = re.sub(r".*Selected Industry\s*:\s*", "", " ".join(heading.split()), flags=re.IGNORECASE).strip()
    if not sector_label:
        raise SectorPageParseError(f"no 'Selected Industry:' heading found for industryno={industryno}")

    table = soup.find("table", class_="table-borderless")
    if table is None:
        raise SectorPageParseError(f"no table-borderless company table found for industryno={industryno}")

    rows: list[tuple[str, str]] = []
    for a in table.find_all("a", href=lambda h: h and "displayCompany.php" in h):
        code = a.get_text(strip=True)
        if not code:
            continue
        name = ""
        tail = a.next_sibling
        if tail:
            name = " ".join(str(tail).split()).strip(" ()").replace("\xa0", " ").strip()
        rows.append((code, name))
    if not rows:
        raise SectorPageParseError(f"no company rows parsed for industryno={industryno} ({sector_label!r})")
    return sector_label, rows


def sync_dse_sector_classification() -> dict:
    """Fetch every DSE sector page and upsert Stock.sector/company_name
    for stocks already known to us (matched by trading_code). Never
    creates Stock rows, never deletes/blanks a field this run didn't see
    fresh data for. A single sector page failing to parse is logged and
    skipped -- it does not abort the rest of the run, matching dse_events'
    per-row leniency; only total discovery failure (no industry numbers
    at all) is a hard error.
    """
    from market.models import Exchange, Stock

    session = _session()
    try:
        industry_numbers = fetch_industry_numbers(session)
    except (requests.exceptions.RequestException, SectorPageParseError) as exc:
        logger.error("dse_sector_sync: failed to discover industry list: %s", exc)
        return {"ok": False, "error": str(exc)}

    updated = unchanged = unmatched = failed_sectors = 0
    sectors_synced = 0
    unmatched_codes: list[str] = []

    for i, industryno in enumerate(industry_numbers):
        if i > 0:
            time.sleep(_REQUEST_DELAY_SECONDS)
        try:
            sector_label, rows = fetch_sector_companies(industryno, session)
        except (requests.exceptions.RequestException, SectorPageParseError) as exc:
            logger.warning("dse_sector_sync: skipping industryno=%s: %s", industryno, exc)
            failed_sectors += 1
            continue

        sectors_synced += 1
        for code, name in rows:
            stock = Stock.objects.filter(exchange=Exchange.DSE, trading_code=code).first()
            if stock is None:
                unmatched += 1
                unmatched_codes.append(code)
                continue
            fields = []
            if stock.sector != sector_label:
                stock.sector = sector_label
                fields.append("sector")
            if name and stock.company_name != name:
                stock.company_name = name
                fields.append("company_name")
            if fields:
                stock.save(update_fields=fields)
                updated += 1
            else:
                unchanged += 1

    return {
        "ok": True,
        "sectors_discovered": len(industry_numbers),
        "sectors_synced": sectors_synced,
        "sectors_failed": failed_sectors,
        "updated": updated,
        "unchanged": unchanged,
        "unmatched": unmatched,
        "unmatched_codes_sample": unmatched_codes[:20],
    }
