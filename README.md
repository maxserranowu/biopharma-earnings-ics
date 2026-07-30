# biopharma-earnings-ics

One ICS URL. Subscribe once in Outlook. Never open anything again.

A scheduled job that publishes a self-maintaining earnings calendar for every
US-listed biotech, pharma, specialty pharma and life-sciences issuer. There is
no app, no dashboard, no login, no database server. The calendar is the product.

---

## Quickstart

1. Fork this repo.
2. **Settings → Pages** → Source: *Deploy from a branch*, branch `main`, folder `/docs`.
3. **Settings → Secrets and variables → Actions** → add:
   | Secret | Required | Notes |
   |---|---|---|
   | `CONTACT_EMAIL` | yes | SEC blocks automated traffic without a contact in the User-Agent |
   | `FMP_API_KEY` | recommended | Financial Modeling Prep — best forward coverage + confirmed times |
   | `FINNHUB_API_KEY` | optional | free tier is fine; used as a cross-check |
4. Edit `config.yaml`: set `public_base_url`, and set `uid_domain` **once, permanently**.
5. **Actions → Build earnings calendar → Run workflow.** First run takes ~10 min
   (it classifies ~10,400 US listings against SEC SIC codes); later runs take ~2 min.
6. In Outlook: **Add calendar → Subscribe from web**, four times:
   ```
   .../biopharma-earnings-watchlist.ics
   .../biopharma-earnings-1.ics
   .../biopharma-earnings-2.ics
   .../biopharma-earnings-3.ics
   ```

Done. Nothing else to operate.

### Why four URLs and not one

The universe is ~987 tickers, which over a 230-day window is ~2,500 events —
about **6 MB** as a single file. Outlook publishes no hard size limit for
subscribed calendars, but large ones sync unreliably. So the full feed ships as
three shards of ~630 KB (compact rendering; blank rows and the HTML body
omitted), plus a small watchlist feed that keeps full detail, clickable links
and a 15-minute reminder.

Shard assignment is `md5(ticker) % 3` and is **permanent** — a symbol never
migrates between files, so nothing ever vanishes from one calendar and
reappears in another. Set `split: 1` in `config.yaml` if you narrow the universe
enough to want a single URL.

> Run with no API keys at all and the Nasdaq public endpoint still populates the
> feed — lower fidelity, but it works out of the box.

---

## Architecture

```
                      GitHub Actions  (cron: every 2 hours)
                                  │
    ┌─────────────────────────────┼──────────────────────────────┐
    │                             ▼                              │
    │  1. UNIVERSE      SEC EDGAR company_tickers_exchange.json   │
    │                   + data.sec.gov/submissions → SIC filter   │
    │                   283x / 8731 → ~900 tickers   [cached 7d]  │
    │                             │                              │
    │  2. PROVIDERS     ┌─────────┴──────────┐                    │
    │     (forward)     FMP  Finnhub  Nasdaq  SEC 8-K Item 2.02   │
    │                   └─────────┬──────────┘                    │
    │                             ▼                              │
    │  3. RECONCILE     match on (ticker, FY, quarter)            │
    │                   ± 45-day reschedule window                │
    │                   source-rank merge → bump SEQUENCE         │
    │                             │                              │
    │  4. ENRICH        IR site: JSON-LD → Q4 feed → heuristics   │
    │                   webcast · deck · replay · dial-in         │
    │                             │                              │
    │  5. STATE         state/events.json  (committed to git)     │
    │                             │                              │
    │  6. RENDER        docs/*.ics  ← RFC 5545, validated in CI   │
    └─────────────────────────────┼──────────────────────────────┘
                                  ▼
                      GitHub Pages (static HTTPS)
                                  │
                                  ▼
                  Outlook Desktop / Web / Mobile
                     (server-side pull, ~3h)
```

Nothing is stateful except one JSON file in git. There is no server to patch, no
queue to drain, no secret that expires silently.

---

## Data sources

| Source | Role | Cost | Rank |
|---|---|---|---|
| **SEC EDGAR — SIC classification** | Defines the universe. Picks up new IPOs automatically. | free | — |
| **SEC EDGAR — 8-K Item 2.02** | Official confirmation the call happened + permanent release URL. Retrospective. | free | 90 |
| **Company IR sites** | Webcast, deck, replay, dial-in. JSON-LD / Q4 feed / heuristics. | free | 100 |
| **FMP `earning-calendar-confirmed`** | Company-confirmed dates *and exact times*. The highest-value paid field. | paid | 70 |
| **FMP `earnings-calendar`** | Broad forward coverage, estimated dates. | paid | 50 |
| **Finnhub `/calendar/earnings`** | Cross-check; supplies explicit fiscal quarter/year. | free tier | 45 |
| **Nasdaq public calendar** | Keyless safety net if a paid key lapses. Undocumented endpoint. | free | 40 |

Rank resolves disagreements field by field: an IR-page webcast URL beats a
vendor's, an SEC filing date beats an estimate, a `config.yaml` override beats
everything.

**Why SIC and not a vendor sector tag:** the SEC assigns every issuer an SIC
code, and 2833/2834/2835/2836/8731 map exactly to the industry. It is official,
free, unrate-limited, and a clinical-stage biotech appears the day it files its
S-1. Verified against LLY (2834), PFE (2834), AMGN (2836), MRNA (2836).

---

## Sync and update logic

**Identity is the whole game.** Outlook updates a subscribed event when the
`UID` matches and `SEQUENCE` has advanced; it creates a duplicate when `UID`
changes. So identity is `(ticker, fiscal year, quarter)` — never the date.

```
UID: LLY-FY2026Q3@biopharma-earnings.feeds
```

| Situation | What happens |
|---|---|
| New call announced | New UID, `SEQUENCE:0` → appears |
| Date moves Nov 4 → Nov 11 | Same UID, `DTSTART` updated, `SEQUENCE:1` → **event moves, no duplicate** |
| Webcast/deck link published | Same UID, `SEQUENCE+1` → description updates in place |
| Replay posted after the call | Same UID, `SEQUENCE+1` → back-filled |
| Time firms up from "AMC" to 4:30pm | Confidence upgrades, `(est. time)` drops from title |
| Call cancelled or withdrawn | `STATUS:CANCELLED`, title prefixed, held 30 days, then dropped |
| One vendor has an outage | Nothing changes — 3 consecutive misses required before cancelling |
| Nothing changed at all | `SEQUENCE` does **not** move — no calendar churn |

`SEQUENCE` advances only when a `material_fingerprint()` (times, status, links,
company, notes) changes. `last_checked` is excluded on purpose: bumping on every
run would make Outlook re-render hundreds of events every few hours.

**Unknown times are not invented.** No published time → an all-day event titled
`… (time TBD)`. "before market open" / "after market close" → 08:00 / 16:30 ET,
title marked `(est. time)`. Being visibly approximate beats being confidently wrong.

---

## Outlook design notes

| Property | Value | Why |
|---|---|---|
| `METHOD` | `PUBLISH` | Not `REQUEST` — these are publications, not meeting invites |
| `UID` | period-based | See above |
| `SEQUENCE` | monotonic, change-gated | Outlook ignores updates that don't advance it |
| `TRANSP` | `TRANSPARENT` | **Critical.** Otherwise subscribing marks you busy for hundreds of hours a quarter and wrecks your free/busy |
| `DTSTART` | UTC instants | No `VTIMEZONE` block needed; no DST edge cases; renders in each viewer's local zone |
| all-day | `VALUE=DATE`, `DTEND` = next day | `DTEND` is exclusive for DATE values |
| `X-ALT-DESC` | `FMTTYPE=text/html` | Outlook renders this, making webcast/deck links clickable. Other clients ignore it safely |
| `LOCATION` | webcast URL | Tappable on Outlook Mobile |
| encoding | CRLF, 75-**octet** folds | Folding on character count corrupts any accented company name |

CI runs `python -m tests.validate docs/*.ics` before publishing and **fails the
build** on duplicate UIDs, over-length lines, bare LFs, or missing required
properties. Output is additionally verified to round-trip through the
independent `icalendar` parser.

### The one honest caveat

Outlook controls refresh timing, and you cannot change it. Microsoft documents a
target of roughly every 3 hours, and states it can exceed 24. `REFRESH-INTERVAL`
and `X-PUBLISHED-TTL` are advertised in the feed but Exchange Online does not
reliably honour them. There is no force-refresh button.

The mitigation is the design above: build every 2 hours so the feed is *already*
correct whenever Outlook happens to look. If you need an update within minutes —
a call moved this morning — no ICS subscription in any client can deliver that,
in Outlook or elsewhere.

---

## Hosting

**Recommended: GitHub Actions + GitHub Pages.** Zero servers, zero cost, git
history as a free audit log of every change to every event.

After first deploy, confirm the content type:

```bash
curl -sI https://YOURNAME.github.io/biopharma-earnings-ics/biopharma-earnings.ics \
  | grep -i content-type      # expect: text/calendar
```

If that ever returns `application/octet-stream`, Outlook may refuse the feed.
Two drop-in fixes, in order of simplicity:

| Alternative | When | Effort |
|---|---|---|
| **Cloudflare Pages** | You want explicit control of `Content-Type` and `Cache-Control` via `_headers` | ~10 min, still free |
| **Azure Blob static website** | Corporate policy requires Azure; set the blob content type on upload | ~30 min, pennies/month |
| **AWS Lambda + S3 + CloudFront** | You want the generator inside an existing AWS account | ~1 hr, ~$1/month |

The build is a plain Python module with no GitHub-specific code — moving it to
Azure Functions or a Lambda on an EventBridge schedule means changing the
scheduler and the upload step, nothing else.

**Private feed:** the URL is unguessable but public. If that matters, host on
Cloudflare Pages behind Access, or publish to a randomised filename
(`biopharma-earnings-<uuid>.ics`) and treat the URL as a bearer token.

---

## Schema

No database. `state/events.json`, committed each run:

```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-07-30T18:00:00+00:00",
  "events": {
    "LLY-FY2026Q3": {
      "key": "LLY-FY2026Q3",
      "uid": "LLY-FY2026Q3@biopharma-earnings.feeds",
      "ticker": "LLY", "cik": "0000059478",
      "company": "Eli Lilly and Company",
      "fiscal_year": 2026, "fiscal_quarter": 3,

      "start_utc": "2026-11-04T14:00:00Z",   // null when time unknown
      "end_utc":   "2026-11-04T15:00:00Z",
      "all_day_date": null,                   // set instead when time unknown
      "time_confidence": "confirmed",         // confirmed | estimated | unknown

      "status": "CONFIRMED",                  // CONFIRMED | TENTATIVE | CANCELLED
      "sequence": 3,                          // → ICS SEQUENCE
      "content_hash": "a91f...",              // gates the SEQUENCE bump
      "missing_runs": 0,                      // consecutive absences from all providers

      "first_seen":    "2026-08-01T12:00:00Z",
      "last_modified": "2026-10-14T09:00:00Z",
      "last_checked":  "2026-10-30T18:00:00Z",

      "links": { "webcast": "", "dial_in": "", "ir": "",
                 "release": "", "presentation": "", "replay": "" },
      "sources": ["fmp_confirmed", "ir_site"],
      "notes": ""
    }
  }
}
```

Sidecars: `state/universe.json` (SIC-classified tickers, rebuilt weekly),
`state/ir_urls.json` (resolved IR roots, 90-day TTL).

Past ~50k events, swap `store.py` for SQLite or DynamoDB. Nothing else changes.

---

## Repository layout

```
config.yaml                 every tunable; secrets via ${ENV}
src/
  build.py                  orchestrator — one run = one full refresh
  models.py                 Candidate / EarningsEvent / identity rules
  universe.py               SEC EDGAR SIC-based universe
  reconcile.py              identity, reschedule matching, cancellation
  icswriter.py              RFC 5545 serialiser
  store.py                  atomic JSON state
  httpclient.py             retries, per-host throttling, disk cache
  providers/{fmp,finnhub,nasdaq,sec8k}.py
  enrich/{discovery,ir_site}.py
tests/
  test_pipeline.py          22 unit tests
  validate.py               CI publish gate
  smoke.py                  end-to-end, no API keys needed
.github/workflows/build.yml
```

---

## Operating it

```bash
pip install -r requirements-dev.txt
export CONTACT_EMAIL=you@example.com FMP_API_KEY=... FINNHUB_API_KEY=...

python -m src.build --dry-run --no-enrich -v   # fast sanity check
python -m src.build                            # full run
python -m tests.smoke                          # end-to-end, no keys required
python -m pytest tests/ -q
```

| Symptom | Cause | Fix |
|---|---|---|
| Duplicate events in Outlook | `uid_domain` was changed after subscribing | Revert it; remove and re-add the calendar once |
| Feed stopped updating | Actions cron paused after 60 days repo inactivity | Push any commit, or run the workflow manually |
| SEC returns 403 | `CONTACT_EMAIL` unset → bad User-Agent | Set the secret |
| Feed >900 KB | Window too wide | Lower `window.days_forward`, or split by market cap |
| Some events have no webcast link | IR site is bespoke | Pin it in `enrichment.ir_urls` — the highest-leverage manual knob |

### Where to spend attention

Everything except the last mile is fully automatic. IR-site enrichment is
best-effort by nature — roughly 60–70% of issuers sit on platforms that emit
readable structured data, the rest are bespoke HTML. The floor is deliberate:
**every event always carries an IR link**, so the worst case is one click, never
a dead end. Thirty entries in `enrichment.ir_urls` covering the names you
actually trade will lift deep-link coverage on those to near 100%.

---

## Cost

| Item | Cost |
|---|---|
| GitHub Actions (public repo) | $0 |
| GitHub Pages | $0 |
| SEC EDGAR, Nasdaq, IR sites | $0 |
| Finnhub free tier | $0 |
| FMP (optional but recommended) | ~$20–50/mo |

Runs entirely free at reduced fidelity.
