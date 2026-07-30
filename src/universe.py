"""Build the biotech / pharma / life-sciences ticker universe from SEC EDGAR.

Why EDGAR and not a vendor sector tag: every US-listed issuer is assigned an SIC
code by the SEC's Division of Corporation Finance, and the life-sciences codes
(283x, 8731) are exactly the industry we want. It is free, official, has no rate
plan, and it automatically picks up new IPOs the moment they file.

Two calls:
  1. https://www.sec.gov/files/company_tickers_exchange.json  -> ticker/CIK/exchange
  2. https://data.sec.gov/submissions/CIK##########.json      -> SIC, name, website

Step 2 is ~10k requests on a cold start, so the result is cached in
state/universe.json and refreshed weekly.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .httpclient import Http
from .models import Company

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

# SIC codes under the SEC's Office of Life Sciences that map to public
# biotech / pharma / specialty pharma / life-science issuers.
DEFAULT_SIC_CODES = {
    "2833",  # Medicinal chemicals & botanical products
    "2834",  # Pharmaceutical preparations       <- most big & small pharma
    "2835",  # In-vitro & in-vivo diagnostic substances
    "2836",  # Biological products (ex diagnostics)  <- most biotech
    "8731",  # Commercial physical & biological research <- clinical-stage biotech
}

US_EXCHANGES = {"Nasdaq", "NYSE", "NYSE American", "NYSE Arca", "CBOE", "OTC"}


class UniverseBuilder:
    def __init__(self, http: Http, cfg):
        self.http = http
        self.cfg = cfg
        self.sic_codes = set(
            str(s) for s in cfg.universe.get("sic_codes", sorted(DEFAULT_SIC_CODES))
        )
        self.exchanges = set(cfg.universe.get("exchanges", sorted(US_EXCHANGES)))
        self.cache_path = Path(cfg.state_dir) / "universe.json"
        self.max_age_days = int(cfg.universe.get("refresh_days", 7))
        self.workers = int(cfg.universe.get("workers", 8))

    # ------------------------------------------------------------------
    def load(self, force_refresh: bool = False) -> dict[str, Company]:
        cached = self._read_cache()
        if cached and not force_refresh and not self._stale(cached):
            log.info("Universe: using cache (%d companies)", len(cached["companies"]))
            companies = {
                t: Company(**c) for t, c in cached["companies"].items()
            }
        else:
            companies = self._rebuild()
            self._write_cache(companies)

        self._apply_overrides(companies)
        log.info("Universe: %d companies after overrides", len(companies))
        return companies

    # ------------------------------------------------------------------
    def _stale(self, cached: dict) -> bool:
        try:
            built = datetime.fromisoformat(cached["built_at"])
        except (KeyError, ValueError):
            return True
        return datetime.now(timezone.utc) - built > timedelta(days=self.max_age_days)

    def _read_cache(self) -> dict | None:
        if not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, companies: dict[str, Company]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "sic_codes": sorted(self.sic_codes),
            "companies": {t: c.to_dict() for t, c in sorted(companies.items())},
        }
        self.cache_path.write_text(json.dumps(payload, indent=1, sort_keys=True))

    # ------------------------------------------------------------------
    def _rebuild(self) -> dict[str, Company]:
        log.info("Universe: full rebuild from SEC EDGAR (this takes a few minutes)")
        data = self.http.get_json(TICKERS_URL, allow_cache=False)
        if not data:
            raise RuntimeError("Could not fetch SEC company_tickers_exchange.json")

        fields = data["fields"]          # ["cik","name","ticker","exchange"]
        idx = {name: i for i, name in enumerate(fields)}
        rows = data["data"]

        listings: dict[str, tuple[str, str, str]] = {}   # ticker -> (cik, name, exch)
        for row in rows:
            ticker = (row[idx["ticker"]] or "").strip().upper()
            exch = (row[idx["exchange"]] or "").strip()
            if not ticker or exch not in self.exchanges:
                continue
            cik = str(row[idx["cik"]]).zfill(10)
            listings[ticker] = (cik, row[idx["name"]], exch)

        log.info("Universe: %d US-listed tickers to classify", len(listings))

        # De-duplicate by CIK -- one submissions fetch per issuer, not per class.
        by_cik: dict[str, list[str]] = {}
        for ticker, (cik, _, _) in listings.items():
            by_cik.setdefault(cik, []).append(ticker)

        results: dict[str, dict] = {}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._fetch_submission, cik): cik for cik in by_cik}
            done = 0
            for fut in as_completed(futures):
                done += 1
                if done % 500 == 0:
                    log.info("Universe: classified %d/%d (%.0fs)",
                             done, len(by_cik), time.time() - t0)
                cik = futures[fut]
                sub = fut.result()
                if sub:
                    results[cik] = sub

        companies: dict[str, Company] = {}
        for cik, sub in results.items():
            sic = str(sub.get("sic", "")).strip()
            if sic not in self.sic_codes:
                continue
            for ticker in self._primary_tickers(by_cik[cik]):
                _, name, exch = listings[ticker]
                companies[ticker] = Company(
                    ticker=ticker,
                    cik=cik,
                    name=sub.get("name") or name,
                    sic=sic,
                    sic_description=sub.get("sicDescription", ""),
                    exchange=exch,
                    ir_url=self._guess_ir_url(sub),
                )

        log.info("Universe: %d life-sciences tickers matched SIC %s",
                 len(companies), sorted(self.sic_codes))
        return companies

    @staticmethod
    def _primary_tickers(tickers: list[str]) -> list[str]:
        """Collapse an issuer to its common stock.

        EDGAR lists warrants, rights, units and extra share classes under the
        same CIK: Liminatus files LIMN and LIMNW, BriaCell BCTX and BCTXZ. Each
        of those extra symbols would otherwise generate its own duplicate
        earnings event for a call the issuer only holds once.

        An issuer holds ONE earnings call, so we keep ONE ticker. The common
        stock is reliably the shortest symbol; the derivative classes are that
        symbol plus a W/R/U/Z suffix. Where lengths tie (genuine dual-class such
        as A/B shares), alphabetical order picks a stable representative.
        """
        if len(tickers) <= 1:
            return tickers
        return [min(tickers, key=lambda t: (len(t), t))]

    def _fetch_submission(self, cik: str) -> dict | None:
        url = SUBMISSIONS_URL.format(cik=cik)
        data = self.http.get_json(url)
        if not data:
            return None
        # Trim: we only need a handful of fields, and holding 10k full blobs
        # in memory is wasteful.
        return {
            "sic": data.get("sic"),
            "sicDescription": data.get("sicDescription"),
            "name": data.get("name"),
            "website": data.get("website") or "",
            "investorWebsite": data.get("investorWebsite") or "",
        }

    @staticmethod
    def _guess_ir_url(sub: dict) -> str:
        return (sub.get("investorWebsite") or sub.get("website") or "").strip()

    # ------------------------------------------------------------------
    def _apply_overrides(self, companies: dict[str, Company]) -> None:
        """config.yaml can force-add or force-remove tickers.

        Needed because SIC assignment is imperfect: some diagnostics and tools
        companies sit under 3826/3841, and a few holdcos are miscoded.
        """
        for entry in self.cfg.universe.get("include", []) or []:
            if isinstance(entry, str):
                entry = {"ticker": entry}
            t = entry["ticker"].upper()
            existing = companies.get(t)
            companies[t] = Company(
                ticker=t,
                cik=entry.get("cik", existing.cik if existing else ""),
                name=entry.get("name", existing.name if existing else t),
                sic=entry.get("sic", existing.sic if existing else "manual"),
                sic_description="manual include",
                exchange=entry.get("exchange", existing.exchange if existing else ""),
                ir_url=entry.get("ir_url", existing.ir_url if existing else ""),
            )
        for t in self.cfg.universe.get("exclude", []) or []:
            companies.pop(t.upper(), None)
