#!/usr/bin/env python3
"""Write a deterministic, facts-only specification of each Airflow DAG, as input to the rewrite route (`rewrite_prompt.py`).

    python3 dag_spec.py ir.json [--report report.json] [--manifest manifest.json] [--parity af_results.json] [--dag DAG_ID ...] [--out-dir rewrite]

Writes `<out-dir>/<dag_id>.spec.md` and `<dag_id>.spec.json` per DAG. Everything in it is read from the IR (what Airflow built), the converter's report (what the converter did, where it
stubbed or degraded), the manifest (units) and a parity result (what Airflow produced): tasks, operator types and resolved arguments, the callables' source, dependencies, what each
task reads and writes, value (XCom) flow, assets in and out, schedule, timezone, catchup, retries, pools and timeouts, callbacks and SLAs, connection and Variable NAMES, trigger rules,
branching and mapping, and for Cosmos the dbt project and selection. The same input gives the same files (no timestamps, sorted lists).

Purpose is stated only as what the facts show and is marked *inferred*. No secret value is written: the value of any argument whose name looks like a credential is replaced, and so is a
string literal assigned to such a name in a callable's source; connection and Variable references are names only.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SECRET_NAME = re.compile(r"pass|secret|token|key|credential|private|auth", re.I)
SECRET_ASSIGN = re.compile(r"""((?:\w*(?:pass|secret|token|key|credential|private|auth)\w*)\s*[:=]\s*)(['"])([^'"\n]{4,})\2""", re.I)
VAR_RX = [re.compile(r"var\.(?:value|json)\.([A-Za-z0-9_\-]+)"), re.compile(r"Variable\.(?:get|set)\(\s*['\"]([^'\"]+)['\"]")]


NOISE_ARGS = {"deps", "expand_input", "op_kwargs_expand_input", "partial_kwargs", "mapped_kwargs"}      # Airflow internals that carry no behaviour


def mask(v: Any, name: str = "") -> Any:
    if isinstance(v, dict) and "xcom_from" in v:                      # a value reference ({"xcom_from": task, "key": "return_value"}), not a credential
        return v
    if isinstance(v, dict):
        return {str(k): mask(x, str(k)) for k, x in v.items() if str(k) not in NOISE_ARGS}
    if isinstance(v, (list, tuple)):
        return [mask(x, name) for x in v]
    if isinstance(v, str) and name and SECRET_NAME.search(name) and not name.endswith("_conn_id") and name != "conn_id":
        return "<redacted: a credential-like argument; its value is not recorded>"
    return v


def mask_source(src: str) -> str:
    return SECRET_ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>{m.group(2)}", src)


def strings(x: Any):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from strings(v)


CONN_CALL = re.compile(r"""(?:get_connection|get_hook|BaseHook\.get_connection)\(\s*['"]([^'"]+)['"]""")


def conn_ids(t: dict[str, Any]) -> list[str]:
    out = set(CONN_CALL.findall((t.get("callable") or {}).get("source") or ""))      # a connection read inside a callable (names only)
    for src in (t.get("args") or {}, t.get("template_fields") or {}):
        for k, v in src.items():
            if (k.endswith("_conn_id") or k == "conn_id") and isinstance(v, str):
                out.add(v)
    return sorted(out)


def var_names(t: dict[str, Any]) -> list[str]:
    found = set()
    for text in strings([t.get("template_fields"), t.get("op_args"), t.get("op_kwargs"), (t.get("callable") or {}).get("source")]):
        for rx in VAR_RX:
            found |= set(rx.findall(text))
    return sorted(found)


def xcom_sources(t: dict[str, Any]) -> list[str]:
    out = set()

    def walk(x):
        if isinstance(x, dict):
            if "xcom_from" in x and x["xcom_from"]:
                out.add(x["xcom_from"])
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    walk([t.get("op_args"), t.get("op_kwargs"), t.get("template_fields"), t.get("args")])
    return sorted(out)


def task_fact(t: dict[str, Any]) -> dict[str, Any]:
    c = t.get("callable") or {}
    return {
        "task_id": t["task_id"], "operator": t["type"], "group": t.get("group"), "upstream": sorted(t.get("upstream") or []), "downstream": sorted(t.get("downstream") or []),
        "retries": t.get("retries") or 0, "retry_delay_s": t.get("retry_delay_s"), "retry_exponential_backoff": bool(t.get("retry_exponential_backoff")), "pool": t.get("pool"), "pool_slots": t.get("pool_slots"),
        "execution_timeout_s": t.get("execution_timeout_s"), "trigger_rule": t.get("trigger_rule"), "owner": t.get("owner"), "queue": t.get("queue"),
        "depends_on_past": bool(t.get("depends_on_past")), "wait_for_downstream": bool(t.get("wait_for_downstream")), "max_active_tis_per_dag": t.get("max_active_tis_per_dag"),
        "is_mapped": bool(t.get("is_mapped")), "expand": t.get("expand"), "is_setup": bool(t.get("is_setup")), "is_teardown": bool(t.get("is_teardown")),
        "sensor": t.get("sensor"), "sla_s": t.get("sla_s"), "callbacks": sorted(t.get("callbacks") or []),
        "arguments": mask({**(t.get("args") or {}), **(t.get("template_fields") or {})}), "callable_args": sorted((t.get("callable_args") or {}).keys()),
        "callable_arg_sources": {k: {"name": (v or {}).get("name"), "source": mask_source((v or {}).get("source") or "")} for k, v in sorted((t.get("callable_arg_info") or {}).items())},
        "callable": ({"name": c.get("name"), "source": mask_source(c.get("source") or ""), "params": c.get("params")} if c else None),
        "op_args": mask(t.get("op_args") or []), "op_kwargs": mask(t.get("op_kwargs") or {}),
        "reads_value_from": xcom_sources(t), "produces_value": bool(t.get("do_xcom_push", True)),
        "outlets": [] if t.get("cosmos") else sorted(o for o in (t.get("outlets") or []) if "object at 0x" not in o), "inlets": sorted(i for i in (t.get("inlets") or []) if "object at 0x" not in i), "connections": conn_ids(t), "variables": var_names(t),
        "cosmos": ({k: v for k, v in t["cosmos"].items() if k in ("kind", "mode", "select", "exclude", "selector", "vars", "full_refresh", "node")} if t.get("cosmos") else None),
    }


def purpose(d: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What the facts support, each marked inferred. Never a business claim."""
    out = []
    ops = sorted({t["operator"] for t in tasks})
    out.append({"statement": f"a {len(tasks)}-task DAG using {', '.join(ops)}", "basis": "the task list", "inferred": "no"})
    sinks = [t["task_id"] for t in tasks if not t["downstream"]]
    if sinks:
        out.append({"statement": f"its final step(s) are {', '.join(sinks)}", "basis": "tasks with no downstream task", "inferred": "no"})
    persisted = sorted({o for t in tasks for o in t["outlets"]})
    if persisted:
        out.append({"statement": f"it produces {', '.join(persisted)}", "basis": "task outlets", "inferred": "no"})
    if any(t["operator"].startswith("Dbt") or t.get("cosmos") for t in tasks):
        out.append({"statement": "it runs a dbt project", "basis": "Cosmos operators", "inferred": "no"})
    if d.get("description"):
        out.append({"statement": f"its description reads: {d['description']}", "basis": "the DAG's description", "inferred": "no"})
    if any(t["sensor"] for t in tasks):
        out.append({"statement": "it waits for an external condition before processing", "basis": "a sensor task", "inferred": "yes"})
    if not persisted and not any(t["connections"] for t in tasks):
        out.append({"statement": "it has no persisted output and uses no connection, so it may be a demonstration or a local computation", "basis": "no outlets, no connection ids", "inferred": "yes"})
    return out


MUST_PRESERVE = [
    "the dependencies between tasks (what must finish before what), including trigger rules and which branch or skip decisions are taken",
    "the schedule (cron or interval) and its timezone, and what `catchup` and `start_date` meant for missed intervals",
    "retries, retry delay and backoff, pools and concurrency limits, execution timeouts",
    "every value that one task passes to another, and every output a task persists (outlets / assets, files, tables, keys)",
    "the failure behaviour of each step (what fails the run, what is skipped, what continues)",
    "the connections and Variables used, by name (values come from the environment, never from the code)",
    "the result of each external side effect (the file written, the message sent, the job started) for the same inputs",
]
ACCEPTABLE = [
    "asset keys and the grouping of tasks into data products (one asset per data product is the target; several tasks may become one asset)",
    "per-task run records, durations and logs are replaced by per-asset runs and materializations",
    "XCom is replaced by asset values (IO manager) or by plain function calls inside an asset",
    "the Airflow UI, task instances and `dag_run` objects have no counterpart",
    "notification, wait and glue tasks may become sensors, hooks, run-status sensors or be folded into the producing asset",
    "operators may be replaced by an official integration or a registry component with the same effect",
]


def build(dag: dict[str, Any], report: dict[str, Any] | None, manifest: dict[str, Any] | None, parity: dict[str, Any] | None, airflow_version: str | None) -> dict[str, Any]:
    tasks = [task_fact(t) for t in sorted(dag["tasks"], key=lambda t: t["task_id"])]
    tt = dag.get("timetable") or {}
    rep = ((report or {}).get("dags") or {}).get(dag["dag_id"])
    units = [{"id": u["id"], "task_id": u.get("task_id"), "class": u.get("classification"), "status": u.get("status")} for u in ((manifest or {}).get("units") or [])
             if u.get("dag_id") == dag["dag_id"]] if manifest else []
    spec: dict[str, Any] = {
        "dag_id": dag["dag_id"], "file": dag.get("file"), "airflow_version": airflow_version, "description": dag.get("description"), "tags": sorted(dag.get("tags") or []),
        "owners": sorted({t["owner"] for t in tasks if t.get("owner")}),
        "schedule": {"kind": tt.get("kind"), "cron": tt.get("cron"), "interval_seconds": tt.get("interval_seconds"), "timezone": tt.get("timezone"), "assets": [a for a in (tt.get("assets") or []) if "object at 0x" not in str(a)] or None, "asset_logic": tt.get("asset_logic") if [a for a in (tt.get("assets") or []) if "object at 0x" not in str(a)] else None,
                     "catchup": dag.get("catchup"), "start_date": dag.get("start_date"), "max_active_runs": dag.get("max_active_runs"), "settings": dag.get("dag_settings"), "has_dag_callbacks": dag.get("has_callbacks")},
        "default_args": mask(dag.get("default_args") or {}), "user_defined_macros": mask(dag.get("user_defined_macros") or {}), "user_defined_macro_functions": dag.get("user_defined_macro_functions") or [], "params": mask(dag.get("params") or {}),
        "tasks": tasks,
        "edges": sorted([u, t["task_id"]] for t in tasks for u in t["upstream"]),
        "value_flow": sorted([s, t["task_id"]] for t in tasks for s in t["reads_value_from"]),
        "assets_out": sorted({o for t in tasks for o in t["outlets"]}), "assets_in": sorted({i for t in tasks for i in t["inlets"]} | {a for a in (tt.get("assets") or []) if "object at 0x" not in str(a)}),
        "connections": sorted({c for t in tasks for c in t["connections"]}), "variables": sorted({v for t in tasks for v in t["variables"]}),
        "cosmos_groups": [{k: g.get(k) for k in ("group", "cosmos_type", "project_name", "render", "execution", "dbt_vars", "env_names")} | {"nodes": sorted(n["unique_id"] for n in g.get("nodes") or [])}
                          | {"profile": {"name": (g.get("profile") or {}).get("profile_name"), "target": (g.get("profile") or {}).get("target_name"), "mapping": ((g.get("profile") or {}).get("mapping") or {}).get("class")}}
                          for g in dag.get("cosmos_groups") or []],
        "purpose": purpose(dag, tasks),
        "must_preserve": MUST_PRESERVE, "acceptable_differences": ACCEPTABLE,
        "converter": None, "manifest_units": units, "parity_baseline": None,
    }
    if rep:
        spec["converter"] = {"status": rep.get("status"), "reason": rep.get("reason"), "output": rep.get("output"), "keys": ["/".join(k) if isinstance(k, list) else k for k in rep.get("keys") or []],
                             "task_keys": rep.get("task_keys"), "stubbed": sorted({c["task"] for c in rep.get("caveats") or [] if c.get("kind") == "stub"}),
                             "caveats": [{"task": c.get("task"), "kind": c.get("kind"), "reason": c.get("reason")} for c in rep.get("caveats") or []], "notes": rep.get("notes"),
                             "asset_checks": rep.get("asset_checks"), "generated_tests": rep.get("generated_tests")}
    if parity and dag["dag_id"] in parity and "error" not in parity[dag["dag_id"]]:
        p = parity[dag["dag_id"]]
        spec["parity_baseline"] = {"state": p.get("state"), "tasks": {tid: {"state": v.get("state"), "value": (v.get("value") or "")[:120]} for tid, v in sorted((p.get("tasks") or {}).items())},
                                   "note": "Airflow's own outcome from dag.test(); a value is a repr truncated to 120 characters and may contain data"}
    return spec


def render_md(s: dict[str, Any]) -> str:
    L = [f"# Specification: Airflow DAG `{s['dag_id']}`", "",
         "Facts read from the IR (what Airflow built). Statements marked *inferred* are not facts. No secret value is in this file: credential-like arguments are redacted and connections and Variables are names only.", "",
         f"- file: `{s['file']}`; Airflow {s['airflow_version']}; tags: {', '.join(s['tags']) or 'none'}; owners: {', '.join(s['owners']) or 'none'}",
         f"- description: {s['description'] or 'none'}", "", "## What it does (from the facts)", ""]
    for p in s["purpose"]:
        L.append(f"- {p['statement']} ({p['basis']}){' *inferred*' if p['inferred'] == 'yes' else ''}")
    sc = s["schedule"]
    if s["user_defined_macros"] or s["user_defined_macro_functions"]:
        L += ["", "## User-defined macros (names a template can use, e.g. `{{ my_json }}`)", "", f"values: `{json.dumps(s['user_defined_macros'], sort_keys=True, default=str)}`; functions (source not recorded): {', '.join(s['user_defined_macro_functions']) or 'none'}"]
    L += ["", "## DAG parameters (`params`, readable as `context[\"params\"]` and `{{ params.x }}`)", "", f"`{json.dumps(s['params'], sort_keys=True, default=str)}`" if s["params"] else "none"]
    L += ["", "## Schedule", "", f"- kind `{sc['kind']}`, cron `{sc['cron']}`, interval seconds {sc['interval_seconds']}, timezone `{sc['timezone']}`", f"- catchup {sc['catchup']}, start date {sc['start_date']}, max active runs {sc['max_active_runs']}",
          f"- triggered by assets: {sc['assets'] or 'none'} ({sc['asset_logic']})", f"- DAG settings: {json.dumps(sc['settings'], sort_keys=True)}; DAG-level callbacks: {sc['has_dag_callbacks']}", "",
          "## Graph", "", f"- edges (upstream -> downstream): {', '.join(f'{a} -> {b}' for a, b in s['edges']) or 'none'}", f"- value flow (producer -> consumer): {', '.join(f'{a} -> {b}' for a, b in s['value_flow']) or 'none'}",
          f"- assets in: {', '.join(s['assets_in']) or 'none'}; assets out: {', '.join(s['assets_out']) or 'none'}", f"- connections (names): {', '.join(s['connections']) or 'none'}; Variables (names): {', '.join(s['variables']) or 'none'}",
          f"- default_args: `{json.dumps(s['default_args'], sort_keys=True)}`", "", "## Tasks", ""]
    for t in s["tasks"]:
        L += [f"### `{t['task_id']}` ({t['operator']})", "",
              f"- upstream: {', '.join(t['upstream']) or 'none'}; trigger rule `{t['trigger_rule']}`; group {t['group']}; owner {t['owner']}",
              f"- retries {t['retries']} (delay {t['retry_delay_s']} s, exponential {t['retry_exponential_backoff']}); pool {t['pool']}{' (its slot count lives in Airflow\'s metadata database and is not in the IR)' if t['pool'] else ''}; timeout {t['execution_timeout_s']} s; SLA {t['sla_s']} s; callbacks {t['callbacks'] or 'none'}",
              f"- reads a value from: {', '.join(t['reads_value_from']) or 'nothing'}; pushes a value: {t['produces_value']}; outlets {t['outlets'] or 'none'}; inlets {t['inlets'] or 'none'}",
              f"- connections: {t['connections'] or 'none'}; Variables: {t['variables'] or 'none'}"]
        if t["is_mapped"] or t["expand"]:
            L.append(f"- dynamic mapping: `{json.dumps(t['expand'], sort_keys=True, default=str)[:400]}`")
        if t["sensor"]:
            L.append(f"- sensor: `{json.dumps(t['sensor'], sort_keys=True)}`")
        if t["cosmos"]:
            L.append(f"- Cosmos: `{json.dumps(t['cosmos'], sort_keys=True, default=str)[:600]}`")
        if t["arguments"]:
            L.append(f"- resolved arguments: `{json.dumps(t['arguments'], sort_keys=True, default=str)[:1200]}`")
        if t["callable_args"]:
            L.append(f"- arguments that hold a function: {', '.join(t['callable_args'])}")
        for k, v in t["callable_arg_sources"].items():
            if v["source"]:
                L += [f"- source of the function given as `{k}` (`{v['name']}`):", "", "```python", v["source"].rstrip(), "```"]
        if t["callable"]:
            L += ["- callable source:", "", "```python", t["callable"]["source"].rstrip(), "```"]
            if t["op_args"] or t["op_kwargs"]:
                L.append(f"- called with args `{json.dumps(t['op_args'], default=str)[:400]}` kwargs `{json.dumps(t['op_kwargs'], default=str)[:400]}`")
        L.append("")
    if s["cosmos_groups"]:
        L += ["## dbt (Cosmos)", ""] + [f"- group `{g['group']}` ({g['cosmos_type']}): project `{g['project_name']}`, render `{json.dumps(g['render'], sort_keys=True, default=str)}`, execution `{json.dumps(g['execution'], sort_keys=True)}`, vars `{json.dumps(g['dbt_vars'], sort_keys=True)}`, "
                                          f"profile `{json.dumps(g['profile'])}`, {len(g['nodes'])} nodes" for g in s["cosmos_groups"]] + [""]
    L += ["## Behaviours that must be preserved", ""] + [f"- {x}" for x in s["must_preserve"]] + ["", "## Differences that are acceptable", ""] + [f"- {x}" for x in s["acceptable_differences"]] + [""]
    cv = s["converter"]
    L += ["## What the converter did (reference draft, not a requirement)", ""]
    if not cv:
        L.append("- no conversion report was given")
    else:
        L += [f"- status: {cv['status']}" + (f" ({cv['reason']})" if cv.get("reason") else ""), f"- converter asset keys: {', '.join(cv['keys']) or 'none'}", f"- tasks it stubbed (loud `AIRFLOW-CAVEAT`): {', '.join(cv['stubbed']) or 'none'}"]
        L += [f"- {c['kind']} caveat on `{c['task'] or 'the DAG'}`: {c['reason']}" for c in cv["caveats"]]
        L += [f"- note: {n}" for n in cv.get("notes") or []]
        if cv.get("asset_checks"):
            L.append(f"- structural checks it generated: {', '.join(c['name'] + ' on ' + c['asset'] for c in cv['asset_checks'])}")
    L += ["", "## Parity baseline", ""]
    pb = s["parity_baseline"]
    L += ["- none: no Airflow parity result was given (the DAG may be ineligible: external systems, long waits)"] if not pb else [f"- Airflow ran it: {pb['state']}"] + [f"- `{k}`: {v['state']}, value `{v['value']}`" for k, v in pb["tasks"].items()] + [f"- {pb['note']}"]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ir", type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--parity", type=Path, help="Airflow results (af_*.json from the parity harness): outcome and value per task")
    ap.add_argument("--dag", action="append", default=[])
    ap.add_argument("--out-dir", type=Path, default=Path("rewrite"))
    a = ap.parse_args(argv)
    ir = json.loads(a.ir.read_text())
    load = lambda p: json.loads(p.read_text()) if p else None          # noqa: E731
    report, manifest, parity = load(a.report), load(a.manifest), load(a.parity)
    a.out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for d in sorted(ir["dags"], key=lambda d: d["dag_id"]):
        if a.dag and d["dag_id"] not in a.dag:
            continue
        spec = build(d, report, manifest, parity, ir.get("airflow_version"))
        (a.out_dir / f"{d['dag_id']}.spec.json").write_text(json.dumps(spec, indent=1, sort_keys=True, default=str) + "\n")
        (a.out_dir / f"{d['dag_id']}.spec.md").write_text(render_md(spec))
        n += 1
    print(f"wrote {n} spec(s) to {a.out_dir} (facts only; no secret value)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
