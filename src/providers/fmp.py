"""Financial Modeling Prep.

Two endpoints, deliberately:

  stable/earnings-calendar        -> broad forward coverage, dates are estimates
                                     until the company announces
  v4/earning-calendar-confirmed   -> only calls the company has actually
                                     announced, and it carries `url` (the press
                                     release) and often the exact time

The confirmed feed is ranked above the general one in models.SOURCE_RANK, so a
company-confirmed 4:30pm date overrides a vendor-estimated one automatically.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Iterable

from ..models import Candidate, Links
from .base import ET, Provider, derive_fiscal_period

log = logging.getLogger(__name__)

STABLE = "https://financialmodelingprep.com/stable"
V4 = "https://financialmodelingprep.com/api/v4"


class FmpProvider(Provider):
    name = "fmp"

    def __init__(self, http, cfg):
        super().__init__(http, cfg)
        self.api_key = self.settings.get("api_key", "")
        if self.enabled and not self.api_key:
            log.warning("FMP enabled but no api_key set; disabling")
            self.enabled = False

    def fetch(self, tickers: set[str], start: date, end: date) -> Iterable[Candidate]:
        if not self.enabled:
            return []
        out: list[Candidate] = []
        out.extend(self._fetch_calendar(tickers, start, end))
        if self.settings.get("use_confirmed", True):
            out.extend(self._fetch_confirmed(tickers, start, end))
        log.info("FMP: %d candidates", len(out))
        return out

    # ------------------------------------------------------------------
    def _fetch_calendar(self, tickers, start, end) -> list[Candidate]:
        out = []
        for a, b in self._chunk_dates(start, end):
            rows = self.http.get_json(
                f"{STABLE}/earnings-calendar",
                params={"from": a.isoformat(), "to": b.isoformat(),
                        "apikey": self.api_key},
                allow_cache=False,
            )
            if not isinstance(rows, list):
                continue
            for r in rows:
                sym = (r.get("symbol") or "").upper()
                if sym not in tickers:
                    continue
                try:
                    rd = date.fromisoformat(str(r.get("date"))[:10])
                except (TypeError, ValueError):
                    continue
                start_utc, conf = self._resolve(rd, hour_code=r.get("time"))
                fy, q = self._period(r, rd)
                out.append(Candidate(
                    ticker=sym, source="fmp", report_date=rd,
                    fiscal_year=fy, fiscal_quarter=q,
                    start_utc=start_utc, time_confidence=conf,
                ))
        return out

    def _fetch_confirmed(self, tickers, start, end) -> list[Candidate]:
        out = []
        for a, b in self._chunk_dates(start, end):
            rows = self.http.get_json(
                f"{V4}/earning-calendar-confirmed",
                params={"from": a.isoformat(), "to": b.isoformat(),
                        "apikey": self.api_key},
                allow_cache=False,
            )
            if not isinstance(rows, list):
                continue
            for r in rows:
                sym = (r.get("symbol") or "").upper()
                if sym not in tickers:
                    continue
                try:
                    rd = date.fromisoformat(str(r.get("date"))[:10])
                except (TypeError, ValueError):
                    continue

                explicit = None
                raw_time = (r.get("time") or "").strip()
                if raw_time and ":" in raw_time:
                    try:
                        hh, mm = raw_time.split(":")[:2]
                        explicit = datetime(rd.year, rd.month, rd.day,
                                            int(hh), int(mm), tzinfo=ET)
                    except ValueError:
                        explicit = None

                start_utc, conf = self._resolve(rd, hour_code=raw_time,
                                                explicit=explicit)
                fy, q = self._period(r, rd)
                links = Links()
                url = (r.get("url") or "").strip()
                if url:
                    links.release = url
                out.append(Candidate(
                    ticker=sym, source="fmp_confirmed", report_date=rd,
                    fiscal_year=fy, fiscal_quarter=q,
                    start_utc=start_utc, time_confidence=conf,
                    links=links,
                ))
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _period(row: dict, rd: date) -> tuple[int, int]:
        """Prefer the provider's own period end (fiscalDateEnding) over a guess."""
        fde = row.get("fiscalDateEnding") or row.get("fiscalDateEnded")
        if fde:
            try:
                d = date.fromisoformat(str(fde)[:10])
                return d.year, (d.month - 1) // 3 + 1
            except ValueError:
                pass
        return derive_fiscal_period(rd)
