"""Provider interface plus the date/time normalisation every provider shares."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from ..models import Candidate

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# Provider "hour" codes -> our confidence model.
BMO_CODES = {"bmo", "before market open", "before-market-open", "premarket", "pre-market"}
AMC_CODES = {"amc", "after market close", "after-market-close", "postmarket", "post-market"}
DMH_CODES = {"dmh", "during market hours", "during-market-hours"}


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def et_to_utc(d: date, t: time) -> datetime:
    """Localise a wall-clock Eastern time and convert to UTC.

    Doing this properly matters: a 4:30pm ET call is 20:30Z in summer and 21:30Z
    in winter. Hard-coding an offset silently shifts every event by an hour
    twice a year.
    """
    return datetime.combine(d, t, tzinfo=ET).astimezone(UTC)


def resolve_time(report_date: date, hour_code: Optional[str], explicit: Optional[datetime],
                 bmo: str, amc: str) -> tuple[Optional[datetime], str]:
    """Return (start_utc, confidence)."""
    if explicit is not None:
        dt = explicit if explicit.tzinfo else explicit.replace(tzinfo=ET)
        return dt.astimezone(UTC), "confirmed"

    code = (hour_code or "").strip().lower()

    # Several providers return a literal clock time ("16:30", "08:30:00")
    # in the same field others use for bmo/amc codes. Treat it as authoritative:
    # a published time is the single most valuable field in the whole record.
    if ":" in code:
        try:
            hh, mm = code.split(":")[:2]
            hh, mm = int(hh), int(mm[:2])
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return et_to_utc(report_date, time(hh, mm)), "confirmed"
        except ValueError:
            pass

    if code in BMO_CODES:
        return et_to_utc(report_date, parse_hhmm(bmo)), "estimated"
    if code in AMC_CODES:
        return et_to_utc(report_date, parse_hhmm(amc)), "estimated"
    if code in DMH_CODES:
        return et_to_utc(report_date, time(12, 0)), "estimated"
    return None, "unknown"


def derive_fiscal_period(report_date: date,
                         lag_days: int = 45) -> tuple[int, int]:
    """Infer (fiscal_year, quarter) from a report date.

    Companies report roughly 30-60 days after period end, so we step back
    `lag_days` and take the calendar quarter that lands in. This is only a
    fallback -- providers that supply an explicit quarter always win, because
    this value becomes part of the permanent UID.
    """
    period_end = report_date - timedelta(days=lag_days)
    quarter = (period_end.month - 1) // 3 + 1
    return period_end.year, quarter


class Provider(ABC):
    name: str = "base"
    enabled: bool = True

    def __init__(self, http, cfg):
        self.http = http
        self.cfg = cfg
        self.settings = cfg.providers.get(self.name, {}) or {}
        self.enabled = bool(self.settings.get("enabled", False))

    @abstractmethod
    def fetch(self, tickers: set[str], start: date, end: date) -> Iterable[Candidate]:
        """Yield candidates for tickers in the window. Must never raise."""

    # Convenience for subclasses
    def _resolve(self, report_date: date, hour_code=None, explicit=None):
        return resolve_time(
            report_date, hour_code, explicit,
            self.cfg.bmo_time_et, self.cfg.amc_time_et,
        )

    @staticmethod
    def _chunk_dates(start: date, end: date, days: int = 85):
        """Most calendar APIs cap a query window at ~3 months."""
        cur = start
        while cur <= end:
            stop = min(cur + timedelta(days=days), end)
            yield cur, stop
            cur = stop + timedelta(days=1)
