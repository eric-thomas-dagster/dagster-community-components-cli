"""Inventory of a RunMyJobs job listing (the HTML report a RunMyJobs report definition writes).

Reads the table (columns such as JobDefinition, Description, Application, Parent1..4, LastModified) and classifies each
definition by name and description only. A common naming convention: JC_ = job chain, JD_ = job definition,
`dbt_cloud`/`dbtCloud` = dbt Cloud, `_Linux`/`_dbt` = dbt Core on a process server, Z_JD_..Email.. = notification
templates, System_Internal_Chain<n> = wrapper chains, OEVAL-* = reports. A listing has no steps, schedules or parameters:
this sizes and triages, it does not convert.
"""
from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser


class _Table(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_listing(html: str) -> list[dict[str, str]]:
    t = _Table()
    t.feed(html)
    rows = [r for r in t.rows if len(r) > 1]
    if not rows:
        return []
    head, body = rows[0], rows[1:]
    return [dict(zip(head, r)) for r in body if len(r) == len(head)]


def classify(row: dict[str, str]) -> dict[str, str]:
    name = row.get("JobDefinition", "")
    desc = row.get("Description", "") or ""
    low = f"{name} {desc}".lower()
    kind = "chain" if name.startswith(("JC_", "Z_JC_")) or re.match(r"(?i)(archived_)?jc_", name) else "definition"
    if name.startswith("System_Internal_Chain"):
        role = "wrapper chain (system generated, folds into its parent)"
    elif name.startswith("OEVAL-") or name == "DEJobDefinitions":
        role = "report (not a job to migrate)"
    elif re.match(r"(?i)z_jd_.*(email|alert|overdue|delay|error|completed|trigger)", name):
        role = "notification template (alert policy, not a job)"
    elif re.search(r"dbt[_ ]?cloud", low):
        role = "dbt Cloud"
    elif re.search(r"(hvr|ldp)[_ ]?refresh", low):
        role = "HVR refresh"
    elif re.search(r"dbt", low):
        role = "dbt Core on a process server"
    elif re.search(r"python|api|script|etl", low):
        role = "python / script"
    elif re.search(r"snowsql|sql", low):
        role = "SQL"
    else:
        role = "other"
    flags = []
    if re.search(r"deprecated|decommission|^old_|^archived_|inactive|_backup|_duplicate|\(copied at", low) or name.startswith(("OLD_", "Archived_")):
        flags.append("likely retired or a copy")
    if name.startswith("Z_") and not role.startswith("notification"):
        flags.append("Z_ prefix (likely parked)")
    path = [row.get(f"Parent{i}", "") for i in (4, 3, 2, 1)]
    return {"name": name, "kind": kind, "role": role, "flags": "; ".join(flags), "application": row.get("Application", ""),
            "path": "/".join(p for p in path if p and p != "null"), "modified": row.get("LastModified", "")}


def summarize(rows: list[dict[str, str]]) -> dict:
    c = [classify(r) for r in rows]
    return {"total": len(c), "by_role": Counter(x["role"] for x in c), "by_kind": Counter(x["kind"] for x in c),
            "retired_or_copy": sum(1 for x in c if "retired" in x["flags"]), "items": c}
