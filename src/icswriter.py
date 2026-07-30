"""RFC 5545 serialiser, written specifically for Outlook subscribed calendars.

Decisions that matter, and why:

METHOD:PUBLISH        Marks the feed as a publication, not an invitation. With
                      METHOD:REQUEST Outlook treats each VEVENT as a meeting
                      invite from an organiser and can generate responses.

Stable UID            UID is (ticker, fiscal year, quarter) -- never the date.
                      Outlook keys off UID; a date-derived UID would produce a
                      duplicate every time a company reschedules.

SEQUENCE              Incremented only on material change. Outlook ignores an
                      update whose SEQUENCE has not advanced, and re-renders the
                      whole calendar if SEQUENCE churns on every build.

TRANSP:TRANSPARENT    Earnings calls are reference information, not commitments.
                      Without this, subscribing marks the user busy for hundreds
                      of hours per quarter and breaks their free/busy lookup.

DTSTART in UTC        Absolute instants, so no VTIMEZONE block is needed and no
                      DST edge case can shift an event. Outlook renders each one
                      in the viewer's own time zone.

VALUE=DATE fallback   When no source has published a call time, an all-day event
                      is honest. Inventing 4:30pm and being wrong is worse than
                      saying "time TBD".

X-ALT-DESC            Outlook (desktop and web) renders this HTML alternative
                      body, which makes the webcast and deck links clickable
                      instead of raw text. Ignored safely by every other client.

CRLF + 75-octet folds Both are mandatory in RFC 5545 and both are places where
                      hand-rolled ICS output most often breaks strict parsers.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .models import EarningsEvent

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
CRLF = "\r\n"
PRODID = "-//biopharma-earnings-ics//Earnings Calendar 1.0//EN"

STATUS_LABEL = {
    "CONFIRMED": "Confirmed by company",
    "TENTATIVE": "Expected (date not yet company-confirmed)",
    "CANCELLED": "Cancelled / withdrawn",
}


# ---------------------------------------------------------------------------
# Low-level RFC 5545 primitives
# ---------------------------------------------------------------------------
def escape_text(value: str) -> str:
    """Escape per RFC 5545 3.3.11. Backslash must be handled first."""
    if value is None:
        return ""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\r\n", r"\n")
        .replace("\n", r"\n")
        .replace("\r", r"\n")
    )


def fold(line: str) -> str:
    """Fold to <=75 octets per line, never splitting a UTF-8 code point.

    RFC 5545 counts octets, not characters. Folding on character count corrupts
    any line containing a non-ASCII company name (Sanofi's 'société', accented
    executives, en-dashes pasted from press releases).
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, start, limit = [], 0, 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        if end < len(raw):
            while end > start and (raw[end] & 0xC0) == 0x80:
                end -= 1          # walk back off a continuation byte
        out.append(raw[start:end].decode("utf-8"))
        start = end
        limit = 74                # continuation lines start with one space
    return (CRLF + " ").join(out)


def prop(name: str, value: str, params: str = "") -> str:
    return fold(f"{name}{params}:{value}")


def dt_utc(iso_z: str) -> str:
    dt = datetime.fromisoformat(iso_z.replace("Z", "+00:00")).astimezone(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def dt_date(iso_date: str) -> str:
    return iso_date.replace("-", "")


def now_utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Human-readable bodies
# ---------------------------------------------------------------------------
def _when_text(ev: EarningsEvent) -> str:
    if ev.all_day_date:
        return f"{ev.all_day_date} - time to be announced"
    dt = datetime.fromisoformat(ev.start_utc.replace("Z", "+00:00"))
    et = dt.astimezone(ET)
    qualifier = "" if ev.time_confidence == "confirmed" else "  (estimated)"
    return (f"{et.strftime('%A, %B %d, %Y at %I:%M %p').replace(' 0', ' ')} ET"
            f"{qualifier}")


_ROWS = [
    ("webcast", "Webcast"),
    ("dial_in", "Dial-In"),
    ("ir", "Investor Relations"),
    ("release", "Earnings Release"),
    ("presentation", "Presentation"),
    ("replay", "Replay"),
]


def description_text(ev: EarningsEvent, compact: bool = False) -> str:
    L = ev.links or {}
    lines = [
        ev.company,
        f"Ticker: {ev.ticker}",
        f"Quarter: Q{ev.fiscal_quarter} {ev.fiscal_year}",
        f"Date/Time: {_when_text(ev)}",
        f"Status: {STATUS_LABEL.get(ev.status, ev.status)}",
        "",
    ]
    for field, label in _ROWS:
        value = L.get(field)
        if value:
            lines.append(f"{label}: {value}")
        elif not compact:
            # Full mode lists every field so the layout is predictable. Compact
            # mode omits blanks -- across ~1,300 events those placeholder lines
            # alone cost hundreds of KB, which is the difference between a feed
            # Outlook syncs and one it struggles with.
            lines.append(f"{label}: Not yet published")
    if ev.notes:
        lines += ["", ev.notes]
    lines += [
        "",
        f"Sources: {', '.join(ev.sources) or 'n/a'}",
        f"Last updated: {ev.last_modified}  (revision {ev.sequence})",
    ]
    return "\n".join(lines)


def description_html(ev: EarningsEvent) -> str:
    L = ev.links or {}
    e = html.escape

    def cell(field: str) -> str:
        v = L.get(field) or ""
        if not v:
            return "<i>Not yet published</i>"
        if field == "dial_in":
            return e(v)
        return f'<a href="{e(v)}">{e(v[:70])}{"&hellip;" if len(v) > 70 else ""}</a>'

    rows = "".join(
        f"<tr><td style='padding:2px 12px 2px 0'><b>{label}</b></td>"
        f"<td style='padding:2px 0'>{cell(field)}</td></tr>"
        for field, label in _ROWS
    )
    note = f"<p>{e(ev.notes)}</p>" if ev.notes else ""
    return (
        "<html><body style=\"font-family:Segoe UI,Arial,sans-serif;font-size:11pt\">"
        f"<p><b>{e(ev.company)}</b> ({e(ev.ticker)})<br>"
        f"Q{ev.fiscal_quarter} {ev.fiscal_year} &middot; {e(_when_text(ev))}<br>"
        f"<span style='color:#666'>{e(STATUS_LABEL.get(ev.status, ev.status))}</span></p>"
        f"<table cellspacing='0' cellpadding='0'>{rows}</table>{note}"
        f"<p style='color:#888;font-size:9pt'>Sources: {e(', '.join(ev.sources) or 'n/a')}"
        f" &middot; Last updated {e(ev.last_modified)} (rev {ev.sequence})</p>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
def render_event(ev: EarningsEvent, *, alarm_minutes: int | None = None,
                 compact: bool = False) -> str:
    lines = ["BEGIN:VEVENT"]
    lines.append(prop("UID", ev.uid))
    lines.append(prop("DTSTAMP", now_utc_stamp()))
    lines.append(prop("SEQUENCE", str(ev.sequence)))

    if ev.all_day_date:
        lines.append(prop("DTSTART", dt_date(ev.all_day_date), ";VALUE=DATE"))
        # DTEND is exclusive for DATE values: next day = a single all-day event.
        from datetime import date as _d, timedelta as _td
        nxt = _d.fromisoformat(ev.all_day_date) + _td(days=1)
        lines.append(prop("DTEND", dt_date(nxt.isoformat()), ";VALUE=DATE"))
    else:
        lines.append(prop("DTSTART", dt_utc(ev.start_utc)))
        lines.append(prop("DTEND", dt_utc(ev.end_utc or ev.start_utc)))

    lines.append(prop("SUMMARY", escape_text(ev.summary)))
    lines.append(prop("DESCRIPTION", escape_text(description_text(ev, compact))))
    if not compact:
        # The HTML body makes links clickable in Outlook, but it roughly doubles
        # per-event size. Worth it on a curated feed, not on a 1,000-name one.
        lines.append(prop("X-ALT-DESC", escape_text(description_html(ev)),
                          ";FMTTYPE=text/html"))

    L = ev.links or {}
    primary = L.get("webcast") or L.get("ir") or L.get("release") or ""
    if primary:
        lines.append(prop("URL", primary, ";VALUE=URI"))
        lines.append(prop("LOCATION", escape_text(primary)))
    lines.append(prop("STATUS", ev.status))
    lines.append(prop("TRANSP", "TRANSPARENT"))
    lines.append(prop("CLASS", "PUBLIC"))
    lines.append(prop("CATEGORIES", "Earnings,Biopharma"))
    lines.append(prop("LAST-MODIFIED", dt_utc(ev.last_modified)))
    if not compact:
        lines.append(prop("CREATED", dt_utc(ev.first_seen or ev.last_modified)))
        lines.append(prop("X-BIOPHARMA-TICKER", ev.ticker))
        lines.append(prop("X-BIOPHARMA-CONFIDENCE", ev.time_confidence))

    if alarm_minutes and ev.status != "CANCELLED" and not ev.all_day_date:
        lines += [
            "BEGIN:VALARM",
            prop("TRIGGER", f"-PT{int(alarm_minutes)}M"),
            "ACTION:DISPLAY",
            prop("DESCRIPTION", escape_text(ev.summary)),
            "END:VALARM",
        ]

    lines.append("END:VEVENT")
    return CRLF.join(lines)


def render_calendar(events: Iterable[EarningsEvent], *, calname: str,
                    caldesc: str = "", ttl_minutes: int = 120,
                    alarm_minutes: int | None = None,
                    compact: bool = False) -> str:
    events = sorted(events, key=lambda e: (e.event_date, e.ticker))
    head = [
        "BEGIN:VCALENDAR",
        prop("PRODID", PRODID),
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        prop("X-WR-CALNAME", escape_text(calname)),
        prop("NAME", escape_text(calname)),
        prop("X-WR-CALDESC", escape_text(caldesc)),
        prop("DESCRIPTION", escape_text(caldesc)),
        "X-WR-TIMEZONE:America/New_York",
        prop("REFRESH-INTERVAL", f"PT{ttl_minutes}M", ";VALUE=DURATION"),
        prop("X-PUBLISHED-TTL", f"PT{ttl_minutes}M"),
        "COLOR:seagreen",
    ]
    body = [render_event(e, alarm_minutes=alarm_minutes, compact=compact)
            for e in events]
    return CRLF.join(head + body + ["END:VCALENDAR", ""])
