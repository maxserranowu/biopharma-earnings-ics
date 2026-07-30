"""End-to-end smoke test: synthetic providers -> state -> ICS -> validator.

Proves the full pipeline without touching a paid API. Run:  python -m tests.smoke
"""
import sys, shutil, tempfile
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, ".")

from src.config import Config
from src.icswriter import render_calendar
from src.models import Candidate, Links
from src.providers.base import resolve_time, derive_fiscal_period
from src.reconcile import Reconciler
from src.store import EventStore
from src.models import Company
from tests.validate import validate

tmp = Path(tempfile.mkdtemp())
shutil.copy("config.yaml", tmp / "config.yaml")
cfg = Config.load(tmp / "config.yaml")
cfg.state_dir, cfg.output_dir = tmp / "state", tmp / "docs"

UNIVERSE = {
    "LLY": ("Eli Lilly and Company", "0000059478"),
    "PFE": ("Pfizer Inc.", "0000078003"),
    "AMGN": ("Amgen Inc.", "0000318154"),
    "MRNA": ("Moderna, Inc.", "0001682852"),
    "SNY": ("Sanofi société anonyme", "0001121404"),   # non-ASCII fold check
    "TINY": ("Tiny Clinical-Stage Biotech Inc.", "0001999999"),
}
companies = {t: Company(ticker=t, cik=c, name=n, ir_url=f"https://investors.{t.lower()}.com")
             for t, (n, c) in UNIVERSE.items()}

def mk(t, d, src, hour=None, fy=None, q=None, links=None):
    start, conf = resolve_time(d, hour, None, cfg.bmo_time_et, cfg.amc_time_et)
    if fy is None: fy, q = derive_fiscal_period(d)
    return Candidate(ticker=t, source=src, report_date=d, fiscal_year=fy,
                     fiscal_quarter=q, start_utc=start, time_confidence=conf,
                     links=links or Links())

T = date.today()
r = Reconciler(cfg, companies)
store = EventStore(cfg.state_dir / "events.json")

print("=== RUN 1: initial discovery ===")
c1 = [
    mk("LLY",  T + timedelta(days=20), "fmp", "amc"),
    mk("PFE",  T + timedelta(days=22), "finnhub", "bmo"),
    mk("AMGN", T + timedelta(days=25), "fmp"),                 # no time -> all-day
    mk("MRNA", T + timedelta(days=30), "nasdaq", "amc"),
    mk("SNY",  T + timedelta(days=18), "fmp", "bmo"),
    mk("TINY", T + timedelta(days=41), "finnhub"),
]
state = r.run(c1, {})
for k in sorted(state): 
    e = state[k]; print(f"  {k:22} seq={e.sequence} {e.status:9} {e.event_date} conf={e.time_confidence}")

print("\n=== RUN 2: idempotent re-run (nothing should change) ===")
seqs = {k: v.sequence for k, v in state.items()}
state = r.run(c1, state)
print("  sequences unchanged:", all(state[k].sequence == v for k, v in seqs.items()))

print("\n=== RUN 3: LLY reschedules +8d, PFE confirms exact time, AMGN gets webcast ===")
lly_new = T + timedelta(days=28)
c3 = [
    mk("LLY", lly_new, "fmp_confirmed", "amc"),
    mk("PFE", T + timedelta(days=22), "fmp_confirmed", "08:30",
       links=Links(release="https://sec.gov/x.htm")),
    mk("AMGN", T + timedelta(days=25), "ir_site", "amc",
       links=Links(webcast="https://investors.amgen.com/webcast",
                   presentation="https://investors.amgen.com/deck.pdf",
                   dial_in="+1 877 555 0100 (conference ID 4471902)")),
    mk("MRNA", T + timedelta(days=30), "nasdaq", "amc"),
    mk("SNY",  T + timedelta(days=18), "fmp", "bmo"),
    mk("TINY", T + timedelta(days=41), "finnhub"),
]
state = r.run(c3, state)
print(f"  event count: {len(state)} (must be 6 -- reschedule must NOT duplicate)")
for k in sorted(state):
    e = state[k]; print(f"  {k:22} seq={e.sequence} {e.status:9} {e.event_date} conf={e.time_confidence}")

print("\n=== RUNS 4-6: TINY vanishes from every provider ===")
c_no_tiny = [c for c in c3 if c.ticker != "TINY"]
for i in range(3):
    state = r.run(c_no_tiny, state)
    tiny = [v for v in state.values() if v.ticker == "TINY"][0]
    print(f"  after miss {i+1}: status={tiny.status} missing_runs={tiny.missing_runs} seq={tiny.sequence}")

print("\n=== RENDER + VALIDATE ===")
cfg.output_dir.mkdir(parents=True, exist_ok=True)
out = cfg.output_dir / "biopharma-earnings.ics"
out.write_text(render_calendar(list(state.values()), calname="Biopharma Earnings Calls",
                               caldesc="smoke test"), encoding="utf-8", newline="")
store.save(state, {"smoke": True})
errs = validate(out)
for e in errs: print("  ERROR", e)
print("\n--- first VEVENT ---")
body = out.read_text()
ev = body.split("BEGIN:VEVENT")[3]
print("BEGIN:VEVENT" + ev.split("END:VEVENT")[0][:1400])
sys.exit(1 if errs else 0)
