"""Reconciliation: many noisy provider opinions -> one stable event per call.

This module is where the Outlook requirement is actually satisfied. Outlook
updates a subscribed event in place when the UID matches and SEQUENCE has gone
up; it creates a duplicate when the UID changes. So the only thing that must
never change is identity.

Identity = (ticker, fiscal_year, fiscal_quarter), assigned once and then
defended. When a company moves its Q3 call from Nov 4 to Nov 11, the candidate
arriving from the provider looks like a brand-new event. The matching pass below
recognises it as the same call, keeps the original UID, updates DTSTART, and
bumps SEQUENCE -- which is exactly what makes the event move on the user's
calendar instead of doubling.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from .models import (
    SOURCE_RANK,
    Candidate,
    EarningsEvent,
    Links,
    make_key,
)

log = logging.getLogger(__name__)

CONFIDENCE_RANK = {"confirmed": 2, "estimated": 1, "unknown": 0}


class Reconciler:
    def __init__(self, cfg, companies: dict):
        self.cfg = cfg
        self.companies = companies
        self.match_window = int(cfg.raw.get("behaviour", {}).get("reschedule_match_days", 45))
        self.cancel_threshold = int(cfg.raw.get("behaviour", {}).get("cancel_after_missing_runs", 3))

    # ------------------------------------------------------------------
    def run(self, candidates: Iterable[Candidate],
            state: dict[str, EarningsEvent]) -> dict[str, EarningsEvent]:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        today = date.today()

        grouped = self._assign_keys(candidates, state)
        seen_keys: set[str] = set()

        for key, cands in grouped.items():
            seen_keys.add(key)
            prior = state.get(key)
            event = self._build_event(key, cands, prior, now)
            state[key] = event

        # ---- events nobody reported this run -----------------------------
        for key, ev in list(state.items()):
            if key in seen_keys:
                continue
            ev.last_checked = now
            if ev.event_date >= today and ev.status != "CANCELLED":
                ev.missing_runs += 1
                if ev.missing_runs >= self.cancel_threshold:
                    ev.status = "CANCELLED"
                    ev.notes = "No longer appears in any source; treated as cancelled or withdrawn."
                    self._bump(ev, now)
                    log.info("Cancelled %s (missing %d runs)", key, ev.missing_runs)

        self._purge(state, today)
        return state

    # ------------------------------------------------------------------
    def _assign_keys(self, candidates: Iterable[Candidate],
                     state: dict[str, EarningsEvent]) -> dict[str, list[Candidate]]:
        by_ticker: dict[str, list[Candidate]] = {}
        for c in candidates:
            by_ticker.setdefault(c.ticker, []).append(c)

        assigned: dict[str, list[Candidate]] = {}
        for ticker, cands in by_ticker.items():
            existing = {k: v for k, v in state.items() if v.ticker == ticker}
            claimed: set[str] = set()

            # Highest-confidence candidates claim their key first, so a weak
            # source cannot hijack an event that a strong source already owns.
            cands.sort(key=lambda c: (-c.rank, -CONFIDENCE_RANK.get(c.time_confidence, 0)))

            for c in cands:
                key = self._key_for(c, existing, claimed)
                claimed.add(key)
                assigned.setdefault(key, []).append(c)
        return assigned

    def _key_for(self, c: Candidate, existing: dict[str, EarningsEvent],
                 claimed: set[str]) -> str:
        proposed = make_key(c.ticker, c.fiscal_year, c.fiscal_quarter)
        if proposed in existing:
            return proposed

        # Reschedule detection: an unclaimed existing event for the same ticker
        # sitting close to this date is almost certainly the same call.
        best, best_gap = None, self.match_window + 1
        for key, ev in existing.items():
            if key in claimed or ev.status == "CANCELLED":
                continue
            gap = abs((ev.event_date - c.report_date).days)
            if gap < best_gap:
                best, best_gap = key, gap
        if best and best_gap <= self.match_window:
            if best != proposed:
                log.info("Reschedule matched: %s <- candidate dated %s (gap %dd)",
                         best, c.report_date, best_gap)
            return best
        return proposed

    # ------------------------------------------------------------------
    def _build_event(self, key: str, cands: list[Candidate],
                     prior: EarningsEvent | None, now: str) -> EarningsEvent:
        ticker = cands[0].ticker
        company = self.companies.get(ticker)
        fy, q = self._period_from_key(key)

        # Date + time: best source wins, with confirmed beating estimated.
        best = max(cands, key=lambda c: (
            CONFIDENCE_RANK.get(c.time_confidence, 0), c.rank,
        ))
        # Date itself should follow the most authoritative source, which is not
        # always the one carrying a time (SEC 8-K has no time but is definitive).
        best_date = max(cands, key=lambda c: (c.rank, CONFIDENCE_RANK.get(c.time_confidence, 0)))
        report_date = best_date.report_date

        start_utc, end_utc, all_day, confidence = self._times(report_date, best, cands)

        links = Links(**(prior.links if prior else {}))
        for c in sorted(cands, key=lambda x: x.rank):
            links.merge_from(c.links)
        if not links.ir and company and company.ir_url:
            links.ir = company.ir_url
        overrides = self.cfg.overrides.get(key) or self.cfg.overrides.get(ticker) or {}
        for f, v in (overrides.get("links") or {}).items():
            if hasattr(links, f) and v:
                setattr(links, f, v)

        ev = EarningsEvent(
            key=key,
            uid=f"{key}@{self.cfg.uid_domain}",
            ticker=ticker,
            company=(company.name if company else "") or cands[0].company_name or ticker,
            cik=company.cik if company else "",
            fiscal_year=fy,
            fiscal_quarter=q,
            start_utc=start_utc,
            end_utc=end_utc,
            all_day_date=all_day,
            time_confidence=confidence,
            # CONFIRMED reflects whether the *date* is company-published, which
            # is the thing a subscriber acts on. Knowing the date from the
            # company's own IR page but only estimating 16:30 from "after market
            # close" is still a confirmed call -- the SUMMARY carries the
            # "(est. time)" qualifier so nothing is overstated.
            status="CONFIRMED" if (
                confidence == "confirmed"
                or best_date.rank >= SOURCE_RANK["fmp_confirmed"]
            ) else "TENTATIVE",
            links=links.to_dict(),
            sources=sorted({c.source for c in cands}),
            first_seen=prior.first_seen if prior else now,
            last_checked=now,
            missing_runs=0,
        )
        if any(c.cancelled for c in cands):
            ev.status = "CANCELLED"
            ev.notes = "Reported as cancelled by source."
        if overrides.get("status"):
            ev.status = overrides["status"]

        # Carry sequence forward and bump only on material change.
        if prior:
            ev.sequence = prior.sequence
            ev.content_hash = prior.content_hash
            ev.last_modified = prior.last_modified or now
            if ev.material_fingerprint() != prior.content_hash:
                self._bump(ev, now)
        else:
            ev.sequence = 0
            ev.last_modified = now
            ev.content_hash = ev.material_fingerprint()
        return ev

    @staticmethod
    def _bump(ev: EarningsEvent, now: str) -> None:
        ev.sequence += 1
        ev.last_modified = now
        ev.content_hash = ev.material_fingerprint()

    # ------------------------------------------------------------------
    def _times(self, report_date: date, best: Candidate, cands: list[Candidate]):
        """Resolve DTSTART/DTEND, or fall back to an all-day event."""
        # Only accept a time that belongs to the winning date.
        timed = [c for c in cands
                 if c.start_utc and c.report_date == report_date]
        if timed:
            pick = max(timed, key=lambda c: (
                CONFIDENCE_RANK.get(c.time_confidence, 0), c.rank))
            start = pick.start_utc
            end = start + timedelta(minutes=self.cfg.default_duration_minutes)
            return (_iso(start), _iso(end), None, pick.time_confidence)

        if self.cfg.treat_unknown_time_as_all_day:
            return (None, None, report_date.isoformat(), "unknown")

        from .providers.base import et_to_utc, parse_hhmm
        start = et_to_utc(report_date, parse_hhmm(self.cfg.amc_time_et))
        end = start + timedelta(minutes=self.cfg.default_duration_minutes)
        return (_iso(start), _iso(end), None, "unknown")

    @staticmethod
    def _period_from_key(key: str) -> tuple[int, int]:
        tail = key.rsplit("-FY", 1)[1]
        fy, q = tail.split("Q")
        return int(fy), int(q)

    # ------------------------------------------------------------------
    def _purge(self, state: dict[str, EarningsEvent], today: date) -> None:
        cutoff = today - timedelta(days=self.cfg.purge_after_days)
        for key, ev in list(state.items()):
            if ev.event_date < cutoff:
                del state[key]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
