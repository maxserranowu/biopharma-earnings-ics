"""Resolve each ticker to an IR website root, and remember the answer.

EDGAR turns out not to help here: the `website` and `investorWebsite` fields on
data.sec.gov/submissions are empty for essentially every issuer (verified
against LLY, PFE, AMGN, MRNA). So IR roots are resolved in this order:

  1. config.yaml enrichment.ir_urls   -- hand-pinned, always wins
  2. FMP company profile `website`    -- automatic, covers the whole universe
  3. common IR subdomain patterns     -- investors.X / ir.X / investor.X

Resolution is expensive and almost perfectly stable, so results live in
state/ir_urls.json and are only re-checked when they are missing or stale. A
ticker that fails resolution is retried on a later run, not hammered every run.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

FMP_PROFILE = "https://financialmodelingprep.com/stable/profile"
IR_PREFIXES = ("investors.", "investor.", "ir.")


class IrUrlDirectory:
    def __init__(self, http, cfg):
        self.http = http
        self.cfg = cfg
        s = cfg.enrichment or {}
        self.pinned = {k.upper(): v.rstrip("/") for k, v in (s.get("ir_urls") or {}).items()}
        self.path = Path(cfg.state_dir) / "ir_urls.json"
        self.ttl_days = int(s.get("ir_url_ttl_days", 90))
        self.retry_days = int(s.get("ir_url_retry_days", 14))
        self.workers = int(s.get("discovery_workers", 4))
        self.fmp_key = (cfg.providers.get("fmp") or {}).get("api_key", "")
        self.data = self._load()

    # ------------------------------------------------------------------
    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True))

    # ------------------------------------------------------------------
    def resolve_all(self, tickers: list[str], companies: dict) -> None:
        """Fill company.ir_url in place for the tickers given."""
        todo = [t for t in tickers if self._needs_lookup(t)]
        if todo:
            log.info("IR discovery: resolving %d tickers", len(todo))
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(self._discover, t): t for t in todo}
                for fut in as_completed(futures):
                    t = futures[fut]
                    try:
                        url = fut.result()
                    except Exception as e:
                        log.debug("IR discovery failed %s: %s", t, e)
                        url = ""
                    self.data[t] = {
                        "url": url,
                        "checked_at": _now(),
                        "source": "pinned" if t in self.pinned else ("fmp" if url else "none"),
                    }
            self.save()

        for t in tickers:
            url = self.get(t)
            if url and t in companies:
                companies[t].ir_url = url
        resolved = sum(1 for t in tickers if self.get(t))
        log.info("IR discovery: %d/%d tickers have an IR root", resolved, len(tickers))

    # ------------------------------------------------------------------
    def get(self, ticker: str) -> str:
        t = ticker.upper()
        if t in self.pinned:
            return self.pinned[t]
        return (self.data.get(t) or {}).get("url", "")

    def _needs_lookup(self, ticker: str) -> bool:
        t = ticker.upper()
        if t in self.pinned:
            return False
        rec = self.data.get(t)
        if not rec:
            return True
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(rec["checked_at"])
        except (KeyError, ValueError):
            return True
        limit = self.ttl_days if rec.get("url") else self.retry_days
        return age > timedelta(days=limit)

    # ------------------------------------------------------------------
    def _discover(self, ticker: str) -> str:
        site = self._from_fmp(ticker)
        if not site:
            return ""
        host = urlparse(site if site.startswith("http") else "https://" + site).netloc
        host = host.replace("www.", "")
        if not host:
            return ""
        for prefix in IR_PREFIXES:
            candidate = f"https://{prefix}{host}"
            r = self.http.get(candidate)
            if r is not None:
                return candidate
        return f"https://{host}"

    def _from_fmp(self, ticker: str) -> str:
        if not self.fmp_key:
            return ""
        rows = self.http.get_json(FMP_PROFILE,
                                  params={"symbol": ticker, "apikey": self.fmp_key},
                                  allow_cache=False)
        if isinstance(rows, list) and rows:
            return (rows[0].get("website") or "").strip()
        return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
