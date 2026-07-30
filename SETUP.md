# Start to finish: getting this into Outlook

**Total hands-on time: ~35 minutes.** Then you're done permanently.

Work through the phases in order. Each ends with a check — if the check fails,
stop and fix it there. Everything downstream depends on it.

---

## Phase 0 — Before you start (5 min)

You need three things:

**1. A GitHub account.** Free. [github.com/signup](https://github.com/signup).
Use whatever email you like; it doesn't have to be your work address.

**2. The ZIP, extracted.** Right-click → Extract All. You should end up with a
folder called `biopharma-earnings-ics` containing `README.md`, `config.yaml`,
`src/`, `tests/`, `docs/`, and `.github/`.

> If you don't see `.github`, your file explorer is hiding dot-folders.
> On Windows: **View → Show → Hidden items**. This matters later.

**3. An email address you're willing to put in a User-Agent header.** The SEC
requires automated traffic to identify itself with a real contact. It isn't
published anywhere public — it goes in an encrypted GitHub secret and is sent
only to sec.gov.

**Optional, 2 minutes, recommended:** a free
[Finnhub](https://finnhub.io/register) API key. Improves date coverage. If their
free tier gates the calendar endpoint, the build logs it and carries on — so
there's no downside to trying.

**Do not buy anything yet.** Get it working free first; decide about paid data
in Phase 8.

---

## Phase 1 — Create the repository (3 min)

1. github.com → **+** (top right) → **New repository**
2. Name: `biopharma-earnings-ics`
3. **Public** (free Actions minutes; Private works but burns quota)
4. Leave **all three** initialise checkboxes **unchecked** — no README, no
   .gitignore, no license. The ZIP already has them.
5. **Create repository**

✅ **Check:** you land on a page headed *"Quick setup — if you've done this kind
of thing before"*. That's the empty-repo page. Keep this tab open.

---

## Phase 2 — Upload the files (10 min)

> **If you have git installed**, skip to the box at the end of this phase — it's
> five commands and avoids everything below.

### 2a. Drag in the main files

On the empty-repo page, click **uploading an existing file**.

Open your extracted folder and drag these in **together**:

```
README.md   config.yaml   Makefile
requirements.txt   requirements-dev.txt
src/   tests/   docs/   state/
```

Wait for every file to finish (you'll see them listed). Then **Commit changes**.

✅ **Check:** the repo now shows `src`, `tests`, `docs`, `state` folders.
Click into `docs/` — you should see four `.ics` files already there.

### 2b. Create the workflow by hand — don't skip this

**This is the one step that quietly breaks everything if you miss it.**

GitHub's drag-and-drop uploader **silently ignores folders starting with a dot**.
Your `.github/` folder did not upload in step 2a, no matter what you dragged.
Nothing errors. Pages will work. The calendar will simply never update, because
the thing that builds it isn't there.

So:

1. On the repo page: **Add file** → **Create new file**
2. In the filename box, type exactly:
   ```
   .github/workflows/build.yml
   ```
   As you type each `/`, GitHub creates that folder. This is the workaround.
3. Open `.github/workflows/build.yml` from your extracted folder in Notepad
   (or any text editor), select all, copy.
4. Paste into the big editing box on GitHub.
5. **Commit changes**.

✅ **Check — do not continue until this passes:** click the **Actions** tab.
You should see a workflow named **"Build earnings calendar"** listed on the left.
If the tab says *"Get started with GitHub Actions"* instead, the file didn't
land — redo step 2b and check the filename for typos.

> **Git shortcut (replaces all of Phase 2):**
> ```bash
> cd biopharma-earnings-ics
> git init -b main
> git add .
> git commit -m "Initial commit"
> git remote add origin https://github.com/YOURNAME/biopharma-earnings-ics.git
> git push -u origin main
> ```
> Nothing is skipped and there's no dot-folder problem.

---

## Phase 3 — Add your secrets (4 min)

**Settings** (top of repo) → left sidebar **Secrets and variables** →
**Actions** → **New repository secret**.

Add these one at a time:

| Name | Value | Required |
|---|---|---|
| `CONTACT_EMAIL` | your email address | **Yes** |
| `FINNHUB_API_KEY` | your free Finnhub key | Optional |
| `FMP_API_KEY` | leave out for now | Later |

Name must match exactly — case-sensitive, underscores not hyphens.

✅ **Check:** the Actions secrets page lists `CONTACT_EMAIL`. Values show as
`***` — that's correct, they're write-only.

> Without `CONTACT_EMAIL` the SEC returns 403 on every request and the build
> stops immediately with a message telling you exactly this.

---

## Phase 4 — Turn on Pages (2 min)

**Settings** → left sidebar **Pages**.

- **Source:** *Deploy from a branch*
- **Branch:** `main`
- **Folder:** `/docs`  ← not `/root`
- **Save**

✅ **Check:** after ~60 seconds the page shows *"Your site is live at
`https://YOURNAME.github.io/biopharma-earnings-ics/`"*. **Copy that URL** — you
need it twice more.

This works right now because `docs/` already contains four valid (empty)
calendar files. They fill in at Phase 6.

---

## Phase 5 — Point the config at your site (4 min)

In the repo, click `config.yaml` → pencil icon (**Edit this file**).

**Change line 1 of the settings block:**

```yaml
public_base_url: "https://YOURNAME.github.io/biopharma-earnings-ics"
```

**Leave `uid_domain` exactly as it is:**

```yaml
uid_domain: "biopharma-earnings.feeds"
```

> ⚠️ `uid_domain` is baked into the identity of every calendar event. Changing
> it after you've subscribed makes Outlook treat every event as brand new —
> you'd get a complete duplicate set of ~2,500 events with no clean way to
> remove the originals. There is no reason to touch it. It is not a URL and
> doesn't need to resolve to anything.

**While you're here — set your watchlist.** Scroll to the bottom, find the
`watchlist` feed, and replace the ticker list with the names you actually
follow. This feed gets full detail, clickable links and a 15-minute reminder,
so keep it to names worth interrupting you.

```yaml
    tickers:
      - LLY
      - VRTX
      - REGN
      # ...your names
```

**Commit changes** at the bottom.

✅ **Check:** the file view shows your edits.

---

## Phase 6 — First build (~12 min, mostly waiting)

**Actions** tab → **Build earnings calendar** (left sidebar) → **Run workflow**
(grey button, right) → **Run workflow** (green).

Refresh after ~15 seconds. A run appears with a yellow dot. Click into it →
click **build** to watch the log.

**The first run takes about 10 minutes** — it fetches all 7,678 US listings from
SEC EDGAR and classifies each against SIC codes to build your universe. That
result caches for a week, so every later run takes ~2 minutes.

✅ **Check:** green tick, and the run summary shows a table like:

| Feed | Events | Size |
|---|---|---|
| all-1 | ~800 | ~630 KB |
| all-2 | ~830 | ~640 KB |
| all-3 | ~810 | ~620 KB |
| watchlist | ~30 | ~70 KB |

Universe should land near **900–1,000 tickers**.

**If it fails**, open the failed step — the message is usually explicit:
- `CONTACT_EMAIL is not set` → Phase 3
- `Empty universe` → SEC rate-limited you; just re-run
- Provider errors are logged but don't fail the build by design

---

## Phase 7 — Verify the feed before subscribing (3 min)

Open this in your browser (your URL + the filename):

```
https://YOURNAME.github.io/biopharma-earnings-ics/biopharma-earnings-1.ics
```

Your browser will either display text starting `BEGIN:VCALENDAR` or download a
file — either is fine. Open it in Notepad if it downloads.

✅ **Check:** you see real tickers and `SUMMARY:` lines like
`ABBV Q3 2026 Earnings Call`.

**Do this now, not after subscribing.** If the feed is wrong, you want to know
before Outlook caches it.

---

## Phase 8 — Subscribe in Outlook (5 min)

Do this **once, in Outlook on the web** (outlook.office.com) even if you live in
the desktop app. Subscriptions are stored server-side on your mailbox, so they
sync down to Desktop and Mobile automatically. Adding them in Outlook Web is
more reliable than the desktop dialog.

1. Go to **Calendar**
2. Left sidebar → **Add calendar**
3. **Subscribe from web**
4. Paste the first URL, give it a name, pick a colour, **Import**
5. Repeat for all four:

```
https://YOURNAME.github.io/biopharma-earnings-ics/biopharma-earnings-watchlist.ics
https://YOURNAME.github.io/biopharma-earnings-ics/biopharma-earnings-1.ics
https://YOURNAME.github.io/biopharma-earnings-ics/biopharma-earnings-2.ics
https://YOURNAME.github.io/biopharma-earnings-ics/biopharma-earnings-3.ics
```

Suggested: watchlist in a strong colour, the three shards all in one muted
colour — they're one logical calendar, just split for size.

✅ **Check:** four new calendars in your sidebar. Events may take a few hours
to appear — see below.

**That's the last action you ever take.** Everything after this is automatic.

---

## What to expect afterwards

**Events won't appear instantly.** Outlook fetches subscribed calendars on
Microsoft's schedule — documented target is roughly every 3 hours, and it can
exceed 24. There is no refresh button in any Outlook client, and no feed can
change that. If Phase 7 passed, the data is correct and it will arrive.

**Give it 24 hours before concluding anything is wrong.**

Once populated:

| What happens | What you'll see |
|---|---|
| New quarter's dates announced | Events appear on their own |
| Company reschedules | The existing event **moves**. No duplicate. |
| Webcast or deck link published | Event description fills in |
| Replay posted after the call | Back-filled into the same event |
| Call cancelled | Title prefixed `CANCELLED:`, drops off after 30 days |
| Time firms up from "after market close" | `(est. time)` disappears from the title |

Events are marked **free**, not busy — subscribing won't make you look booked
solid or break anyone's scheduling lookup against your calendar.

---

## Should you pay for FMP?

Decide after a week of watching the free version.

**Without it** you get dates from Nasdaq's public endpoint and Finnhub —
generally accurate, roughly 120 days forward, and many events show as all-day
with *"time TBD"* because no free source publishes call times.

**With it** (~$20–50/mo) you get FMP's *confirmed* feed: dates the company has
actually announced, **with exact call times**, plus press-release URLs. That's
the single biggest quality jump available, and it's what turns most "time TBD"
entries into real 4:30pm ET slots.

To add it: create the `FMP_API_KEY` secret (Phase 3), then **Actions → Run
workflow**. Nothing else changes — no re-subscribing, no duplicates. Existing
events just upgrade in place.

---

## If something goes wrong later

| Symptom | Cause | Fix |
|---|---|---|
| Calendar stopped updating | GitHub pauses cron after 60 days of repo inactivity | Actions → Run workflow. Any commit re-arms it. |
| Everything duplicated | `uid_domain` was changed | Revert it, then remove and re-add the calendars once |
| Build fails, red X | Usually a missing secret | Actions → click the run → read the failed step |
| A company shows no webcast link | Bespoke IR site | Add it under `enrichment.ir_urls` in `config.yaml` |
| A name you cover is missing entirely | SIC miscoded | Add it under `universe.include` |
| Too many events cluttering your view | — | Untick the three shard calendars in the sidebar; keep the watchlist |

**Health check any time:** `Actions` tab. Green ticks every couple of hours
means it's working. That's the only monitoring this needs.
