"""
Syncs market.models.MarketEvent from DSE's own published AGM/EGM/Record
Date notice (https://www.dsebd.org/Company_AGM_EGM.pdf) — there's no API
or HTML table for this, only a daily-refreshed PDF ("Last Updated" stamp
on the document itself). Same "scrape the official notice, never delete,
only add/update" philosophy as market.services.holiday_sync.

The PDF is fetched into memory and parsed there — never written to disk —
so there's nothing to clean up after a run.

Company names in the PDF are free text and Stock.company_name is not
populated for any real (non-demo) stock in this deployment, so rows are
NOT linked to a Stock FK: doing that reliably would need a verified
name/code directory, and a wrong link would be worse than no link. The
company name is preserved in full in the event title/details instead.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime

import requests

logger = logging.getLogger(__name__)

DSE_AGM_EGM_PDF_URL = "https://www.dsebd.org/Company_AGM_EGM.pdf"
USER_AGENT = "BazaarMarketAnalyzer/1.0 (+educational; respectful rate limits)"

# Purpose-text keywords that indicate the "Date of AGM/EGM" column is
# actually an EGM rather than the (far more common) AGM — the PDF doesn't
# label rows explicitly, so this is a best-effort heuristic, not a
# guarantee. Misclassifying AGM vs EGM is a low-stakes cosmetic label;
# the date/venue/details themselves are unaffected either way.
_EGM_HINTS = ("egm", "extraordinary", "article")

_PLACEHOLDER_TITLE_MAX = 160


class AGMPageParseError(Exception):
    """DSE's PDF structure didn't match what this parser expects (schema
    drift) — kept distinct from network errors so callers/logs can tell
    "site unreachable" apart from "the PDF layout changed, parser needs
    an update"."""


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _parse_notice_date(text: str | None) -> date | None:
    """Parses DSE's "14-Sep-26" style dates. Returns None for placeholder
    text ("Will be notified later", "Postponed", "-", empty, etc.) rather
    than raising — most rows have at least one placeholder column and
    that is normal, not a parse failure."""
    cleaned = _clean(text).rstrip(".")
    if not cleaned:
        return None
    try:
        return datetime.strptime(cleaned, "%d-%b-%y").date()
    except ValueError:
        return None


def fetch_dse_agm_egm_pdf() -> bytes:
    resp = requests.get(DSE_AGM_EGM_PDF_URL, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.content


def parse_agm_egm_pdf(pdf_bytes: bytes) -> list[dict]:
    """Extract one dict per data row: {company, year_end, purpose,
    agm_egm_date, record_date, venue, time} — the two *_date fields are
    already `date | None` (see _parse_notice_date). Raises
    AGMPageParseError if no data rows were found at all (schema drift),
    matching holiday_sync's "fail loud on total drift, be lenient on
    individual placeholder cells" split."""
    import pdfplumber

    rows: list[dict] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    if not row or len(row) < 7:
                        continue
                    company = _clean(row[0])
                    if not company:
                        continue
                    low = company.lower()
                    if "dhaka stock exchange" in low or low.startswith("name of the company") or "last updated" in low:
                        continue
                    year_end, purpose, agm_egm_raw, record_raw, venue, time_ = (_clean(c) for c in row[1:7])
                    rows.append(
                        {
                            "company": company,
                            "year_end": year_end,
                            "purpose": purpose,
                            "agm_egm_date": _parse_notice_date(agm_egm_raw),
                            "record_date": _parse_notice_date(record_raw),
                            "venue": venue,
                            "time": time_,
                        }
                    )
    if not rows:
        raise AGMPageParseError("no data rows parsed from Company_AGM_EGM.pdf — layout may have changed")
    return rows


def _event_type_for_purpose(purpose: str):
    from market.models import MarketEvent

    low = purpose.lower()
    if any(hint in low for hint in _EGM_HINTS):
        return MarketEvent.EventType.EGM
    return MarketEvent.EventType.AGM


_VENUE_LABEL_PREFIX_RE = re.compile(r"^(venue\s*/\s*(mode|platform)|venue|mode of agm|mode)\s*:\s*", re.IGNORECASE)


def _clean_venue(venue: str) -> str:
    """The source venue cell often already starts with its own "Venue:" /
    "Venue/Mode:" / "Mode of AGM:" label (DSE's free text, not ours), which
    would read as redundant once shown under this page's own "Venue/Mode"
    column header (true for the majority of rows, not an edge case)."""
    return _VENUE_LABEL_PREFIX_RE.sub("", venue) if venue else venue


def _details_text(*, purpose: str = "") -> str:
    """`purpose` is only passed for Record Date events — AGM/EGM events
    already carry it as their title, and repeating it in details as well
    read as redundant on the page. year_end is deliberately omitted here
    (low-value noise for a reader deciding whether to act on this event)
    even though it's still available in the parsed row if a future
    consumer wants it. Venue/time have their own MarketEvent fields now
    (rendered as their own table columns) rather than living here."""
    return f"Purpose: {purpose}" if purpose else ""


def sync_dse_agm_egm_events() -> dict:
    """Fetch + parse the PDF, upsert into MarketEvent, never delete.

    Dedup key is (title, event_type, event_date): title always embeds the
    full company name plus purpose in a fixed, deterministic format, so
    the same underlying (company, purpose, date) triple re-scraped later
    updates the same row in place (e.g. a placeholder date becoming a
    real one) instead of creating a duplicate. A change in the source
    date (a postponement) legitimately produces a new row — the old one
    simply stops appearing on the public page once its date is in the
    past (market.views' MarketEvent query already filters
    event_date__gte=today), so no explicit expiry is needed here.

    Every row is created with is_public=True, matching what was asked:
    new events surface on /market-events/ directly, not into a review
    queue — but each one carries source_url back to this exact PDF so it
    stays independently verifiable per the page's own "confirm each event
    with its source" copy.
    """
    from market.models import MarketEvent

    try:
        pdf_bytes = fetch_dse_agm_egm_pdf()
    except requests.exceptions.RequestException as exc:
        logger.error("dse_events: failed to fetch Company_AGM_EGM.pdf: %s", exc)
        return {"ok": False, "error": str(exc)}

    try:
        rows = parse_agm_egm_pdf(pdf_bytes)
    except AGMPageParseError as exc:
        logger.error("dse_events: %s", exc)
        return {"ok": False, "error": str(exc)}

    created = updated = unchanged = skipped_no_date = 0

    for row in rows:
        company = row["company"]
        purpose = row["purpose"]
        venue, time_ = _clean_venue(row["venue"]), row["time"]

        if row["agm_egm_date"] is not None:
            event_type = _event_type_for_purpose(purpose)
            label = purpose[:_PLACEHOLDER_TITLE_MAX] if purpose else event_type.label
            title = f"{company} — {label}"[:240]
            outcome = _upsert_event(
                title=title,
                event_type=event_type,
                event_date=row["agm_egm_date"],
                details=_details_text(),
                venue=venue,
                event_time=time_,
            )
        else:
            skipped_no_date += 1
            outcome = None
        if outcome == "created":
            created += 1
        elif outcome == "updated":
            updated += 1
        elif outcome == "unchanged":
            unchanged += 1

        if row["record_date"] is not None:
            title = f"{company} — Record date"[:240]
            outcome = _upsert_event(
                title=title,
                event_type=MarketEvent.EventType.RECORD_DATE,
                event_date=row["record_date"],
                details=_details_text(purpose=purpose),
                venue=venue,
                event_time=time_,
            )
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            elif outcome == "unchanged":
                unchanged += 1
        else:
            skipped_no_date += 1

    return {
        "ok": True,
        "parsed_rows": len(rows),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skipped_no_date": skipped_no_date,
    }


def _upsert_event(*, title: str, event_type, event_date: date, details: str, venue: str = "", event_time: str = "") -> str:
    """Returns "created" / "updated" / "unchanged"."""
    from market.models import MarketEvent

    existing = MarketEvent.objects.filter(title=title, event_type=event_type, event_date=event_date).first()
    if existing is None:
        MarketEvent.objects.create(
            title=title,
            event_type=event_type,
            event_date=event_date,
            details=details,
            venue=venue,
            event_time=event_time,
            source_url=DSE_AGM_EGM_PDF_URL,
            is_public=True,
        )
        return "created"
    changed = (
        existing.details != details
        or existing.venue != venue
        or existing.event_time != event_time
        or existing.source_url != DSE_AGM_EGM_PDF_URL
        or not existing.is_public
    )
    if changed:
        existing.details = details
        existing.venue = venue
        existing.event_time = event_time
        existing.source_url = DSE_AGM_EGM_PDF_URL
        existing.is_public = True
        existing.save(update_fields=["details", "venue", "event_time", "source_url", "is_public", "updated_at"])
        return "updated"
    return "unchanged"
