"""Translate submit-frame elements into cron expressions.

Returns (cron, notes). cron is None when the element cannot be expressed in cron;
notes always explain approximations so the report can surface them.
"""
from __future__ import annotations

import re
from typing import Any

_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _hm(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text).strip())
    if not m:
        raise ValueError(f"expected HH:MM, got {text!r}")
    return int(m.group(1)), int(m.group(2))


def _dow(days: list[str]) -> str:
    return ",".join(str(_DOW[d.lower()[:3]]) for d in days)


def element_to_cron(el: dict[str, Any]) -> tuple[str | None, list[str]]:
    kind = el.get("kind", "")
    notes: list[str] = []
    window = el.get("window")  # {start: "08:00", end: "18:00", days: [mon..fri]}

    if kind == "cron":
        return el["expression"], notes

    if kind == "interval":
        every = int(el["every"])
        unit = el.get("unit", "minutes")
        days = "*"
        hours = "*"
        if window:
            sh, _ = _hm(window["start"])
            eh, em = _hm(window["end"])
            end_h = eh if em else eh - 1
            hours = f"{sh}-{max(end_h, sh)}"
            if window.get("days"):
                days = _dow(window["days"])
            notes.append(f"time window {window['start']}-{window['end']} mapped to hour range {hours} (hour granularity)")
        if unit == "minutes":
            if 60 % every != 0:
                notes.append(f"every {every} minutes does not divide an hour evenly; cron resets each hour")
            return f"*/{every} {hours} * * {days}", notes
        if unit == "hours":
            return f"0 {'*/' + str(every) if hours == '*' else hours} * * {days}", notes
        raise ValueError(f"unknown interval unit {unit!r}")

    if kind == "daily":
        h, m = _hm(el["at"])
        return f"{m} {h} * * *", notes

    if kind == "weekly":
        h, m = _hm(el["at"])
        return f"{m} {h} * * {_dow(el['days'])}", notes

    if kind == "monthly":
        h, m = _hm(el["at"])
        day = el["day"]
        if str(day).lower() == "last":
            notes.append("'last day of month' has no cron equivalent; needs a custom schedule function")
            return None, notes
        return f"{m} {h} {int(day)} * *", notes

    notes.append(f"unsupported submit-frame element kind {kind!r}")
    return None, notes
