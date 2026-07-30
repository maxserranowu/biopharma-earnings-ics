"""The "database": one JSON file, committed to git on every run.

At the scale this system operates -- roughly 1,000 issuers x 4 quarters, so a
few thousand rows that change a handful of times a day -- a real database would
be pure operational overhead: another thing to provision, back up, patch, pay
for, and lose credentials to. A flat file gives us everything the pipeline
actually needs from a datastore:

  * durable SEQUENCE counters (without which Outlook stops seeing updates)
  * first_seen / last_modified per event
  * cancellation tombstones
  * a complete, diffable audit trail for free, via git history

If coverage ever grows past ~50k events, swap this class for SQLite or DynamoDB;
nothing else in the codebase has to change.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import EarningsEvent

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def load(self) -> dict[str, EarningsEvent]:
        if not self.path.exists():
            log.info("Store: no prior state at %s (cold start)", self.path)
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log.error("Store: unreadable state (%s) -- refusing to clobber it", e)
            raise
        events = {k: EarningsEvent.from_dict(v) for k, v in (raw.get("events") or {}).items()}
        log.info("Store: loaded %d events (schema v%s)", len(events), raw.get("schema_version"))
        return events

    # ------------------------------------------------------------------
    def save(self, events: dict[str, EarningsEvent], stats: dict | None = None) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event_count": len(events),
            "stats": stats or {},
            "events": {k: events[k].to_dict() for k in sorted(events)},
        }
        body = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False)
        # Atomic replace: a half-written state file on a cancelled CI job would
        # reset every SEQUENCE counter and desynchronise every subscriber.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        log.info("Store: wrote %d events to %s", len(events), self.path)
