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
  8. task states           where an Airflow baseline exists (`--baseline DIR` with airflow1.json/airflow2.json): the project is run in the measurement's sandbox and each task's final state (success, failed,
                           skipped, upstream_failed), the outcome, the failure behaviour and the observable effects (files, SQLite rows, HTTP requests, mails, S3 objects) are compared with Airflow's; the gate
                           names every task that differs. A task folded into another asset whose Airflow state is `skipped` (a branch not taken) is an accepted representation difference only when the
                           decision record declares `fallback to plain Python: <reason>` (or `not preserved`); the native optional-output form is the expected path, where the skipped state itself is compared. Without a baseline: `not run`, which does not fail the gate.
  9. decision record       the header has a `Decision record:` section with `- <behaviour>: preserved | not preserved: <why>` for every behaviour the spec flags (skip/branch, trigger rules, retries, pools,
                           sensor modes, depends_on_past, failure semantics, mapped tasks); `not preserved` needs a reason. The declared losses are recorded in the gate result.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
if (Path(__file__).resolve().parent / "parity").exists():                  # the repository layout; in a worker kit everything is flat in tools/
    sys.path.insert(0, str(Path(__file__).resolve().parent / "parity"))

import gen_tests as G  # noqa: E402
import rewrite_patterns as RP  # noqa: E402
import rewrite_project as RPJ  # noqa: E402
import status as S  # noqa: E402

SECRET_LITERAL = re.compile(r"""(?i)\b(\w*(?:password|passwd|secret|token|api_?key|private_?key|credential)\w*)\s*[:=]\s*(['"])(?!<)([^'"\n]{6,})\2""")
ENV_OK = re.compile(r"(?i)env_var|EnvVar|environ|getenv|os\.environ|\{\{\s*env")
EXTERNAL_OPS = ("Sensor", "Snowflake", "S3", "GCS", "BigQuery", "Http", "Sql", "Postgres", "MySql", "Kubernetes", "Docker", "SSH", "Lambda", "Spark", "Dbt", "Slack", "Email", "Databricks", "Airbyte", "Fivetran", "Trigger")


def gate(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"gate": name, "passed": bool(ok), "detail": detail}


def source_of(module: str, project: Path) -> tuple[str, Path | None]:
    """the source of one module (kept for callers that name a module); the gates themselves read every file under defs/ (`rewrite_project.sources`)."""
    for root in (project / "src", project):
        p = root.joinpath(*module.split(".")).with_suffix(".py")
        if p.exists():
            return p.read_text(), p
    return "", None


def g_load(project: Path, pkg: str, module: str | None = None) -> dict[str, Any]:
    """`dg check defs` in the project (its own `dg` first; VIRTUAL_ENV is unset for the run, so an outer virtualenv does not make `dg` warn about a mismatch). Without `dg` the project's own loader is run instead."""
    dg_bin = RPJ.dg_binary(project)
    if dg_bin:
        r = subprocess.run([dg_bin, "check", "defs"], cwd=project, capture_output=True, text=True, env=RPJ.clean_env(project))
        noise = ("Warning", "class ", "return meta", "real = ", "active virtual environment", "project virtual environment", "To suppress this warning", "root directory. This may lead")
        tail = "\n".join(ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip() and not any(w in ln for w in noise))[-600:]
        return gate("definitions_load", r.returncode == 0, "dg check defs" + ("" if r.returncode == 0 else ": " + tail))
    code = ("import sys, dagster as dg\n" f"sys.path[:0] = [{str(project / 'src')!r}, {str(project)!r}]\n" + RPJ.LOAD_DEFS_SOURCE +
            f"dg.Definitions.validate_loadable(load_defs({RPJ.definitions_module(project, pkg)!r}, {module!r}))\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=project, env=RPJ.clean_env(project))
    return gate("definitions_load", r.returncode == 0, "`dg` is not installed: the project's definitions were loaded with its own loader instead (Definitions.validate_loadable)" + ("" if r.returncode == 0 else ": " + r.stderr[-600:]))


def expected_failure(spec: dict[str, Any], baseline: Path | None) -> dict[str, Any] | None:
    """When the Airflow baseline shows the DAG's own run FAILING (by design, or on a virtualenv / external-interpreter task that cannot build in the sandbox), what the rewrite must do too: fail. None without a
    baseline (today's behaviour: the generated test expects success), and None for a baseline that failed for any other environmental reason (the rewrite may legitimately succeed)."""
    if baseline is None or not (Path(baseline) / "airflow1.json").exists():
        return None
    import behavior_compare as BC
    a1 = json.loads((Path(baseline) / "airflow1.json").read_text())
    a2 = json.loads((Path(baseline) / "airflow2.json").read_text()) if (Path(baseline) / "airflow2.json").exists() else None
    cls, why = BC.baseline_class(a1, a2, spec)
    if cls == "by-design-failure" or (cls == "baseline-failed" and "virtualenv" in why):
        failed = sorted(t for t, v in (a1.get("tasks") or {}).items() if BC.norm_state(v.get("state")) == "failed")
        http = [h for a in (a1, a2) if a for h in ((a.get("observed") or {}).get("http") or [])]
        return {"sandbox_http": bool(http) and cls == "by-design-failure", "why": ("the DAG's own code fails" if cls == "by-design-failure" else "a virtualenv / external-interpreter task cannot build in the sandbox") + (f"; failed task(s) {failed}" if failed else ""), "failed_tasks": failed}
    return None


def sandbox_env(spec: dict[str, Any]) -> dict[str, str]:
    """the Airflow connections and Variables the behaviour sandbox exports (`AIRFLOW_CONN_<ID>`, `AIRFLOW_VAR_<NAME>`), as the generated test sets them: `{TMP}` stands for the test's temporary directory."""
    import harness_cfg as HC
    conns = {"sqlite_default", "http_default", "smtp_default", "aws_default", *(spec.get("connections") or [])}
    env = {"AIRFLOW_CONN_" + c.upper(): HC.conn_uri(c, Path("{TMP}")) for c in sorted(conns)}
    env.update({"AIRFLOW_VAR_" + v.upper(): "eval-variable" for v in (spec.get("variables") or [])})
    return env


def g_tests(spec: dict[str, Any], module: str | None, project: Path, baseline: Path | None = None) -> dict[str, Any]:
    ops = [t["operator"] for t in spec["tasks"]]
    skip = None
    if spec.get("cosmos_groups") or any(any(w in o for w in EXTERNAL_OPS) for o in ops) or spec["schedule"]["assets"]:
        skip = "the DAG has an external system, a sensor, dbt or an asset trigger: only the load and shape tests apply"
    ef = expected_failure(spec, baseline)
    if ef and ef.get("sandbox_http") and skip is None:                       # the baseline failed after an HTTP call the sandbox answered with its canned {"ok": true}; an in-process test cannot reproduce that answer (it would hit the real service), so neither "must fail" nor "must succeed" is a fair contract
        skip, ef = "the baseline's failure follows an HTTP call answered by the sandbox's canned response; a test run outside the sandbox cannot reproduce it: only the load and shape tests apply", None
    sched = spec["schedule"]
    pkg = RPJ.package_of(project, module.split(".")[0] if module else None) or (module or "pkg").split(".")[0]
    info = {"dag_id": spec["dag_id"], "module": module or f"{pkg}.defs.{spec['dag_id']}", "definitions_module": RPJ.definitions_module(project, pkg),
            "schedule": ({"cron": sched["cron"], "timezone": sched["timezone"]} if sched["kind"] in ("cron_trigger", "cron_data_interval") and sched["cron"] else (None if sched["kind"] in ("manual", None) else "unchecked")),
            "materialize_skip": skip, "expect_failure": ef, "needs_shell": any(o in ("BashOperator", "_BashDecoratedOperator") for o in ops), "env": sandbox_env(spec)}
    tdir = project / "tests"
    tdir.mkdir(exist_ok=True)
    f = tdir / f"test_{spec['dag_id']}_rewrite.py"
    f.write_text(G.render_rewrite(info))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore", "-rs", str(f)], cwd=project, capture_output=True, text=True, env=RPJ.clean_env(project))
    tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()][-1:] or [r.stderr[-200:]]
    ef = info["expect_failure"]
    return gate("generated_tests", r.returncode == 0, f"{f.name}: {tail[0]}" + ("; materialize skipped: " + skip if skip else "") + (f"; EXPECTED FAILURE asserted instead of success: Airflow's own run fails ({ef['why']})" if ef else ""))


def count_checks(src: str) -> int:
    return len(re.findall(r"@(?:dg|dagster)\.asset_check\(|@asset_check\(|check_specs\s*=|AssetCheckSpec\(|@dg\.multi_asset_check", src)) + src.count("asset_checks=") * 0


def g_checks(spec: dict[str, Any], texts: dict[str, str], summary: dict[str, Any]) -> dict[str, Any]:
    """Asset checks present: counted in the LOADED definitions (a component's checks included), and, when the definitions could not be summarised, by reading the files under defs/."""
    n = len(summary.get("checks") or []) if "error" not in summary else sum(count_checks(t) for p, t in texts.items() if p.endswith(".py")) + sum(1 for p, t in texts.items() if p.endswith((".yaml", ".yml")) and ("asset_checks" in t or "AssetCheck" in t))
    need = len((spec.get("converter") or {}).get("asset_checks") or [])
    # a rewrite folds steps, so it has fewer places to check than the task-per-asset converter: what is required is a structural check wherever the source supports one at all
    return gate("asset_checks_present", n >= min(need, 1), f"{n} asset check(s) in the rewrite; the converter derived {need} from the source (at least 1 is required when it derived any)")


def g_parity(spec: dict[str, Any], project: Path, pkg: str, parity: dict[str, Any] | None, header: str) -> dict[str, Any]:
    base = (parity or {}).get(spec["dag_id"])
    if not base or "error" in base:
        return gate("parity", True, "not run (no Airflow baseline for this DAG: it may be ineligible, or none was given)")
    code = ("import json, sys, dagster as dg\n" f"sys.path[:0] = [{str(project / 'src')!r}, {str(project)!r}]\n" + RPJ.LOAD_DEFS_SOURCE +
            f"defs = load_defs({RPJ.definitions_module(project, pkg)!r}, None)\n"
            "a = [v for v in defs.assets if isinstance(v, dg.AssetsDefinition) and not isinstance(v, dg.AssetChecksDefinition)]\n"
            "job = defs.resolve_job_def(" + repr(spec['dag_id']) + ") if " + repr(spec['dag_id']) + " in [j.name for j in (defs.jobs or [])] else defs.resolve_implicit_global_asset_job_def()\n"
            "r = job.execute_in_process(raise_on_error=False)\nvals = {}\n"
            "for d in a:\n    for k in d.keys:\n        try:\n            vals[k.to_user_string()] = repr(r.asset_value(k))[:300]\n        except Exception:\n            pass\n"
            "print(json.dumps({'ok': r.success, 'values': vals}))\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=project, env=RPJ.clean_env(project))
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return gate("parity", False, "the rewrite could not be run: " + r.stderr[-300:])
    want_ok = "success" in str(base.get("state", "")).lower()
    problems = [] if out["ok"] == want_ok else [f"overall: Airflow {base.get('state')}, rewrite {'success' if out['ok'] else 'failed'}"]
    compared = 0
    for tid, tb in (base.get("tasks") or {}).items():
        m = re.search(rf"{re.escape(tid)}\s*:\s*replaced by[^`\n]*`([^`]+)`", header)
        if m and m.group(1) in out["values"] and tb.get("value") not in (None, "None"):
            compared += 1
            if out["values"][m.group(1)] != tb["value"]:
                problems.append(f"{tid}: Airflow value {tb['value'][:60]} vs rewrite {out['values'][m.group(1)][:60]} (asset {m.group(1)})")
    return gate("parity", not problems, ("outcome and " + str(compared) + " value(s) match Airflow" if not problems else "; ".join(problems)))


def g_secrets(texts: dict[str, str]) -> dict[str, Any]:
    hits = []
    for name, t in texts.items():
        for i, ln in enumerate(t.splitlines(), 1):
            m = SECRET_LITERAL.search(ln)
            if m and not ENV_OK.search(ln):
                hits.append(f"{Path(name).name} line {i}: a string literal is assigned to `{m.group(1)}` (the value is not echoed)")
    return gate("no_secret_values", not hits, "none found" if not hits else "; ".join(hits[:5]))


def g_apis(texts: dict[str, str], project: Path, pkg: str) -> dict[str, Any]:
    bad = []
    sys.path[:0] = [str(project / "src"), str(project)]
    try:
        for name, text in texts.items():
            if name.endswith(".py"):
                try:
                    tree = ast.parse(text)
                except SyntaxError as e:
                    bad.append(f"{Path(name).name} does not parse: {e.msg}")
                    continue
                for node in ast.walk(tree):
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
            else:
                for m in re.finditer(r"^type:\s*([\w.]+)\s*$", text, re.M):
                    t = m.group(1)
                    if t.startswith("."):                                            # relative to the folder of the defs.yaml, as dg resolves it
                        rel = Path(name).parent.relative_to(project / "src")
                        t = ".".join(rel.parts) + t
                    mod_name, _, cls = t.rpartition(".")
                    try:
                        if not hasattr(importlib.import_module(mod_name), cls):
                            bad.append(f"component type {t} does not exist")
                    except Exception as e:  # noqa: BLE001
                        bad.append(f"component type {t} ({type(e).__name__})")
    finally:
        del sys.path[:2]
    return gate("no_invented_apis", not bad, "every import and component type resolves" if not bad else "; ".join(bad[:6]))


def g_tasks(spec: dict[str, Any], header: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Every Airflow task is named in the header (module docstring or defs.yaml comment block) with what happened to it, and a task `replaced by`, `folded into` or `merged into` an asset names an asset KEY that exists in the
    loaded definitions (resolved as the behaviour comparison resolves it: full key, last segment or function name)."""
    missing = [t["task_id"] for t in spec["tasks"] if t["task_id"] not in header]
    nostatus = [t["task_id"] for t in spec["tasks"] if t["task_id"] in header and not re.search(rf"{re.escape(t['task_id'])}[^\n]*(replaced|folded|dropped|merged)", header)]
    unresolved: list[str] = []
    if "error" not in summary and not missing and not nostatus:
        import behavior_compare as BC
        tm = BC.parse_header(header, [t["task_id"] for t in spec["tasks"]], sorted(summary["assets"]), {k: v for k, v in summary["assets"].items() if v})
        unresolved = [f"{t} ({v['mode']} but no loaded asset key matches)" for t, v in tm.items() if v["mode"] in ("replaced", "folded", "merged") and not v["asset"]]
    ok = not missing and not nostatus and not unresolved
    return gate("tasks_accounted_for", ok, "every Airflow task is named in the header with what happened to it, and each asset it names exists in the loaded definitions" if ok
                else f"missing from the header: {missing}; named without replaced/folded/dropped/merged: {nostatus}; asset not found among the loaded asset keys: {unresolved}")


def g_states(spec: dict[str, Any], project: Path, baseline: Path | None, source: Path | None, header: str) -> dict[str, Any]:
    if baseline is None or not (Path(baseline) / "airflow1.json").exists():
        return gate("task_states", True, "not run (no Airflow baseline for this DAG was given)")
    import compare_behavior as CB
    res = CB.compare_project(project, spec, baseline, source, python=sys.executable)
    if res is None:
        return gate("task_states", True, "not run (no Airflow baseline for this DAG was given)")
    bclass = res["baseline"]["class"]
    if bclass in ("baseline-failed", "unstable"):
        return gate("task_states", True, f"not compared: the Airflow baseline is {bclass} ({res['baseline']['reason'][:160]})")
    if res.get("route_error") and not (res["dimensions"].get("tasks", {}).get("verdict") in ("match", "differ")):
        return gate("task_states", False, f"the project could not be run in the sandbox: {str(res['route_error'])[:200]}")
    dims = res["dimensions"]
    problems = []
    for x in (dims.get("tasks", {}).get("evidence") or {}).get("differences", []) if dims.get("tasks", {}).get("verdict") == "differ" else []:
        problems.append(f"task `{x['task']}`: Airflow {x['airflow']}, rewrite {x['route']}" + (f" (asset `{x['asset']}`)" if x.get("asset") else ""))
    for n in ("outcome", "failure", "files", "sqlite", "http", "smtp", "s3"):
        if dims.get(n, {}).get("verdict") == "differ":
            problems.append(f"{n}: {json.dumps(dims[n].get('evidence'), default=str)[:240]}")
    accepted = (dims.get("tasks", {}).get("evidence") or {}).get("accepted_representation", [])
    if accepted and RP.parse_record(header).get("skip/branch", {}).get("verdict") not in ("fallback", "not preserved"):
        problems.append("tasks " + ", ".join(f"`{a['task']}`" for a in accepted) + " were skipped in Airflow (not taken) and are folded into another asset, so Dagster shows no skipped state for them: either use the native optional-output form "
                        "(the expected path for branches) or declare `- skip/branch: fallback to plain Python: <reason>` in the decision record")
    n = dims.get("tasks", {}).get("compared", 0)
    nd = (dims.get("tasks", {}).get("evidence") or {}).get("nondeterministic_baseline") or {}
    note = (f"; {len(accepted)} folded not-taken task(s) accepted as a representation difference (declared)" if accepted and not problems else "") + (f"; {len(nd)} task(s) `nondeterministic: not compared` (the Airflow baseline's runs disagree on them)" if nd else "")
    return gate("task_states", not problems, (f"{n} task(s) compared; outcome, failure behaviour and effects match Airflow" + note) if not problems else "; ".join(problems)[:900])


def g_decision(spec: dict[str, Any], header: str) -> dict[str, Any]:
    ok, detail, declared = RP.check_record(spec, header)
    g = gate("decision_record", ok, detail)
    g["declared"] = declared
    return g


def run_gates(spec: dict[str, Any], module: str | None, project: Path, defs_dir: Path | None, parity: dict[str, Any] | None, baseline: Path | None = None, source: Path | None = None) -> list[dict[str, Any]]:
    """`module` and `defs_dir` are hints only: the package, the modules and the component folders are discovered under src/<pkg>/defs/ (`rewrite_project`), so the file name a worker chose (`_name.py`, a lower-cased package,
    a component folder instead of a module) does not matter."""
    pkg = RPJ.package_of(project, module.split(".")[0] if module else None)
    texts = RPJ.sources(project, pkg) if pkg else {}
    if not pkg or not texts:
        return [gate("definitions_load", False, f"no rewrite found under {project}: expected src/<package>/defs/ with a Python module or a component folder holding a defs.yaml")]
    header = RPJ.header_text(project, pkg)
    summary = RPJ.defs_summary(project, pkg, python=sys.executable)
    return [g_load(project, pkg, module), g_tests(spec, module, project, baseline), g_checks(spec, texts, summary), g_parity(spec, project, pkg, parity, header), g_secrets(texts), g_apis(texts, project, pkg),
            g_tasks(spec, header, summary), g_states(spec, project, baseline, source, header), g_decision(spec, header)]


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
    ap.add_argument("--module", help="optional hint (the dotted module of a module-style rewrite); the project is discovered under src/<pkg>/defs/ either way")
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--defs-dir", type=Path, help="optional and ignored: every component folder under defs/ is found automatically")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--parity", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--baseline", type=Path, help="a folder with the original DAG's airflow1.json (and airflow2.json): enables the task_states gate")
    ap.add_argument("--source", type=Path, help="the folder whose files are seeded into the sandbox's working directory (a worker kit's source/)")
    a = ap.parse_args(argv)
    spec = json.loads(a.spec.read_text())
    parity = json.loads(a.parity.read_text()) if a.parity else None
    results = run_gates(spec, a.module, a.project.resolve(), a.defs_dir.resolve() if a.defs_dir else None, parity, a.baseline.resolve() if a.baseline else None, a.source.resolve() if a.source else None)
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
