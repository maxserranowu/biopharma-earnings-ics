"""Shared HTTP layer: retries, per-host rate limiting, optional disk cache.

The SEC requires a descriptive User-Agent and asks for <=10 req/s. IR sites are
someone else's infrastructure, so we throttle those hard too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# Per-host minimum seconds between requests.
HOST_DELAY = {
    "www.sec.gov": 0.12,
    "data.sec.gov": 0.12,
    "efts.sec.gov": 0.12,
    "api.nasdaq.com": 1.0,
    "_default": 0.25,
}

_last_call: dict[str, float] = {}
_lock = threading.Lock()


def _throttle(url: str) -> None:
    host = urlparse(url).netloc
    delay = HOST_DELAY.get(host, HOST_DELAY["_default"])
    with _lock:
        prev = _last_call.get(host, 0.0)
        wait = delay - (time.monotonic() - prev)
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()


def build_session(user_agent: str) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(
        {
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
    )
    return s


class Http:
    def __init__(self, user_agent: str, cache_dir: Optional[str] = None,
                 cache_ttl: int = 0, timeout: int = 25):
        self.session = build_session(user_agent)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _cache_path(self, url: str) -> Optional[Path]:
        if not self.cache_dir or self.cache_ttl <= 0:
            return None
        h = hashlib.sha256(url.encode()).hexdigest()[:24]
        return self.cache_dir / f"{h}.cache"

    def get(self, url: str, *, headers: dict | None = None,
            params: dict | None = None, allow_cache: bool = True) -> Optional[requests.Response]:
        cp = self._cache_path(url) if (allow_cache and not params) else None
        if cp and cp.exists() and (time.time() - cp.stat().st_mtime) < self.cache_ttl:
            resp = requests.Response()
            resp.status_code = 200
            resp._content = cp.read_bytes()
            resp.url = url
            return resp

        _throttle(url)
        try:
            r = self.session.get(url, headers=headers, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("GET failed %s: %s", url, e)
            return None
        if r.status_code >= 400:
            log.warning("GET %s -> HTTP %s", url, r.status_code)
            return None
        if cp:
            try:
                cp.write_bytes(r.content)
            except OSError:
                pass
        return r

    def get_json(self, url: str, **kw):
        r = self.get(url, **kw)
        if r is None:
            return None
        try:
            return r.json()
        except (ValueError, json.JSONDecodeError):
            log.warning("Non-JSON body from %s", url)
            return None

    def get_text(self, url: str, **kw) -> Optional[str]:
        r = self.get(url, **kw)
        if r is None:
            return None
        r.encoding = r.encoding or "utf-8"
        return r.text
