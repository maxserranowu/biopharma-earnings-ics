"""Gate that runs in CI before anything is published.

A malformed feed is worse than a stale one: Outlook may silently drop the whole
calendar, and the subscriber gets no error. This refuses to publish output that
would not survive a strict RFC 5545 parser.

    python -m tests.validate docs/*.ics
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REQUIRED_CAL = ("BEGIN:VCALENDAR", "VERSION:2.0", "END:VCALENDAR")
REQUIRED_EVENT = ("UID", "DTSTAMP", "DTSTART", "SUMMARY", "SEQUENCE")


def validate(path: Path) -> list[str]:
    errs: list[str] = []
    raw = path.read_bytes()
    text = raw.decode("utf-8")

    # --- line endings -----------------------------------------------------
    if b"\r\n" not in raw:
        errs.append("no CRLF line endings (RFC 5545 3.1)")
    stray = raw.replace(b"\r\n", b"").count(b"\n")
    if stray:
        errs.append(f"{stray} bare LF line ending(s)")

    lines = text.split("\r\n")

    # --- octet length -----------------------------------------------------
    for i, line in enumerate(lines, 1):
        n = len(line.encode("utf-8"))
        if n > 75:
            errs.append(f"line {i}: {n} octets exceeds 75-octet fold limit")

    # --- unfold for structural checks -------------------------------------
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    for req in REQUIRED_CAL:
        if req not in unfolded:
            errs.append(f"missing calendar property: {req}")

    if unfolded.count("BEGIN:VEVENT") != unfolded.count("END:VEVENT"):
        errs.append("unbalanced BEGIN/END:VEVENT")

    # --- per-event --------------------------------------------------------
    uids: Counter[str] = Counter()
    current: dict[str, str] = {}
    inside = False
    events = 0
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            inside, current = True, {}
            continue
        if line == "END:VEVENT":
            events += 1
            for req in REQUIRED_EVENT:
                if req not in current:
                    errs.append(f"event {current.get('UID','?')}: missing {req}")
            uid = current.get("UID", "")
            uids[uid] += 1
            status = current.get("STATUS", "CONFIRMED")
            if status not in ("CONFIRMED", "TENTATIVE", "CANCELLED"):
                errs.append(f"event {uid}: invalid STATUS {status!r}")
            try:
                if int(current.get("SEQUENCE", "0")) < 0:
                    errs.append(f"event {uid}: negative SEQUENCE")
            except ValueError:
                errs.append(f"event {uid}: non-integer SEQUENCE")
            inside = False
            continue
        if inside and ":" in line:
            name = line.split(":", 1)[0].split(";", 1)[0]
            current[name] = line.split(":", 1)[1]

    for uid, n in uids.items():
        if n > 1:
            errs.append(f"duplicate UID x{n}: {uid}  <-- Outlook would duplicate this event")
        if not uid:
            errs.append("event with empty UID")

    size_kb = len(raw) / 1024
    print(f"{path.name}: {events} events, {size_kb:.1f} KB, "
          f"{len(errs)} problem(s)")
    if size_kb > 900:
        print(f"  warning: {size_kb:.0f} KB is large for an Outlook subscribed "
              f"calendar; consider narrowing window.days_forward")
    return errs


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv if Path(p).exists()]
    if not paths:
        print("no .ics files found", file=sys.stderr)
        return 1
    total = 0
    for p in paths:
        errs = validate(p)
        for e in errs:
            print(f"  ERROR {e}", file=sys.stderr)
        total += len(errs)
    if total:
        print(f"\n{total} validation error(s) -- refusing to publish", file=sys.stderr)
        return 1
    print("\nAll feeds valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
