"""Core domain models.

One rule governs everything downstream: an earnings *call* is identified by
(ticker, fiscal_year, fiscal_quarter) -- NOT by its date. Dates move; the call
does not. That identity becomes the ICS UID, which is why Outlook updates an
event in place instead of creating a duplicate when a company reschedules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional

# ---------------------------------------------------------------------------
# Source precedence. Higher wins when two providers disagree about a field.
# ---------------------------------------------------------------------------
SOURCE_RANK = {
    "ir_site": 100,      # scraped from the company's own IR events page
    "sec_8k": 90,        # SEC EDGAR 8-K Item 2.02 (official, but retrospective)
    "fmp_confirmed": 70, # FMP "earnings confirmed" feed (company-confirmed)
    "fmp": 50,
    "finnhub": 45,
    "nasdaq": 40,
    "manual": 200,       # config.yaml overrides beat everything
}

TIME_CONFIDENCE = ("confirmed", "estimated", "unknown")


@dataclass
class Company:
    ticker: str
    cik: str
    name: str
    sic: str = ""
    sic_description: str = ""
    exchange: str = ""
    ir_url: str = ""
    market_cap: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Links:
    webcast: str = ""
    dial_in: str = ""
    ir: str = ""
    release: str = ""
    presentation: str = ""
    replay: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def merge_from(self, other: "Links") -> bool:
        """Fill blanks from `other`. Returns True if anything changed."""
        changed = False
        for f in ("webcast", "dial_in", "ir", "release", "presentation", "replay"):
            new = (getattr(other, f) or "").strip()
            if new and new != getattr(self, f):
                setattr(self, f, new)
                changed = True
        return changed


@dataclass
class Candidate:
    """A single provider's opinion about one earnings call."""

    ticker: str
    source: str
    report_date: date
    fiscal_year: Optional[int] = None
    fiscal_quarter: Optional[int] = None
    start_utc: Optional[datetime] = None
    time_confidence: str = "unknown"
    cancelled: bool = False
    links: Links = field(default_factory=Links)
    company_name: str = ""

    @property
    def rank(self) -> int:
        return SOURCE_RANK.get(self.source, 0)


@dataclass
class EarningsEvent:
    """The reconciled, persisted record. Serialised into state/events.json."""

    key: str                      # LLY-FY2026Q3
    uid: str                      # LLY-FY2026Q3@<uid_domain>
    ticker: str
    company: str
    cik: str = ""
    fiscal_year: int = 0
    fiscal_quarter: int = 0

    start_utc: Optional[str] = None   # ISO8601 Z, None when all-day
    end_utc: Optional[str] = None
    all_day_date: Optional[str] = None  # YYYY-MM-DD, used when time unknown
    time_confidence: str = "unknown"

    status: str = "CONFIRMED"     # CONFIRMED | TENTATIVE | CANCELLED
    sequence: int = 0
    first_seen: str = ""
    last_modified: str = ""
    last_checked: str = ""
    content_hash: str = ""

    links: dict = field(default_factory=lambda: Links().to_dict())
    sources: list = field(default_factory=list)
    notes: str = ""

    # How many consecutive runs this event has been absent from every provider.
    # Used to distinguish a transient provider outage from a real cancellation.
    missing_runs: int = 0

    # ------------------------------------------------------------------
    def material_fingerprint(self) -> str:
        """Hash of every field a subscriber would notice changing.

        `last_checked` is deliberately excluded -- otherwise every run would
        look like a change and SEQUENCE would climb forever, which makes
        Outlook re-render the whole calendar on every refresh.
        """
        payload = {
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "all_day_date": self.all_day_date,
            "time_confidence": self.time_confidence,
            "status": self.status,
            "company": self.company,
            "links": self.links,
            "notes": self.notes,
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EarningsEvent":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    # ------------------------------------------------------------------
    @property
    def summary(self) -> str:
        base = f"{self.ticker} Q{self.fiscal_quarter} {self.fiscal_year} Earnings Call"
        if self.status == "CANCELLED":
            return f"CANCELLED: {base}"
        if self.all_day_date:
            return f"{base} (time TBD)"
        if self.time_confidence == "estimated":
            return f"{base} (est. time)"
        return base

    @property
    def event_date(self) -> date:
        if self.all_day_date:
            return date.fromisoformat(self.all_day_date)
        return datetime.fromisoformat(self.start_utc.replace("Z", "+00:00")).date()


def make_key(ticker: str, fiscal_year: int, fiscal_quarter: int) -> str:
    return f"{ticker.upper()}-FY{fiscal_year}Q{fiscal_quarter}"
