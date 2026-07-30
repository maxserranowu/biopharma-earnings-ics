"""Nasdaq's public calendar JSON -- the zero-key fallback.

GET https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD

This is an undocumented internal endpoint behind nasdaq.com. It is free and it
covers every US listing, but it is queried one calendar day at a time, it wants
a browser-like User-Agent, and Nasdaq can change or throttle it without notice.
Treat it as a safety net that keeps the feed alive if a paid key lapses -- not
as the primary source. It is ranked lowest in models.SOURCE_RANK accordingly.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable

from ..models import Candidate
from .base import Provider, derive_fiscal_period

log = logging.getLogger(__name__)

URL = "https://api.nasdaq.com/api/calendar/earnings"

BROWSERISH = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/market-activity/earnings",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
}

TIME_MAP = {
    "time-pre-market": "bmo",
    "time-after-hours": "amc",
    "time-not-supplied": None,
}


class NasdaqProvider(Provider):
    name = "nasdaq"

    def fetch(self, tickers: set[str], start: date, end: date) -> Iterable[Candidate]:
        if not self.enabled:
            return []
        max_days = int(self.settings.get("max_days", 120))
        out: list[Candidate] = []
        d = start
        scanned = 0
        while d <= end and scanned < max_days:
            if d.weekday() < 5:      # US issuers do not report on weekends
                out.extend(self._fetch_day(d, tickers))
                scanned += 1
            d += timedelta(days=1)
        log.info("Nasdaq: %d candidates over %d trading days", len(out), scanned)
        return out

    def _fetch_day(self, d: date, tickers: set[str]) -> list[Candidate]:
        data = self.http.get_json(URL, params={"date": d.isoformat()},
                                  headers=BROWSERISH, allow_cache=False)
        rows = (((data or {}).get("data") or {}).get("rows")) or []
        out = []
        for r in rows:
            sym = (r.get("symbol") or "").upper()
            if sym not in tickers:
                continue
            hour = TIME_MAP.get((r.get("time") or "").strip(), None)
            start_utc, conf = self._resolve(d, hour_code=hour)
            fy, q = derive_fiscal_period(d)
            out.append(Candidate(
                ticker=sym, source="nasdaq", report_date=d,
                fiscal_year=fy, fiscal_quarter=q,
                start_utc=start_utc, time_confidence=conf,
                company_name=(r.get("name") or "").strip(),
            ))
        return out
