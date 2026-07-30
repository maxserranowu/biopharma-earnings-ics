"""Entry point. One run = one complete refresh of every published feed.

    python -m src.build --config config.yaml

The whole pipeline is a pure function of (providers, prior state) -> new state,
which is what makes it safe to run on a dumb cron with no coordination: if a run
fails halfway, the next run reconstructs everything from the committed state
file. There is nothing to repair by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .enrich.discovery import IrUrlDirectory
from .enrich.ir_site import IrEnricher
from .httpclient import Http
from .icswriter import render_calendar
from .models import Links
from .providers import FinnhubProvider, FmpProvider, NasdaqProvider, Sec8kProvider
from .reconcile import Reconciler
from .store import EventStore
from .universe import UniverseBuilder

log = logging.getLogger("build")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def shard_of(ticker: str, n: int) -> int:
    """Assign a ticker to a shard, permanently.

    Hashing the ticker (rather than slicing the sorted list) means a symbol's
    shard NEVER changes as the universe grows. Alphabetical ranges would
    reassign tickers whenever the universe shifted, and a subscriber would see
    the event vanish from one calendar and reappear in another -- exactly the
    duplicate-and-delete churn the whole design exists to avoid.
    """
    if n <= 1:
        return 0
    return int(hashlib.md5(ticker.encode()).hexdigest(), 16) % n


def _shards(feed, selected):
    """Yield (shard_index, events, filename) for a feed."""
    if feed.split <= 1:
        yield 0, selected, feed.filename
        return
    stem, _, ext = feed.filename.rpartition(".")
    buckets: dict[int, list] = {i: [] for i in range(feed.split)}
    for e in selected:
        buckets[shard_of(e.ticker, feed.split)].append(e)
    for i in range(feed.split):
        yield i, buckets[i], f"{stem}-{i + 1}.{ext}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build biopharma earnings ICS feeds")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--refresh-universe", action="store_true",
                    help="Force a full SEC EDGAR universe rebuild")
    ap.add_argument("--no-enrich", action="store_true",
                    help="Skip IR-site scraping (fast dry runs)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do everything except write state and .ics files")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = Config.load(args.config)

    # Fail loudly and early. The SEC rejects requests whose User-Agent lacks a
    # real contact, and the resulting 403 is otherwise very hard to diagnose.
    if "()" in cfg.user_agent or "${" in cfg.user_agent:
        log.error("CONTACT_EMAIL is not set -- SEC EDGAR will reject every "
                  "request. Export CONTACT_EMAIL or edit user_agent in %s.",
                  args.config)
        return 2
    http = Http(cfg.user_agent, cache_dir=str(cfg.cache_dir),
                cache_ttl=int(cfg.raw.get("http", {}).get("cache_ttl_seconds", 3600)))

    # ---- 1. universe -----------------------------------------------------
    companies = UniverseBuilder(http, cfg).load(force_refresh=args.refresh_universe)
    tickers = set(companies)
    if not tickers:
        log.error("Empty universe; aborting rather than publishing an empty feed")
        return 2

    # ---- 2. providers ----------------------------------------------------
    start = date.today() - timedelta(days=cfg.window_days_back)
    end = date.today() + timedelta(days=cfg.window_days_forward)
    log.info("Window %s .. %s over %d tickers", start, end, len(tickers))

    providers = [
        FmpProvider(http, cfg),
        FinnhubProvider(http, cfg),
        NasdaqProvider(http, cfg),
        Sec8kProvider(http, cfg, companies=companies),
    ]
    candidates = []
    for p in providers:
        if not p.enabled:
            log.info("Provider %s disabled", p.name)
            continue
        try:
            candidates.extend(list(p.fetch(tickers, start, end)))
        except Exception as e:
            # One dead vendor must not take the calendar down. The prior state
            # file still holds every event, so the feed degrades rather than
            # emptying out.
            log.exception("Provider %s failed, continuing: %s", p.name, e)

    log.info("Collected %d raw candidates from %d providers",
             len(candidates), sum(1 for p in providers if p.enabled))
    if not candidates:
        log.warning("No candidates this run; existing events will be preserved")

    # ---- 3. reconcile ----------------------------------------------------
    store = EventStore(Path(cfg.state_dir) / "events.json")
    state = store.load()
    before = {k: (v.sequence, v.content_hash) for k, v in state.items()}

    state = Reconciler(cfg, companies).run(candidates, state)

    added = [k for k in state if k not in before]
    changed = [k for k, v in state.items()
               if k in before and before[k][1] != v.content_hash]
    log.info("Reconciled: %d total, %d new, %d updated",
             len(state), len(added), len(changed))

    # ---- 4. enrich -------------------------------------------------------
    if not args.no_enrich:
        # Resolve IR roots only for tickers that actually have a near-term
        # event -- no point discovering IR sites for 900 companies to enrich 40.
        from datetime import date as _date
        soon = sorted({
            e.ticker for e in state.values()
            if abs((e.event_date - _date.today()).days) <= max(
                cfg.enrichment.get("days_ahead", 45),
                cfg.enrichment.get("days_behind", 14))
        })
        IrUrlDirectory(http, cfg).resolve_all(soon, companies)

        enricher = IrEnricher(http, cfg)
        found = enricher.enrich(list(state.values()), companies)
        now_touched = 0
        for key, links in found.items():
            ev = state[key]
            current = Links(**ev.links)
            if current.merge_from(links):
                ev.links = current.to_dict()
                if ev.material_fingerprint() != ev.content_hash:
                    ev.sequence += 1
                    ev.last_modified = ev.last_checked
                    ev.content_hash = ev.material_fingerprint()
                    now_touched += 1
        log.info("Enrichment updated %d events", now_touched)

    # ---- 5. render -------------------------------------------------------
    lo = date.today() - timedelta(days=cfg.window_days_back)
    hi = date.today() + timedelta(days=cfg.window_days_forward)
    grace = date.today() - timedelta(days=cfg.cancel_grace_days)

    stats = {"total_events": len(state), "new": len(added), "updated": len(changed),
             "universe_size": len(companies)}

    outputs = []
    for feed in cfg.feeds:
        selected = [
            e for e in state.values()
            if lo <= e.event_date <= hi
            and e.ticker not in feed.exclude_tickers
            and (not feed.tickers or e.ticker in feed.tickers)
            # Keep cancellations visible for a grace period so subscribers
            # actually see the strike-through before the event vanishes.
            and not (e.status == "CANCELLED" and e.event_date < grace)
        ]
        for shard, subset, filename in _shards(feed, selected):
            ics = render_calendar(
                subset,
                calname=feed.calname if feed.split == 1
                        else f"{feed.calname} ({shard + 1}/{feed.split})",
                caldesc=feed.caldesc or f"{len(subset)} events. Rebuilt automatically.",
                ttl_minutes=int(cfg.raw.get("behaviour", {}).get("ttl_minutes", 120)),
                alarm_minutes=feed.alarm_minutes,
                compact=feed.compact,
            )
            path = Path(cfg.output_dir) / filename
            label = feed.name if feed.split == 1 else f"{feed.name}-{shard + 1}"
            outputs.append((label, path, len(subset), len(ics.encode())))
            if not args.dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(ics, encoding="utf-8", newline="")
            stats[f"feed_{label}"] = len(subset)

    for name, path, n, size in outputs:
        log.info("Feed %-14s %4d events  %6.1f KB  %s", name, n, size / 1024, path)
        if size > 900_000:
            log.warning("Feed %s is %.1f KB. Outlook gets unreliable with very "
                        "large subscribed calendars -- narrow the window or "
                        "split the feed.", name, size / 1024)

    # ---- 6. persist ------------------------------------------------------
    if not args.dry_run:
        store.save(state, stats)
        (Path(cfg.output_dir) / "status.json").write_text(
            json.dumps({**stats, "feeds": [
                {"name": n, "file": p.name, "events": c, "bytes": b}
                for n, p, c, b in outputs
            ]}, indent=1)
        )
    else:
        log.info("Dry run: nothing written")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
