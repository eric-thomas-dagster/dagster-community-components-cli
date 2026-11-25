#!/usr/bin/env python3
"""Turn an Airflow connections export into a `resources.py` section and a `.env.example`, with variable NAMES ONLY.

    airflow connections export --format json connections.json        (or `airflow connections export - --format json`)
    python3 connections_to_resources.py connections.json --out-dir connections_out
    python3 connections_to_resources.py connection_names.txt --out-dir connections_out    (the names-only list from reference/hosted-airflow.md)

Writes into --out-dir: `resources_connections.py` (a `@dg.definitions` function returning one resource per connection, under the connection id), `.env.example` (every
variable name, empty), `connections_summary.json` and `connections_summary.md`.

NO VALUE IS EVER WRITTEN, PRINTED OR LOGGED. From each connection the script keeps two strings, the connection id and its `conn_type`, and drops the rest of the record before anything
is produced: passwords, tokens, `extra` JSON (private keys, secret keys), hosts, logins and URIs are never read into a variable, so no message, summary or exception can carry them. A file
that cannot be read produces an error that names no content. Delete the export when you are done (it holds secrets).

The mapping (`connections.py`, shared with `convert_ir.py`) uses the official resource where one exists (dagster-aws, dagster-snowflake, dagster-gcp, dagster-slack, dagster-ssh,
dagster-databricks, dagster-dbt, dagster-airbyte, dagster-fivetran), environment variables read inside the converted step for SQL, HTTP, SMTP and Slack webhook connections, and a documented
placeholder resource for every other type (listed in the summary). The registry components that fit are named in the summary.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import connections as CN

ID_OK = re.compile(r"^[\w.\-: ]{1,200}$")


def read_names(path: Path) -> list[tuple[str, str | None]]:
    """[(connection id, conn_type or None)] and nothing else. Everything else in the file is dropped at once."""
    try:
        text = path.read_text()
    except OSError:
        raise SystemExit("could not read the input file")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    out: list[tuple[str, str | None]] = []
    if data is None:                                       # a names-only list: one connection id per line
        for ln in text.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and ID_OK.match(ln):
                out.append((ln, None))
    else:
        items = data.get("connections") if isinstance(data, dict) and isinstance(data.get("connections"), list) else None
        if items is not None:                              # the REST API: {"connections": [{"connection_id": ..., "conn_type": ...}]}
            for it in items:
                if isinstance(it, dict):
                    cid, ct = it.get("connection_id") or it.get("conn_id"), it.get("conn_type")
                    if isinstance(cid, str) and ID_OK.match(cid):
                        out.append((cid, ct if isinstance(ct, str) else None))
        elif isinstance(data, dict):                       # `airflow connections export --format json`: {conn_id: {conn_type, host, ...}}
            for cid, rec in data.items():
                if isinstance(cid, str) and ID_OK.match(cid):
                    ct = rec.get("conn_type") if isinstance(rec, dict) else None
                    out.append((cid, ct if isinstance(ct, str) else None))
        elif isinstance(data, list):
            for it in data:
                if isinstance(it, str) and ID_OK.match(it):
                    out.append((it, None))
                elif isinstance(it, dict):
                    cid, ct = it.get("connection_id") or it.get("conn_id"), it.get("conn_type")
                    if isinstance(cid, str) and ID_OK.match(cid):
                        out.append((cid, ct if isinstance(ct, str) else None))
    del text, data
    seen: set[str] = set()
    return [(c, t) for c, t in sorted(out) if not (c in seen or seen.add(c))]


def build(conns: list[tuple[str, str | None]]) -> dict:
    rows = []
    for cid, ctype in conns:
        kind = CN.kind_of(ctype)
        rows.append({"conn_id": cid, "conn_type": ctype, "kind": kind or "unmapped", "resource": CN.resource_entry(kind, cid) if kind else None, "env": CN.env_names(kind, cid),
                     "requirements": CN.KINDS[kind]["requirements"] if kind else [], "registry": CN.KINDS[kind]["registry"] if kind else [], "note": CN.KINDS[kind]["note"] if kind else
                     ("no connection type was given (a names-only list)" if ctype is None else ("dagster-azure's ADLS2Resource fits an Azure storage connection, but its credential shape depends on how the connection authenticates: decide it by hand"
                                                                                       if ctype.lower().startswith(("wasb", "azure")) else f"no mapping for the connection type {ctype!r}")), "in_step": bool(kind and CN.KINDS[kind]["ctor"] is None)})
    return {"connections": rows}


def render_resources(rows: list[dict]) -> str:
    imports = sorted({CN.KINDS[r["kind"]]["import"] for r in rows if r["kind"] in CN.KINDS and CN.KINDS[r["kind"]]["import"]})
    lines = ['"""Resources for the Airflow connections, from `connections_to_resources.py`. Every credential is an environment variable NAME (see .env.example): no value was read from Airflow."""',
             "import dagster as dg", *imports, ""]
    unmapped = [r for r in rows if r["kind"] == "unmapped"]
    if unmapped:
        lines += ["", "class UnmappedConnection(dg.ConfigurableResource):",
                  '    """TODO: this Airflow connection type has no mapping yet. Decide which Dagster resource fits, read its settings from the variables listed in .env.example, and replace this."""',
                  "    conn_id: str", "    conn_type: str = ''", ""]
    entries, notes = [], []
    for r in rows:
        if r["kind"] == "unmapped":
            entries.append(f"        {r['conn_id']!r}: UnmappedConnection(conn_id={r['conn_id']!r}, conn_type={(r['conn_type'] or '')!r}),   # TODO: {r['note']}; variables: {', '.join(r['env'])}")
        elif r["resource"]:
            entries.append(f"        {r['conn_id']!r}: {r['resource']},")
        else:
            notes.append(f"    # {r['conn_id']} ({r['conn_type']}): read inside the converted step from {', '.join(r['env'])}; no resource object")
    lines += ["", "", "@dg.definitions", "def connection_resources() -> dg.Definitions:", *notes, "    return dg.Definitions(resources={", *entries, "    })", ""]
    return "\n".join(lines)


def render_env(rows: list[dict]) -> str:
    lines = ["# Variable NAMES only. Fill the values in your deployment's secret store (`dg plus create env`, or your own); never commit a value.", ""]
    seen: set[str] = set()
    for r in rows:
        lines.append(f"# {r['conn_id']} ({r['conn_type'] or 'type unknown'} -> {r['kind']})" + (f": {r['note']}" if r["note"] else ""))
        for n in r["env"]:
            if n not in seen:
                lines.append(f"{n}=")
                seen.add(n)
        lines.append("")
    return "\n".join(lines)


def render_summary(rows: list[dict]) -> str:
    c = Counter(r["kind"] for r in rows)
    out = ["# Connections summary", "", f"{len(rows)} connection(s): " + ", ".join(f"{k} {n}" for k, n in sorted(c.items())) + ".", "",
           "| Connection | Type | Becomes | Variables | Registry component that fits |", "|---|---|---|---|---|"]
    for r in rows:
        becomes = "environment variables read in the step" if r["in_step"] else ("placeholder resource (TODO)" if r["kind"] == "unmapped" else f"`{r['resource'].split('(')[0]}`")
        out.append(f"| `{r['conn_id']}` | {r['conn_type'] or '?'} | {becomes} | {', '.join(f'`{n}`' for n in r['env'])} | {', '.join(f'`{x}`' for x in r['registry']) or 'none found'} |")
    um = [r for r in rows if r["kind"] == "unmapped"]
    if um:
        out += ["", "## Unmapped (placeholder resource with a TODO)", ""] + [f"- `{r['conn_id']}` ({r['conn_type'] or 'type unknown'}): {r['note']}" for r in um]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("connections_out"))
    a = ap.parse_args(argv)
    conns = read_names(a.input)
    data = build(conns)
    rows = data["connections"]
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "resources_connections.py").write_text(render_resources(rows))
    (a.out_dir / ".env.example").write_text(render_env(rows))
    (a.out_dir / "connections_summary.json").write_text(json.dumps(data, indent=1) + "\n")
    (a.out_dir / "connections_summary.md").write_text(render_summary(rows))
    c = Counter(r["kind"] for r in rows)
    print(f"{len(rows)} connection(s) -> {a.out_dir}: " + ", ".join(f"{k} {n}" for k, n in sorted(c.items())) + ". Variable names only; no value was read.")
    if c.get("unmapped"):
        print("unmapped (placeholder with a TODO): " + ", ".join(r["conn_id"] for r in rows if r["kind"] == "unmapped"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
