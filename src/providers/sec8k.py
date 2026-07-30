"""SEC EDGAR 8-K Item 2.02 -- the authoritative "it actually happened" signal.

Item 2.02 of Form 8-K is *Results of Operations and Financial Condition*. Every
US issuer files one when it releases quarterly results, and the exhibit attached
to it is the earnings press release itself.

This is retrospective by nature -- an 8-K lands the morning of the call, not
weeks before -- so it cannot drive the forward calendar. What it does, and no
vendor feed does as well, is:

  * confirm the true report date (upgrading a vendor estimate to CONFIRMED)
  * hand back the official earnings-release URL on sec.gov, permanently
  * expose companies that quietly slipped a quarter

Because it is per-issuer, we only call it for companies with an event in the
recent past, which keeps this to a few dozen requests per run.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Iterable

from ..models import Candidate, Links
from .base import Provider, derive_fiscal_period

log = logging.getLogger(__name__)

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_INDEX = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"


class Sec8kProvider(Provider):
    name = "sec_8k"

    def __init__(self, http, cfg, companies: dict | None = None):
        super().__init__(http, cfg)
        self.companies = companies or {}
        self.lookback_days = int(self.settings.get("lookback_days", 21))
        self.workers = int(self.settings.get("workers", 6))

    def fetch(self, tickers: set[str], start: date, end: date) -> Iterable[Candidate]:
        if not self.enabled:
            return []
        today = date.today()
        since = today - timedelta(days=self.lookback_days)

        targets = [
            (t, self.companies[t].cik)
            for t in sorted(tickers)
            if t in self.companies and self.companies[t].cik
        ]
        if not targets:
            return []

        out: list[Candidate] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._scan_company, t, cik, since, today): t
                for t, cik in targets
            }
            for fut in as_completed(futures):
                try:
                    out.extend(fut.result())
                except Exception as e:  # a single bad issuer must not kill the run
                    log.warning("SEC 8-K scan failed for %s: %s", futures[fut], e)
        log.info("SEC 8-K: %d confirmations", len(out))
        return out

    # ------------------------------------------------------------------
    def _scan_company(self, ticker: str, cik: str,
                      since: date, until: date) -> list[Candidate]:
        data = self.http.get_json(SUBMISSIONS.format(cik=cik))
        if not data:
            return []
        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        items = recent.get("items") or []
        accs = recent.get("accessionNumber") or []
        docs = recent.get("primaryDocument") or []

        out = []
        for i, form in enumerate(forms):
            if form != "8-K":
                continue
            if "2.02" not in (items[i] if i < len(items) else ""):
                continue
            try:
                fd = date.fromisoformat(dates[i])
            except (ValueError, IndexError):
                continue
            if not (since <= fd <= until):
                continue

            links = Links()
            try:
                acc = accs[i].replace("-", "")
                links.release = FILING_INDEX.format(
                    cik_int=int(cik), acc_nodash=acc, doc=docs[i]
                )
            except (IndexError, ValueError):
                pass

            fy, q = derive_fiscal_period(fd)
            out.append(Candidate(
                ticker=ticker, source="sec_8k", report_date=fd,
                fiscal_year=fy, fiscal_quarter=q,
                start_utc=None, time_confidence="unknown",
                links=links,
            ))
        return out
