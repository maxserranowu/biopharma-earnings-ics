"""Finnhub earnings calendar.

GET https://finnhub.io/api/v1/calendar/earnings?from=&to=&token=
  -> {"earningsCalendar":[{"date","symbol","hour","quarter","year",...}]}

Finnhub is the useful second opinion: it supplies an explicit fiscal
`quarter`/`year` pair, which keeps our UID assignment honest around fiscal-year
boundaries where a date-based guess would drift.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable

from ..models import Candidate
from .base import Provider, derive_fiscal_period

log = logging.getLogger(__name__)

URL = "https://finnhub.io/api/v1/calendar/earnings"


class FinnhubProvider(Provider):
    name = "finnhub"

    def __init__(self, http, cfg):
        super().__init__(http, cfg)
        self.token = self.settings.get("api_key", "")
        if self.enabled and not self.token:
            log.warning("Finnhub enabled but no api_key set; disabling")
            self.enabled = False

    def fetch(self, tickers: set[str], start: date, end: date) -> Iterable[Candidate]:
        if not self.enabled:
            return []
        out: list[Candidate] = []
        for a, b in self._chunk_dates(start, end):
            data = self.http.get_json(
                URL,
                params={"from": a.isoformat(), "to": b.isoformat(), "token": self.token},
                allow_cache=False,
            )
            rows = (data or {}).get("earningsCalendar") or []
            for r in rows:
                sym = (r.get("symbol") or "").upper()
                if sym not in tickers:
                    continue
                try:
                    rd = date.fromisoformat(str(r.get("date"))[:10])
                except (TypeError, ValueError):
                    continue
                start_utc, conf = self._resolve(rd, hour_code=r.get("hour"))
                fy = r.get("year")
                q = r.get("quarter")
                if not (isinstance(fy, int) and isinstance(q, int) and 1 <= q <= 4):
                    fy, q = derive_fiscal_period(rd)
                out.append(Candidate(
                    ticker=sym, source="finnhub", report_date=rd,
                    fiscal_year=fy, fiscal_quarter=q,
                    start_utc=start_utc, time_confidence=conf,
                ))
        log.info("Finnhub: %d candidates", len(out))
        return out
