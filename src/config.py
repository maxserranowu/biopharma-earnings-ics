"""Typed config loader. Reads config.yaml, expands ${ENV_VAR} references."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


@dataclass
class FeedSpec:
    name: str
    filename: str
    calname: str
    caldesc: str = ""
    tickers: list[str] = field(default_factory=list)   # empty = whole universe
    min_market_cap: float = 0.0
    exclude_tickers: list[str] = field(default_factory=list)
    alarm_minutes: int | None = None
    compact: bool = False
    split: int = 1


@dataclass
class Config:
    raw: dict

    # identity / hosting
    user_agent: str
    uid_domain: str
    public_base_url: str
    output_dir: Path
    state_dir: Path
    cache_dir: Path

    # windowing
    window_days_back: int
    window_days_forward: int
    cancel_grace_days: int
    purge_after_days: int

    # behaviour
    default_duration_minutes: int
    bmo_time_et: str
    amc_time_et: str
    treat_unknown_time_as_all_day: bool

    # providers
    providers: dict
    enrichment: dict
    universe: dict
    feeds: list[FeedSpec]
    overrides: dict

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        data = _expand(yaml.safe_load(Path(path).read_text()))
        root = Path(path).resolve().parent

        def p(key: str, default: str) -> Path:
            return (root / data.get("paths", {}).get(key, default)).resolve()

        feeds = [
            FeedSpec(
                name=f["name"],
                filename=f["filename"],
                calname=f.get("calname", f["name"]),
                caldesc=f.get("caldesc", ""),
                tickers=[t.upper() for t in f.get("tickers", []) or []],
                min_market_cap=float(f.get("min_market_cap", 0) or 0),
                exclude_tickers=[t.upper() for t in f.get("exclude_tickers", []) or []],
                alarm_minutes=f.get("alarm_minutes"),
                compact=bool(f.get("compact", False)),
                split=max(1, int(f.get("split", 1) or 1)),
            )
            for f in data.get("feeds", [])
        ]

        w = data.get("window", {})
        b = data.get("behaviour", {})

        return cls(
            raw=data,
            user_agent=data["user_agent"],
            uid_domain=data.get("uid_domain", "biopharma-earnings.local"),
            public_base_url=data.get("public_base_url", "").rstrip("/"),
            output_dir=p("output_dir", "docs"),
            state_dir=p("state_dir", "state"),
            cache_dir=p("cache_dir", ".cache"),
            window_days_back=int(w.get("days_back", 30)),
            window_days_forward=int(w.get("days_forward", 200)),
            cancel_grace_days=int(w.get("cancel_grace_days", 30)),
            purge_after_days=int(w.get("purge_after_days", 45)),
            default_duration_minutes=int(b.get("default_duration_minutes", 60)),
            bmo_time_et=b.get("bmo_time_et", "08:00"),
            amc_time_et=b.get("amc_time_et", "16:30"),
            treat_unknown_time_as_all_day=bool(b.get("treat_unknown_time_as_all_day", True)),
            providers=data.get("providers", {}),
            enrichment=data.get("enrichment", {}),
            universe=data.get("universe", {}),
            feeds=feeds,
            overrides=data.get("overrides", {}) or {},
        )
