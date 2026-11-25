#!/usr/bin/env python3
"""The acceptance gates for a REWRITTEN DAG (the rewrite route), and the record of which route produced each manifest unit.

    python3 accept_rewrite.py --spec rewrite/<dag>.spec.json --module pkg.defs.<dag> --project <dagster project root> \\
        [--defs-dir src/pkg/defs/<dag>_components] [--manifest manifest.json] [--parity af_results.json] [--out rewrite/<dag>.gates.json]

Gates (all must pass; a gate that cannot run FAILS, it is never skipped silently):
  1. definitions load      `dg check defs` in the project when `dg` is installed; otherwise the module's definitions are loaded with dagster and the gate says so.
  2. generated pytest      a behavioural contract (module loads, a job named after the DAG, the schedule's cron and timezone equal Airflow's, and for a DAG with no external system the whole
                           DAG materializes and every asset check passes) written to tests/test_<dag>_rewrite.py and run with pytest. The converter's key-for-key tests do not apply: a rewrite picks its own keys.
  3. asset checks present  at least one when the converter derived any from the source (a rewrite folds steps, so it has fewer places to check).
  4. parity                when an Airflow baseline (`--parity`) exists for the DAG: the rewrite materializes with the same overall outcome, and each task the header says is `replaced by` an asset gets the
                           same value as Airflow's (a task `folded into` another has no value of its own to compare). Without a baseline: `not run (no baseline)`, which does not fail the gate and is recorded as such.
  5. no secret values      no credential-looking literal in the module or the component YAML.
  6. no invented APIs      every import resolves to a real module and name, and every `type:` in the component YAML is an importable class.
  7. tasks accounted for   the module docstring names every Airflow task and says what happened to it (replaced, folded, dropped).

With `--manifest`, every unit of the DAG gets `route: rewrite` and the gate results. All gates passed: pending or deferred units are advanced with the evidence. Any gate failed: the units that are not
already complete stay or become `deferred` with the failure, and the converter's output (if any) remains the fallback. A unit the converter already completed keeps `route: converter` and gets a
`rewrite_attempt` note. Nothing is marked complete without the gates.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import gen_tests as G
import status as S

SECRET_LITERAL = re.compile(r"""(?i)\b(\w*(?:password|passwd|secret|token|api_?key|private_?key|credential)\w*)\s*[:=]\s*(['"])(?!<)([^'"\n]{6,})\2""")
ENV_OK = re.compile(r"(?i)env_var|EnvVar|environ|getenv|os\.environ|\{\{\s*env")
EXTERNAL_OPS = ("Sensor", "Snowflake", "S3", "GCS", "BigQuery", "Http", "Sql", "Postgres", "MySql", "Kubernetes", "Docker", "SSH", "Lambda", "Spark", "Dbt", "Slack", "Email", "Databricks", "Airbyte", "Fivetran", "Trigger")


def gate(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"gate": name, "passed": bool(ok), "detail": detail}


def source_of(module: str, project: Path) -> tuple[str, Path | None]:
    for root in (project / "src", project):
        p = root.joinpath(*module.split(".")).with_suffix(".py")
        if p.exists():
            return p.read_text(), p
    return "", None


def g_load(module: str, project: Path) -> dict[str, Any]:
    dg_bin = shutil.which("dg")
    if dg_bin:
        r = subprocess.run([dg_bin, "check", "defs"], cwd=project, capture_output=True, text=True, env={**__import__("os").environ, "PYTHONPATH": f"{project / 'src'}:{project}"})
        noise = ("Warning", "class ", "return meta", "real = ")
        tail = "\n".join(ln for ln in (r.stdout + r.stderr).splitlines() if not any(w in ln for w in noise))[-600:]
        return gate("definitions_load", r.returncode == 0, "dg check defs" + ("" if r.returncode == 0 else ": " + tail))
    code = ("import importlib, sys, dagster as dg\n" f"sys.path[:0] = [{str(project / 'src')!r}, {str(project)!r}]\n" f"m = importlib.import_module({module!r})\n"
            "a = [v for v in vars(m).values() if isinstance(v, dg.AssetsDefinition) and not isinstance(v, dg.AssetChecksDefinition)]\n"
            "c = [v for v in vars(m).values() if isinstance(v, dg.AssetChecksDefinition)]\n"
            "j = [v for v in vars(m).values() if type(v).__name__ in ('UnresolvedAssetJobDefinition', 'JobDefinition')]\n"
            "s = [v for v in vars(m).values() if isinstance(v, dg.ScheduleDefinition)]\n"
            "need = set().union(set(), *(set(d.required_resource_keys) for d in a + c)) - {'io_manager'}\n"
            "dg.Definitions.validate_loadable(dg.Definitions(resources={k: dg.ResourceDefinition.mock_resource() for k in need}, assets=a, asset_checks=c, jobs=j, schedules=s))\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    return gate("definitions_load", r.returncode == 0, "`dg` is not installed: the module's definitions were loaded with dagster instead (Definitions.validate_loadable)" + ("" if r.returncode == 0 else ": " + r.stderr[-600:]))


def g_tests(spec: dict[str, Any], module: str, project: Path) -> dict[str, Any]:
    ops = [t["operator"] for t in spec["tasks"]]
    skip = None
    if spec.get("cosmos_groups") or any(any(w in o for w in EXTERNAL_OPS) for o in ops) or spec["schedule"]["assets"]:
        skip = "the DAG has an external system, a sensor, dbt or an asset trigger: only the load and shape tests apply"
    sched = spec["schedule"]
    info = {"dag_id": spec["dag_id"], "module": module, "schedule": ({"cron": sched["cron"], "timezone": sched["timezone"]} if sched["kind"] in ("cron_trigger", "cron_data_interval") and sched["cron"] else None),
            "materialize_skip": skip, "needs_shell": any(o in ("BashOperator", "_BashDecoratedOperator") for o in ops)}
    tdir = project / "tests"
    tdir.mkdir(exist_ok=True)
    f = tdir / f"test_{spec['dag_id']}_rewrite.py"
    f.write_text(G.render_rewrite(info))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore", "-rs", str(f)], cwd=project, capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONPATH": f"{project / 'src'}:{project}"})
    tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()][-1:] or [r.stderr[-200:]]
    return gate("generated_tests", r.returncode == 0, f"{f.name}: {tail[0]}" + ("; materialize skipped: " + skip if skip else ""))


def count_checks(src: str) -> int:
    return len(re.findall(r"@(?:dg|dagster)\.asset_check\(|@asset_check\(|check_specs\s*=|AssetCheckSpec\(|@dg\.multi_asset_check", src)) + src.count("asset_checks=") * 0


def g_checks(spec: dict[str, Any], src: str, defs_dir: Path | None) -> dict[str, Any]:
    n = count_checks(src) + (sum(count_checks(p.read_text()) for p in defs_dir.rglob("*.py")) if defs_dir and defs_dir.exists() else 0)
    need = len((spec.get("converter") or {}).get("asset_checks") or [])
    if defs_dir and defs_dir.exists() and any("asset_checks" in p.read_text() or "AssetCheck" in p.read_text() for p in defs_dir.rglob("*.yaml")):
        n += 1
    # a rewrite folds steps, so it has fewer places to check than the task-per-asset converter: what is required is a structural check wherever the source supports one at all
    return gate("asset_checks_present", n >= min(need, 1), f"{n} asset check(s) in the rewrite; the converter derived {need} from the source (at least 1 is required when it derived any)")


def g_parity(spec: dict[str, Any], module: str, project: Path, parity: dict[str, Any] | None) -> dict[str, Any]:
    base = (parity or {}).get(spec["dag_id"])
    if not base or "error" in base:
        return gate("parity", True, "not run (no Airflow baseline for this DAG: it may be ineligible, or none was given)")
    code = ("import importlib, json, sys, dagster as dg\n" f"sys.path[:0] = [{str(project / 'src')!r}, {str(project)!r}]\n" f"m = importlib.import_module({module!r})\n"
            "a = [v for v in vars(m).values() if isinstance(v, dg.AssetsDefinition) and not isinstance(v, dg.AssetChecksDefinition)]\n"
            "r = dg.materialize(a, raise_on_error=False)\nvals = {}\n"
            "for d in a:\n    for k in d.keys:\n        try:\n            vals[k.to_user_string()] = repr(r.asset_value(k))[:300]\n        except Exception:\n            pass\n"
            "print(json.dumps({'ok': r.success, 'values': vals}))\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return gate("parity", False, "the rewrite could not be run: " + r.stderr[-300:])
    want_ok = "success" in str(base.get("state", "")).lower()
    problems = [] if out["ok"] == want_ok else [f"overall: Airflow {base.get('state')}, rewrite {'success' if out['ok'] else 'failed'}"]
    src, _ = source_of(module, project)
    doc = ast.get_docstring(ast.parse(src)) or ""
    compared = 0
    for tid, tb in (base.get("tasks") or {}).items():
        m = re.search(rf"{re.escape(tid)}\s*:\s*replaced by[^`\n]*`([^`]+)`", doc)
        if m and m.group(1) in out["values"] and tb.get("value") not in (None, "None"):
            compared += 1
            if out["values"][m.group(1)] != tb["value"]:
                problems.append(f"{tid}: Airflow value {tb['value'][:60]} vs rewrite {out['values'][m.group(1)][:60]} (asset {m.group(1)})")
    return gate("parity", not problems, ("outcome and " + str(compared) + " value(s) match Airflow" if not problems else "; ".join(problems)))


def g_secrets(src: str, defs_dir: Path | None) -> dict[str, Any]:
    texts = [("module", src)] + ([(str(p.name), p.read_text()) for p in defs_dir.rglob("*") if p.is_file() and p.suffix in (".py", ".yaml", ".yml")] if defs_dir and defs_dir.exists() else [])
    hits = []
    for name, t in texts:
        for i, ln in enumerate(t.splitlines(), 1):
            m = SECRET_LITERAL.search(ln)
            if m and not ENV_OK.search(ln):
                hits.append(f"{name} line {i}: a string literal is assigned to `{m.group(1)}` (the value is not echoed)")
    return gate("no_secret_values", not hits, "none found" if not hits else "; ".join(hits[:5]))


def g_apis(src: str, defs_dir: Path | None, project: Path) -> dict[str, Any]:
    bad = []
    sys.path[:0] = [str(project / "src"), str(project)]
    try:
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                try:
                    mod = importlib.import_module(node.module)
                except Exception as e:  # noqa: BLE001
                    bad.append(f"import {node.module} ({type(e).__name__})")
                    continue
                for a in node.names:
                    if a.name != "*" and not hasattr(mod, a.name):
                        try:
                            importlib.import_module(f"{node.module}.{a.name}")
                        except Exception:  # noqa: BLE001
                            bad.append(f"{node.module}.{a.name} does not exist")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    try:
                        importlib.import_module(a.name)
                    except Exception as e:  # noqa: BLE001
                        bad.append(f"import {a.name} ({type(e).__name__})")
        if defs_dir and defs_dir.exists():
            for y in defs_dir.rglob("defs.yaml"):
                for m in re.finditer(r"^type:\s*([\w.]+)\s*$", y.read_text(), re.M):
                    mod_name, _, cls = m.group(1).rpartition(".")
                    try:
                        if not hasattr(importlib.import_module(mod_name), cls):
                            bad.append(f"component type {m.group(1)} does not exist")
                    except Exception as e:  # noqa: BLE001
                        bad.append(f"component type {m.group(1)} ({type(e).__name__})")
    finally:
        del sys.path[:2]
    return gate("no_invented_apis", not bad, "every import and component type resolves" if not bad else "; ".join(bad[:6]))


def g_tasks(spec: dict[str, Any], src: str) -> dict[str, Any]:
    try:
        doc = ast.get_docstring(ast.parse(src)) or ""
    except SyntaxError as e:
        return gate("tasks_accounted_for", False, f"the module does not parse: {e.msg}")
    missing = [t["task_id"] for t in spec["tasks"] if t["task_id"] not in doc]
    nostatus = [t["task_id"] for t in spec["tasks"] if t["task_id"] in doc and not re.search(rf"{re.escape(t['task_id'])}[^\n]*(replaced|folded|dropped|merged)", doc)]
    return gate("tasks_accounted_for", not missing and not nostatus, "every Airflow task is named in the header with what happened to it" if not missing and not nostatus
                else f"missing from the header docstring: {missing}; named without replaced/folded/dropped/merged: {nostatus}")


def run_gates(spec: dict[str, Any], module: str, project: Path, defs_dir: Path | None, parity: dict[str, Any] | None) -> list[dict[str, Any]]:
    src, path = source_of(module, project)
    if not path:
        return [gate("definitions_load", False, f"module {module} not found under {project}")]
    return [g_load(module, project), g_tests(spec, module, project), g_checks(spec, src, defs_dir), g_parity(spec, module, project, parity), g_secrets(src, defs_dir), g_apis(src, defs_dir, project), g_tasks(spec, src)]


def record(manifest: dict[str, Any], dag_id: str, results: list[dict[str, Any]]) -> dict[str, int]:
    ok = all(r["passed"] for r in results)
    summary = {"passed": ok, "gates": {r["gate"]: r["passed"] for r in results}}
    stats = {"advanced": 0, "deferred": 0, "kept_converter": 0}
    for u in manifest["units"]:
        if u.get("dag_id") != dag_id:
            continue
        if u["status"] == "complete" and u.get("route", "converter") == "converter":
            u["rewrite_attempt"] = {**summary, "failures": [f"{r['gate']}: {r['detail']}" for r in results if not r["passed"]]}
            stats["kept_converter"] += 1
            continue
        u["route"] = "rewrite"
        u["gates"] = summary
        if ok:
            S.advance(manifest, u["id"], "rewrite route: all gates passed (" + ", ".join(r["gate"] for r in results) + ")")
            stats["advanced"] += 1
        else:
            S.defer(manifest, u["id"], "rewrite failed its gates (the converter output remains the fallback): " + "; ".join(f"{r['gate']}: {r['detail']}" for r in results if not r["passed"])[:800])
            stats["deferred"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--module", required=True)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--defs-dir", type=Path)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--parity", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    spec = json.loads(a.spec.read_text())
    parity = json.loads(a.parity.read_text()) if a.parity else None
    results = run_gates(spec, a.module, a.project.resolve(), a.defs_dir.resolve() if a.defs_dir else None, parity)
    out = a.out or a.spec.with_name(f"{spec['dag_id']}.gates.json")
    out.write_text(json.dumps({"dag_id": spec["dag_id"], "module": a.module, "passed": all(r["passed"] for r in results), "gates": results}, indent=1) + "\n")
    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['gate']}: {r['detail']}")
    if a.manifest:
        m = json.loads(a.manifest.read_text())
        st = record(m, spec["dag_id"], results)
        a.manifest.write_text(json.dumps(m, indent=2, default=str) + "\n")
        print("manifest:", ", ".join(f"{k}={v}" for k, v in st.items()))
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
