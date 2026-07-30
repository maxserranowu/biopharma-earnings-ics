"""Best-effort enrichment from the company's own IR site.

Honest framing, because this is the part of the system that cannot be perfect:
there are ~1,000 life-sciences issuers and no common schema across their IR
sites. Roughly 60-70% of them sit on a handful of vendor platforms (Q4, Notified,
Investis, EQS) that emit schema.org JSON-LD or a predictable events feed, and
those we can read reliably. The rest are bespoke.

So the design goal is *graceful degradation*, not full coverage:

  tier 1  JSON-LD schema.org/Event on the IR events page   (high confidence)
  tier 2  Q4-style events JSON feed                         (high confidence)
  tier 3  keyword/anchor heuristics near the matching date  (medium)
  tier 4  nothing found -> the event still carries the IR events page URL

Tier 4 is the floor, and it is why every event in the feed is always useful:
worst case the description contains one click to the page that has the webcast.
Never a dead end.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..models import Links

log = logging.getLogger(__name__)

# Anchor text / href keywords, most specific first.
PATTERNS = {
    "webcast": re.compile(r"\b(webcast|listen to the (live )?call|audio webcast|"
                          r"live call|conference call|event details|register)\b", re.I),
    "presentation": re.compile(r"\b(presentation|slide(s| deck)?|investor deck)\b", re.I),
    "replay": re.compile(r"\b(replay|archived|on demand|on-demand|playback)\b", re.I),
    "release": re.compile(r"\b(press release|earnings release|news release|"
                          r"results announcement|full release)\b", re.I),
}

DIAL_IN_RE = re.compile(
    r"(?:(?:toll[- ]free|domestic|international|dial[- ]?in|us|u\.s\.)[^\n\r]{0,40})?"
    r"(\+?1?[\s.\-(]*\d{3}[\s.\-)]*\d{3}[\s.\-]*\d{4})",
    re.I,
)
CONF_ID_RE = re.compile(r"(?:conference\s*id|access\s*code|passcode|pin)\D{0,10}([0-9]{5,12})", re.I)

EVENTS_PATH_CANDIDATES = [
    "/events", "/events-and-presentations", "/news-events/events",
    "/investors/events", "/investors/news-events/events",
    "/ir/events", "/news-and-events/events", "/events-presentations",
]

Q4_FEED_PATHS = [
    "/feed/Event.svc/GetEventList?serviceDto=%7B%22ViewType%22%3A%22ALL%22%2C%22ViewDate%22%3A%22%22%2C%22RevisionNumber%22%3A1%2C%22LanguageId%22%3A1%2C%22Signature%22%3A%22%22%2C%22ItemCount%22%3A50%2C%22StartIndex%22%3A0%2C%22TagList%22%3A%5B%5D%2C%22IncludeTags%22%3Atrue%7D",
]


class IrEnricher:
    def __init__(self, http, cfg):
        self.http = http
        self.cfg = cfg
        s = cfg.enrichment or {}
        self.enabled = bool(s.get("enabled", True))
        self.workers = int(s.get("workers", 4))
        self.days_ahead = int(s.get("days_ahead", 45))
        self.days_behind = int(s.get("days_behind", 14))
        self.date_tolerance = int(s.get("date_tolerance_days", 1))
        self.url_overrides = {
            k.upper(): v for k, v in (s.get("ir_urls") or {}).items()
        }

    # ------------------------------------------------------------------
    def enrich(self, events: list, companies: dict) -> dict[str, Links]:
        """Return {event_key: Links} for events inside the enrichment window."""
        if not self.enabled:
            return {}

        today = date.today()
        lo, hi = today - timedelta(days=self.days_behind), today + timedelta(days=self.days_ahead)
        targets = [e for e in events
                   if lo <= e.event_date <= hi and e.status != "CANCELLED"]
        if not targets:
            return {}

        log.info("IR enrichment: %d events in window", len(targets))
        results: dict[str, Links] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._enrich_one, e, companies.get(e.ticker)): e.key
                for e in targets
            }
            for fut in as_completed(futures):
                key = futures[fut]
                try:
                    links = fut.result()
                except Exception as exc:
                    log.debug("enrichment failed for %s: %s", key, exc)
                    continue
                if links:
                    results[key] = links

        hits = sum(1 for l in results.values() if l.webcast or l.presentation or l.replay)
        log.info("IR enrichment: %d/%d events got at least one deep link",
                 hits, len(targets))
        return results

    # ------------------------------------------------------------------
    def _enrich_one(self, event, company) -> Optional[Links]:
        base = self._ir_base(event.ticker, company)
        if not base:
            return None

        links = Links()
        page_url, soup = self._find_events_page(base)
        if not page_url:
            links.ir = base
            return links
        links.ir = page_url

        # tier 2: vendor feed (cheap, structured) -- try before parsing HTML
        if self._from_q4_feed(base, event, links):
            return links

        if soup is None:
            return links

        # tier 1: JSON-LD
        if self._from_jsonld(soup, page_url, event, links):
            return links

        # tier 3: heuristics
        self._from_heuristics(soup, page_url, event, links)
        return links

    # ------------------------------------------------------------------
    def _ir_base(self, ticker: str, company) -> str:
        override = self.url_overrides.get(ticker.upper())
        if override:
            return override.rstrip("/")
        url = (getattr(company, "ir_url", "") or "").strip()
        if not url:
            return ""
        if not url.startswith("http"):
            url = "https://" + url
        return url.rstrip("/")

    def _find_events_page(self, base: str):
        """Try the usual IR events paths; fall back to the base URL."""
        parsed = urlparse(base)
        root = f"{parsed.scheme}://{parsed.netloc}"
        for path in EVENTS_PATH_CANDIDATES:
            for candidate in (base + path, root + path):
                html = self.http.get_text(candidate)
                if html and len(html) > 2000:
                    return candidate, BeautifulSoup(html, "html.parser")
        html = self.http.get_text(base)
        if html:
            return base, BeautifulSoup(html, "html.parser")
        return "", None

    # ------------------------------------------------------------------
    def _from_q4_feed(self, base: str, event, links: Links) -> bool:
        parsed = urlparse(base)
        root = f"{parsed.scheme}://{parsed.netloc}"
        for path in Q4_FEED_PATHS:
            data = self.http.get_json(root + path)
            if not isinstance(data, dict):
                continue
            items = (data.get("GetEventListResult") or {}).get("Items") or []
            for it in items:
                raw = it.get("StartDate") or ""
                d = _parse_loose_date(raw)
                if not d or abs((d - event.event_date).days) > self.date_tolerance:
                    continue
                links.webcast = _abs(root, it.get("WebCastLink") or it.get("EventUrl") or "")
                for doc in it.get("Documents") or []:
                    url = _abs(root, doc.get("DocumentPath") or doc.get("Url") or "")
                    title = (doc.get("DocumentTitle") or doc.get("Title") or "")
                    if PATTERNS["presentation"].search(title):
                        links.presentation = url
                    elif PATTERNS["release"].search(title):
                        links.release = url
                if links.webcast or links.presentation:
                    return True
        return False

    # ------------------------------------------------------------------
    def _from_jsonld(self, soup, page_url: str, event, links: Links) -> bool:
        found = False
        for tag in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                blob = json.loads(tag.string or "{}")
            except (ValueError, TypeError):
                continue
            for node in _iter_nodes(blob):
                if not isinstance(node, dict):
                    continue
                if "Event" not in str(node.get("@type", "")):
                    continue
                d = _parse_loose_date(str(node.get("startDate", "")))
                if not d or abs((d - event.event_date).days) > self.date_tolerance:
                    continue
                url = node.get("url") or ""
                loc = node.get("location") or {}
                if isinstance(loc, dict):
                    url = loc.get("url") or url
                if url:
                    links.webcast = _abs(page_url, url)
                    found = True
        return found

    # ------------------------------------------------------------------
    def _from_heuristics(self, soup, page_url: str, event, links: Links) -> None:
        """Look for a block mentioning the event date, then read its links."""
        date_variants = _date_strings(event.event_date)
        blocks = []
        for el in soup.find_all(["li", "article", "tr", "div", "section"]):
            text = " ".join(el.get_text(" ", strip=True).split())[:800]
            if not text or len(text) > 800:
                continue
            if any(v.lower() in text.lower() for v in date_variants):
                blocks.append(el)
        if not blocks:
            return
        block = min(blocks, key=lambda e: len(e.get_text()))

        for a in block.find_all("a", href=True):
            label = f"{a.get_text(' ', strip=True)} {a['href']}"
            for field, pat in PATTERNS.items():
                if getattr(links, field):
                    continue
                if pat.search(label):
                    setattr(links, field, _abs(page_url, a["href"]))

        text = block.get_text(" ", strip=True)
        numbers = DIAL_IN_RE.findall(text)
        conf = CONF_ID_RE.search(text)
        if numbers:
            bits = ", ".join(dict.fromkeys(n.strip() for n in numbers[:2]))
            if conf:
                bits += f" (conference ID {conf.group(1)})"
            links.dial_in = bits


# ---------------------------------------------------------------------------
def _iter_nodes(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_nodes(v)


def _abs(base: str, url: str) -> str:
    url = (url or "").strip()
    if not url or url.startswith(("mailto:", "javascript:", "#")):
        return ""
    return urljoin(base, url)


def _parse_loose_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"/Date\((\d+)", s)   # legacy .NET JSON date
    if m:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date()
    return None


def _date_strings(d: date) -> list[str]:
    return [
        d.strftime("%B %-d, %Y") if hasattr(d, "strftime") else "",
        d.strftime("%b %-d, %Y"),
        d.strftime("%m/%d/%Y"),
        d.strftime("%b %d, %Y"),
        d.strftime("%B %d, %Y"),
        d.isoformat(),
    ]
