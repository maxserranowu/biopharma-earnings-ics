"""Tests for the behaviours Outlook actually depends on."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.icswriter import escape_text, fold, render_calendar, render_event
from src.models import Candidate, EarningsEvent, Links, make_key
from src.providers.base import derive_fiscal_period, et_to_utc, resolve_time
from src.reconcile import Reconciler


# --------------------------------------------------------------------------
class FakeCfg:
    uid_domain = "test.local"
    default_duration_minutes = 60
    bmo_time_et = "08:00"
    amc_time_et = "16:30"
    treat_unknown_time_as_all_day = True
    purge_after_days = 45
    overrides: dict = {}
    raw = {"behaviour": {"reschedule_match_days": 45, "cancel_after_missing_runs": 3}}


def cand(ticker="LLY", d=None, source="fmp", fy=2026, q=3, hour=None):
    d = d or date(2026, 11, 4)
    start, conf = resolve_time(d, hour, None, "08:00", "16:30")
    return Candidate(ticker=ticker, source=source, report_date=d,
                     fiscal_year=fy, fiscal_quarter=q,
                     start_utc=start, time_confidence=conf)


def reconciler():
    return Reconciler(FakeCfg(), {})


# --------------------------------------------------------------------------
# RFC 5545 primitives
# --------------------------------------------------------------------------
def test_fold_respects_75_octets():
    line = "DESCRIPTION:" + "x" * 500
    for part in fold(line).split("\r\n"):
        assert len(part.encode()) <= 75


def test_fold_never_splits_multibyte_chars():
    line = "SUMMARY:" + "é" * 200          # 2 octets each
    folded = fold(line)
    assert folded.replace("\r\n ", "") == line   # round-trips exactly
    for part in folded.split("\r\n"):
        assert len(part.encode()) <= 75


def test_escape_order_backslash_first():
    assert escape_text(r"a\b;c,d" + "\n") == r"a\\b\;c\,d\n"


# --------------------------------------------------------------------------
# Identity: the core Outlook contract
# --------------------------------------------------------------------------
def test_uid_is_period_based_not_date_based():
    r = reconciler()
    state = r.run([cand()], {})
    ev = state["LLY-FY2026Q3"]
    assert ev.uid == "LLY-FY2026Q3@test.local"
    assert "20261104" not in ev.uid


def test_reschedule_keeps_uid_and_bumps_sequence():
    r = reconciler()
    state = r.run([cand(d=date(2026, 11, 4), hour="amc")], {})
    first = state["LLY-FY2026Q3"]
    assert first.sequence == 0

    # Same call, moved a week out. Provider offers no quarter hint this time.
    moved = cand(d=date(2026, 11, 11), hour="amc")
    moved.fiscal_year, moved.fiscal_quarter = derive_fiscal_period(date(2026, 11, 11))
    state = r.run([moved], state)

    assert len(state) == 1, "reschedule must not create a second event"
    ev = state["LLY-FY2026Q3"]
    assert ev.sequence == 1, "SEQUENCE must advance or Outlook ignores the update"
    assert ev.event_date == date(2026, 11, 11)


def test_idempotent_run_does_not_bump_sequence():
    r = reconciler()
    state = r.run([cand(hour="amc")], {})
    for _ in range(5):
        state = r.run([cand(hour="amc")], state)
    assert state["LLY-FY2026Q3"].sequence == 0


def test_confirmed_source_overrides_estimated_time():
    r = reconciler()
    weak = cand(source="finnhub", hour="amc")           # estimated 16:30
    strong = cand(source="fmp_confirmed")
    strong.start_utc = et_to_utc(date(2026, 11, 4), datetime(2026, 1, 1, 17, 0).time())
    strong.time_confidence = "confirmed"

    state = r.run([weak, strong], {})
    ev = state["LLY-FY2026Q3"]
    assert ev.time_confidence == "confirmed"
    assert ev.status == "CONFIRMED"
    assert ev.start_utc.endswith("22:00:00Z")   # 17:00 EST -> 22:00Z


def test_unknown_time_becomes_all_day():
    state = reconciler().run([cand(hour=None)], {})
    ev = state["LLY-FY2026Q3"]
    assert ev.all_day_date == "2026-11-04"
    assert ev.start_utc is None
    assert "time TBD" in ev.summary


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------
def test_single_provider_outage_does_not_cancel():
    r = reconciler()
    future = date.today() + timedelta(days=40)
    fy, q = derive_fiscal_period(future)
    state = r.run([cand(d=future, fy=fy, q=q, hour="amc")], {})
    key = make_key("LLY", fy, q)

    state = r.run([], state)      # provider down once
    assert state[key].status != "CANCELLED"
    state = r.run([], state)      # twice
    assert state[key].status != "CANCELLED"
    state = r.run([], state)      # three strikes
    assert state[key].status == "CANCELLED"
    assert state[key].sequence >= 1


def test_past_events_are_not_cancelled_when_they_drop_off_feeds():
    r = reconciler()
    past = date.today() - timedelta(days=10)
    fy, q = derive_fiscal_period(past)
    state = r.run([cand(d=past, fy=fy, q=q, hour="amc")], {})
    for _ in range(5):
        state = r.run([], state)
    assert state[make_key("LLY", fy, q)].status != "CANCELLED"


def test_stale_events_are_purged():
    r = reconciler()
    old = date.today() - timedelta(days=200)
    fy, q = derive_fiscal_period(old)
    state = r.run([cand(d=old, fy=fy, q=q)], {})
    assert state == {}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _sample() -> EarningsEvent:
    ev = EarningsEvent(
        key="LLY-FY2026Q3", uid="LLY-FY2026Q3@test.local", ticker="LLY",
        company="Eli Lilly and Company", fiscal_year=2026, fiscal_quarter=3,
        start_utc="2026-11-04T14:00:00Z", end_utc="2026-11-04T15:00:00Z",
        time_confidence="confirmed", status="CONFIRMED", sequence=2,
        first_seen="2026-08-01T00:00:00Z", last_modified="2026-10-01T00:00:00Z",
        links=Links(webcast="https://investor.lilly.com/q3", ir="https://investor.lilly.com").to_dict(),
        sources=["fmp_confirmed", "ir_site"],
    )
    ev.content_hash = ev.material_fingerprint()
    return ev


def test_render_event_has_outlook_essentials():
    out = render_event(_sample())
    assert "UID:LLY-FY2026Q3@test.local" in out
    assert "SEQUENCE:2" in out
    assert "TRANSP:TRANSPARENT" in out          # must not block free/busy
    assert "DTSTART:20261104T140000Z" in out
    assert "X-ALT-DESC;FMTTYPE=text/html" in out
    assert "SUMMARY:LLY Q3 2026 Earnings Call" in out


def test_all_day_dtend_is_exclusive_next_day():
    ev = _sample()
    ev.start_utc = ev.end_utc = None
    ev.all_day_date = "2026-11-04"
    out = render_event(ev)
    assert "DTSTART;VALUE=DATE:20261104" in out
    assert "DTEND;VALUE=DATE:20261105" in out


def test_calendar_is_crlf_and_within_fold_limit():
    ics = render_calendar([_sample()], calname="Test")
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert ics.rstrip("\r\n").endswith("END:VCALENDAR")
    assert "\r\n" in ics
    for line in ics.split("\r\n"):
        assert len(line.encode()) <= 75


def test_cancelled_event_is_labelled():
    ev = _sample()
    ev.status = "CANCELLED"
    out = render_event(ev)
    assert "STATUS:CANCELLED" in out
    assert "CANCELLED: LLY Q3 2026" in out.replace("\r\n ", "")


def test_fingerprint_ignores_last_checked():
    ev = _sample()
    h1 = ev.material_fingerprint()
    ev.last_checked = "2099-01-01T00:00:00Z"
    assert ev.material_fingerprint() == h1


def test_fingerprint_reacts_to_link_change():
    ev = _sample()
    h1 = ev.material_fingerprint()
    ev.links["replay"] = "https://example.com/replay"
    assert ev.material_fingerprint() != h1


# --------------------------------------------------------------------------
def test_fiscal_period_derivation():
    assert derive_fiscal_period(date(2026, 11, 4)) == (2026, 3)
    assert derive_fiscal_period(date(2026, 2, 10)) == (2025, 4)
    assert derive_fiscal_period(date(2026, 5, 1)) == (2026, 1)


def test_dst_is_handled_not_hardcoded():
    summer = et_to_utc(date(2026, 7, 15), datetime(2026, 1, 1, 16, 30).time())
    winter = et_to_utc(date(2026, 12, 15), datetime(2026, 1, 1, 16, 30).time())
    assert summer.hour == 20      # EDT, UTC-4
    assert winter.hour == 21      # EST, UTC-5


def test_literal_clock_time_is_treated_as_confirmed():
    start, conf = resolve_time(date(2026, 11, 4), "16:30:00", None, "08:00", "16:30")
    assert conf == "confirmed"
    assert start.strftime("%H%M") == "2130"      # 16:30 EST -> 21:30Z


def test_company_confirmed_date_yields_confirmed_status():
    r = reconciler()
    state = r.run([cand(source="fmp_confirmed", hour="amc")], {})
    ev = state["LLY-FY2026Q3"]
    assert ev.status == "CONFIRMED"
    assert ev.time_confidence == "estimated"     # honest about the 16:30 guess
    assert "(est. time)" in ev.summary


def test_weak_source_alone_stays_tentative():
    state = reconciler().run([cand(source="nasdaq", hour="amc")], {})
    assert state["LLY-FY2026Q3"].status == "TENTATIVE"


# --------------------------------------------------------------------------
# Universe hygiene
# --------------------------------------------------------------------------
def test_warrants_and_units_are_dropped():
    from src.universe import UniverseBuilder as U
    assert U._primary_tickers(["LIMN", "LIMNW"]) == ["LIMN"]
    assert U._primary_tickers(["BCTX", "BCTXZ"]) == ["BCTX"]
    assert U._primary_tickers(["ABCD", "ABCDU", "ABCDW"]) == ["ABCD"]


def test_single_ticker_issuer_untouched():
    from src.universe import UniverseBuilder as U
    assert U._primary_tickers(["LLY"]) == ["LLY"]
    assert U._primary_tickers([]) == []


def test_dual_class_collapses_to_one_stable_pick():
    from src.universe import UniverseBuilder as U
    picked = U._primary_tickers(["BRKB", "BRKA"])
    assert picked == ["BRKA"]                     # deterministic across runs
    assert len(picked) == 1                       # one call, one event


# --------------------------------------------------------------------------
# Feed sharding
# --------------------------------------------------------------------------
def test_shard_assignment_is_stable_and_in_range():
    from src.build import shard_of
    for t in ("LLY", "PFE", "AMGN", "CABA", "HOWL", "IDYA"):
        a = shard_of(t, 3)
        assert 0 <= a < 3
        assert all(shard_of(t, 3) == a for _ in range(10))   # deterministic


def test_shard_of_is_identity_for_single_feed():
    from src.build import shard_of
    assert shard_of("LLY", 1) == 0


def test_shards_partition_without_loss_or_overlap():
    from src.build import shard_of
    tickers = [f"T{i:04d}" for i in range(2000)]
    buckets = {i: [] for i in range(3)}
    for t in tickers:
        buckets[shard_of(t, 3)].append(t)
    flat = [t for b in buckets.values() for t in b]
    assert sorted(flat) == sorted(tickers)          # nothing lost or duplicated
    assert all(len(b) > 500 for b in buckets.values())   # reasonably balanced


def test_compact_mode_omits_html_body_and_blank_rows():
    ev = _sample()
    full = render_event(ev, compact=False)
    lean = render_event(ev, compact=True)
    assert "X-ALT-DESC" in full and "X-ALT-DESC" not in lean
    assert "Not yet published" in full and "Not yet published" not in lean
    assert len(lean) < len(full) / 2
    assert "UID:LLY-FY2026Q3@test.local" in lean      # identity preserved
    assert "SEQUENCE:2" in lean
    assert "TRANSP:TRANSPARENT" in lean
