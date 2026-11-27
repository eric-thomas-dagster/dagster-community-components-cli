#!/usr/bin/env python3
"""Behaviour comparison for one DAG, or a whole kit: the original DAG in real Airflow against the converter's output and the rewrite, all in isolated sandboxes with the same stand-ins.

    python3 eval_behavior.py one   --work DIR --run stage1 --manifest corpus/manifest.json --key <repo>::<dag_id> --venv-2 PY --venv-3 PY [--routes converter,rewrite] [--dagster-python PY]
    python3 eval_behavior.py batch --work DIR --run stage1 --manifest corpus/manifest.json --venv-2 PY --venv-3 PY [--jobs 4] [--only dag_id ...]

Per DAG it runs FOUR executions concurrently, each in its own sandbox (`parity/sandbox.py`: a scratch cwd and HOME, local SQLite, HTTP and SMTP recorders, moto for S3, capped sleeps):
    airflow #1 and #2    the original DAG with `dag.test()` in the Airflow interpreter (twice: whatever differs between them is nondeterministic and is not compared)
    converter            `convert_ir.py --return-all` output, materialized in Dagster (absent when the converter deferred the DAG)
    rewrite              the worker's project, `<work>/<run>/<dag>/out/<project>`, in the project's own virtualenv when it has one (absent when there is none)
and compares each route to the baseline with `parity/behavior_compare.py`: overall outcome and each task's outcome, values, files, SQLite rows, HTTP requests, mails, S3 objects, failure behaviour,
schedule, retries, pools, dependencies, job. Writes `<work>/<run>-private/behavior/<dag>/behavior.json` and the raw results beside it. Nothing here is shown to the workers.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import eval_common as C
import eval_corpus as EC
import rewrite_project as RPJ

sys.path.insert(0, str(C.HERE / "parity"))
import behavior_compare as B  # noqa: E402
from harness_cfg import conn_uri, safe_module, sandbox_cfg, trace_cfg  # noqa: E402,F401

PARITY = C.HERE / "parity"


def fresh(path: Path) -> Path:
    if "sandboxes" not in path.parts:
        raise ValueError(f"refusing to clear {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


CONV_DIR: Path | None = None                                                  # set by --conv-dir: where the converter output is cached (default <work>/conv)


def converter_src(work: Path, row: dict[str, Any], conv_dir: Path | None = None, manifest: dict[str, Any] | None = None) -> tuple[Path, dict[str, Any] | None]:
    """The converter's output with --return-all (every Python task's value is returned, as the parity runs need), converted once per (repository, Airflow) and cached.
    When two files of the repository define the same dag id, the IR handed to the converter keeps, for each such id, the file the manifest row names (the one Airflow runs): the output then lives in `<repo>.<airflow>.rad`."""
    irp = work / "irs" / f"{row['repo']}.{row['airflow']}.json"
    ir = json.loads(irp.read_text()) if irp.exists() else {"dags": []}
    files = {r["dag_id"]: r["file"] for r in (manifest or {}).get("dags", []) if r["repo"] == row["repo"] and r.get("file")}
    deduped, dups = C.dedupe_ir(ir, files)
    out = (Path(conv_dir) if conv_dir else work / "conv") / f"{row['repo']}.{row['airflow']}.{'rad' if dups else 'ra'}"
    rep = out / "report.json"
    if not rep.exists():
        (out / "src" / "pkg" / "defs").mkdir(parents=True, exist_ok=True)
        if dups:
            irp = out / "ir.deduped.json"
            irp.write_text(json.dumps(deduped))
        subprocess.run([sys.executable, str(C.HERE / "convert_ir.py"), str(irp), "--out-dir", str(out / "src" / "pkg" / "defs"), "--report", str(rep), "--return-all"],
                       capture_output=True, text=True, timeout=1800)
    if not rep.exists():
        return out / "src", None
    return out / "src", json.loads(rep.read_text()).get("dags", {}).get(row["dag_id"])


def launch(cmd: list[str], env: dict[str, str], timeout: int, log: Path) -> subprocess.Popen:
    f = open(log, "w")
    return subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env={**os.environ, **env}, start_new_session=True)


def wait(p: subprocess.Popen, timeout: int) -> str | None:
    try:
        p.wait(timeout=timeout)
        return None
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, 9)
        p.wait()
        return f"killed after {timeout}s"


def rewrite_taskmap(project: Path, spec: dict[str, Any], route: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str | None]:
    return B.rewrite_taskmap(RPJ.sources(project), spec, route)


def converter_taskmap(rep: dict[str, Any]) -> dict[str, dict[str, Any]]:
    folded = {**(rep.get("folded") or {}), **{m: "loop" for m in (rep.get("loop_members") or {})}}      # a task-group loop's inner tasks (`group.task`, `group.__loop_gate`) run inside the loop's one step: folded
    out = {tid: {"mode": "folded" if tid in folded else "replaced", "asset": key} for tid, key in (rep.get("task_keys") or {}).items()}
    out.update({tid: {"mode": "dropped", "asset": None} for tid in rep.get("glue") or [] if tid not in out})        # an EmptyOperator that only joins dependencies is not an asset
    return out


def seed_for(dag_path: Path, root: Path) -> dict[str, str]:
    """the project files the DAG needs (imports and named data files, as in the worker's source/ folder), by repo-relative path: the sandbox copies them into the cwd for every route."""
    return {str(f.relative_to(root)): str(f) for f in C.local_deps(root, dag_path)}


def run_one(a: argparse.Namespace, entry: dict[str, Any], key: str) -> dict[str, Any]:
    work = a.work.resolve()
    manifest = C.load_manifest(a.manifest)
    row = C.row_for(manifest, key)
    priv = work / f"{a.run}-private"
    name = entry["dir"]
    spec = json.loads((priv / "specs" / f"{name}.spec.json").read_text())
    routes = [x for x in a.routes.split(",") if x]
    conv = None
    stratum = row["converter"]                                              # the manifest's stratum is from when the corpus was built; the converter changes, so it is recomputed from this run's report
    if "converter" in routes:
        src, rep = converter_src(work, row, CONV_DIR, manifest)
        if rep is not None:
            stratum = EC.converter_stratum(rep)[0]
        if stratum != "deferred":
            conv = {"src": src, "report": rep}
    res = execute(priv / "behavior" / name, priv / "sandboxes" / name, spec, row["dag_id"], C.dag_file(work, manifest, row), work / "repos" / row["repo"],
                  a.venv_2 if row["airflow"] == "2" else a.venv_3, a.dagster_python, conv, work / a.run / name / "out" / entry["project"], routes, a.timeout, a.sleep_cap,
                  deferred=stratum == "deferred", seed=seed_for(C.dag_file(work, manifest, row), work / "repos" / row["repo"]), extra_runs=earlier_baseline_runs(work, a.run, name), trace=not getattr(a, "no_trace", False))
    res.update({"key": key, "stratum": stratum, "manifest_stratum": row["converter"], "pattern": row["pattern"], "repo": row["repo"]})
    (priv / "behavior" / name / "behavior.json").write_text(json.dumps(res, indent=1, sort_keys=True, default=str) + "\n")
    return res


def earlier_baseline_runs(work: Path, run: str, name: str) -> list[dict[str, Any]]:
    """Airflow runs of this DAG recorded BEFORE the measurement (the two a kit baked into `tools/baseline/`, or the harness's private copy `<run>-private/baseline/<dag>/`). They are extra evidence on whether a task is
    deterministic: a task whose state differs across any of them is `nondeterministic: not compared`."""
    out = []
    for d in (work / run / name / "tools" / "baseline", work / f"{run}-private" / "baseline" / name):
        for f in ("airflow1.json", "airflow2.json"):
            if (d / f).exists():
                out.append(json.loads((d / f).read_text()))
        if out:
            break
    return out


def execute(out_dir: Path, sb: Path, spec: dict[str, Any], dag_id: str, dag_path: Path, root: Path, af_py: str, dagster_python: str, conv: dict[str, Any] | None, project: Path,
            routes: list[str], timeout: int = 300, cap: float | None = None, deferred: bool = False, seed: dict[str, str] | None = None, extra_runs: list[dict[str, Any]] | None = None, trace: bool = True) -> dict[str, Any]:
    """The four executions and the comparison for one DAG. `conv` is {"src": converted source dir, "report": the converter's report entry} or None; `project` is the rewrite's project root."""
    out_dir, sb, dag_path, root, project = (Path(p).resolve() for p in (out_dir, sb, dag_path, root, project))
    af_py, dagster_python = str(Path(af_py).absolute()), str(Path(dagster_python).absolute())          # absolute, but not resolved: a virtualenv's python is a symlink and its bin directory must stay on the PATH
    if conv:
        conv = {**conv, "src": Path(conv["src"]).resolve()}
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    procs: dict[str, tuple[subprocess.Popen, Path]] = {}
    base_env = {"PYTHONWARNINGS": "ignore"}
    path_with = lambda py: {"PATH": str(Path(py).parent) + os.pathsep + os.environ.get("PATH", "")}      # noqa: E731  # `python`, `uv`, `dbt` of the route's own environment are on the PATH
    for label in ("airflow1", "airflow2"):
        r = fresh(sb / label)
        cfg = {"dag_file": str(dag_path), "dag_id": dag_id, "repo_root": str(root), "sandbox": sandbox_cfg(r, spec, cap, seed, trace_cfg([root, dag_path.parent]) if trace else None), "out": str(out_dir / f"{label}.json"), "timeout_s": timeout}
        (r / "config.json").write_text(json.dumps(cfg))
        (out_dir / f"{label}.json").unlink(missing_ok=True)
        (out_dir / f"{label}.trace.json").unlink(missing_ok=True)
        procs[label] = (launch([af_py, "-W", "ignore", str(PARITY / "run_airflow.py"), str(r / "config.json")], {**base_env, **path_with(af_py)}, timeout, out_dir / f"{label}.log"), r)
    rep = (conv or {}).get("report")
    notes: dict[str, str] = {}
    if "converter" in routes:
        if deferred:
            notes["converter"] = "no-output: the converter deferred this DAG"
        elif conv is None or rep is None or rep.get("status") != "converted":
            notes["converter"] = "no-output: the converter produced nothing for this DAG"
        else:
            r = fresh(sb / "converter")
            cfg = {"kind": "converter", "dag_id": dag_id, "repo_root": str(root), "sandbox": sandbox_cfg(r, spec, cap, seed, trace_cfg([conv["src"] / "pkg" / "defs" / f"{safe_module(dag_id)}.py", root]) if trace else None), "out": str(out_dir / "converter.json"), "timeout_s": timeout, "converter": {"src": str(conv["src"]), "pkg": "pkg"}}
            (r / "config.json").write_text(json.dumps(cfg))
            (out_dir / "converter.json").unlink(missing_ok=True)
            (out_dir / "converter.trace.json").unlink(missing_ok=True)
            procs["converter"] = (launch([dagster_python, str(PARITY / "run_dagster.py"), str(r / "config.json")], {**base_env, **path_with(dagster_python), "PYTHONPATH": os.pathsep.join([str(conv["src"]), str(root)])}, timeout, out_dir / "converter.log"), r)
    if "rewrite" in routes:
        if not (project / "src").exists():
            notes["rewrite"] = "no-output: the worker produced no project"
        else:
            r = fresh(sb / "rewrite")
            py = project / ".venv" / "bin" / "python"
            cfg = {"kind": "rewrite", "dag_id": dag_id, "repo_root": str(root), "sandbox": sandbox_cfg(r, spec, cap, seed, trace_cfg([project / "src", root], [project / ".venv"]) if trace else None), "out": str(out_dir / "rewrite.json"), "timeout_s": timeout, "rewrite": {"project": str(project), "module": None}}
            (r / "config.json").write_text(json.dumps(cfg))
            (out_dir / "rewrite.json").unlink(missing_ok=True)
            (out_dir / "rewrite.trace.json").unlink(missing_ok=True)
            procs["rewrite"] = (launch([str(py) if py.exists() else dagster_python, str(PARITY / "run_dagster.py"), str(r / "config.json")], {**base_env, **path_with(str(py) if py.exists() else dagster_python)}, timeout, out_dir / "rewrite.log"), r)
    killed = {k: wait(p, timeout + 120) for k, (p, _) in procs.items()}
    elapsed = round(time.time() - t0, 1)
    for _, r in procs.values():                                               # the metadata databases are the bulk of the disk and are not evidence; the cwd, data and results stay
        for sub in ("airflow_home", "dagster_home"):
            if "sandboxes" in r.parts and (r / sub).exists():
                shutil.rmtree(r / sub, ignore_errors=True)

    return assess(out_dir, spec, dag_id, rep, project, routes, notes, killed, elapsed, extra_runs)



def assess(out_dir: Path, spec: dict[str, Any], dag_id: str, rep: dict[str, Any] | None, project: Path, routes: list[str], notes: dict[str, str], killed: dict[str, str | None], elapsed: float | None,
           extra_runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The comparison of one DAG's finished executions: reads the raw results in `out_dir` (airflow1/2.json, <route>.json, their `.trace.json` and `.log` files) and writes `behavior.json`. Split from `execute` so a
    comparison rule can be re-applied to recorded runs without running anything again (`recompare`)."""
    trace_stats: dict[str, Any] = {}

    def load(label: str) -> dict[str, Any]:
        f = out_dir / f"{label}.json"
        if f.exists():
            d = json.loads(f.read_text())
            tf = out_dir / f"{label}.trace.json"
            if tf.exists():                                                   # the observation layer's record (calls, commands), next to the raw result
                d["calltrace"] = json.loads(tf.read_text())
                trace_stats[label] = {"calls": len(d["calltrace"].get("calls", [])), "commands": len(d["calltrace"].get("commands", [])), "bytes": tf.stat().st_size, "run_seconds": d["calltrace"].get("seconds"), "dropped": d["calltrace"].get("dropped")}
            return d
        log = out_dir / f"{label}.log"
        tail = log.read_text()[-600:] if log.exists() else ""
        return {"error": f"the run left no result ({killed.get(label) or 'process exited'}): {tail.strip().splitlines()[-1] if tail.strip() else ''}"[:400], "state": None, "errors": [tail]}
    a1, a2 = load("airflow1"), load("airflow2")
    for a_, label in ((a1, "airflow1"), (a2, "airflow2")):                    # the process output holds what Airflow printed about a failed task
        lg = out_dir / f"{label}.log"
        if lg.exists() and "success" not in str(a_.get("state", "")).lower():
            a_.setdefault("errors", []).append("[process output] " + lg.read_text(errors="replace")[-2500:])
    for a_, label in ((a1, "airflow1"), (a2, "airflow2")):                    # what the original printed, from the process output (the printed-output comparison; also baked into kits made from this run)
        lg = out_dir / f"{label}.log"
        if lg.exists() and a_.get("state") is not None:
            a_["stdout_lines"] = B.printed_lines(lg.read_text(errors="replace"))
            a_["run_epoch"] = lg.stat().st_mtime                                  # when the Airflow run finished: the value comparison tells a wall-clock string of that run from a real difference
            raw = json.loads((out_dir / f"{label}.json").read_text()) if (out_dir / f"{label}.json").exists() else {}
            if raw:
                (out_dir / f"{label}.json").write_text(json.dumps({**raw, "stdout_lines": a_["stdout_lines"]}, indent=1, default=str))
    result: dict[str, Any] = {"dag_id": dag_id, "airflow": a1.get("airflow_version"), "seconds": elapsed, "routes": {}, "trace_stats": trace_stats}
    for route in routes:
        if route in notes:
            result["routes"][route] = {"rollup": {"verdict": "no-output", "why": notes[route]}, "dimensions": {}}
            continue
        r = load(route)
        rl = out_dir / f"{route}.log"
        if rl.exists() and r.get("state") is not None:
            r["stdout_lines"] = B.printed_lines(rl.read_text(errors="replace"), r.get("partition_key"))
        if route == "converter":
            tm = converter_taskmap(rep or {})
            r["stub_assets"] = sorted({tm[t]["asset"] for t in EC.converter_stratum(rep)[1] if t in tm and tm[t].get("asset")})       # a failing step that is a converter stub is never the DAG's own failure
        else:
            tm, hdr = rewrite_taskmap(project, spec, r)
            r.setdefault("notes", []).append(f"header accounting read from {hdr}")
        if "left no result" in str(r.get("error")) and not r.get("assets"):
            result["routes"][route] = {"rollup": {"verdict": "not-comparable", "why": "harness: " + r["error"]}, "dimensions": {}}
            continue
        cmp_ = B.compare(spec, a1, a2 if a2.get("state") is not None or a2.get("error") is None else None, r, tm, route, extra_runs=extra_runs)
        cmp_["taskmap"] = tm
        cmp_["route_notes"] = r.get("notes", [])
        cmp_["route_error"] = r.get("error")
        result["routes"][route] = cmp_
    result["baseline"] = {"airflow1": {"state": a1.get("state"), "error": a1.get("error")}, "airflow2": {"state": a2.get("state"), "error": a2.get("error")}, "class": B.baseline_class(a1, a2, spec)}
    (out_dir / "behavior.json").write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n")
    return result


def batch(a: argparse.Namespace) -> int:
    work = a.work.resolve()
    priv = work / f"{a.run}-private"
    index = json.loads((priv / "kit_index.json").read_text())
    manifest = C.load_manifest(a.manifest)
    todo = [(k, v) for k, v in sorted(index.items()) if not a.only or v["dag_id"] in a.only]
    if "converter" in a.routes:                                               # convert once per repository, before the parallel runs
        for k, v in todo:
            converter_src(work, C.row_for(manifest, k), CONV_DIR, manifest)
    print(f"comparing {len(todo)} DAG(s) with {a.jobs} at a time ...", flush=True)

    def go(item: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        try:
            return run_one(a, item[1], item[0])
        except Exception as e:  # noqa: BLE001
            return {"key": item[0], "error": f"{type(e).__name__}: {e}"}
    n = 0
    with ThreadPoolExecutor(a.jobs) as ex:
        for res in ex.map(go, todo):
            n += 1
            v = {r: x["rollup"]["verdict"] for r, x in res.get("routes", {}).items()}
            print(f"[{n}/{len(todo)}] {res.get('key')}: {v or res.get('error')} ({res.get('seconds')}s)", flush=True)
    return 0


def recompare(a: argparse.Namespace) -> int:
    """Re-apply the CURRENT comparison rules to the runs already recorded in `<work>/<run>-private/behavior/<dag>/` (nothing is executed): rewrites each `behavior.json`. For a change to how traces or values are read."""
    work = a.work.resolve()
    priv = work / f"{a.run}-private"
    index = json.loads((priv / "kit_index.json").read_text())
    manifest = C.load_manifest(a.manifest)
    n = 0
    for key, entry in sorted(index.items()):
        name = entry["dir"]
        out_dir = priv / "behavior" / name
        old = out_dir / "behavior.json"
        if not (out_dir / "airflow1.json").exists() or not old.exists():
            continue
        prev = json.loads(old.read_text())
        if "converter" not in prev.get("routes", {}) or not (out_dir / "converter.json").exists():
            continue
        row = C.row_for(manifest, key)
        spec = json.loads((priv / "specs" / f"{name}.spec.json").read_text())
        _, rep = converter_src(work, row, CONV_DIR, manifest)
        res = assess(out_dir, spec, row["dag_id"], rep, work / a.run / name / "out" / entry["project"], ["converter"], {}, {}, prev.get("seconds"), earlier_baseline_runs(work, a.run, name))
        res.update({k: prev[k] for k in ("key", "stratum", "manifest_stratum", "pattern", "repo") if k in prev})
        old.write_text(json.dumps(res, indent=1, sort_keys=True, default=str) + "\n")
        n += 1
    print(f"recompared {n} DAG(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("one", "batch", "recompare"):
        p = sub.add_parser(n)
        p.add_argument("--work", type=Path, required=True)
        p.add_argument("--run", required=True)
        p.add_argument("--manifest", type=Path, required=True)
        p.add_argument("--venv-2", required=True)
        p.add_argument("--venv-3", required=True)
        p.add_argument("--dagster-python", default=str(Path(os.environ.get("EVAL_DAGSTER_PYTHON", sys.executable))))
        p.add_argument("--sleep-cap", type=float, default=None, help="cap time.sleep at this many seconds on every route (off by default: it stops timeouts from firing)")
        p.add_argument("--routes", default="converter,rewrite")
        p.add_argument("--conv-dir", type=Path, help="where to cache the converter's --return-all output (default <work>/conv); point it at a fresh folder to convert with the CURRENT converter")
        p.add_argument("--timeout", type=int, default=300)
        p.add_argument("--no-trace", action="store_true", help="do not record the call and command trace (the observation layer)")
        if n == "one":
            p.add_argument("--key", required=True)
        else:
            p.add_argument("--jobs", type=int, default=3)
            p.add_argument("--only", nargs="*")
    a = ap.parse_args(argv)
    global CONV_DIR
    CONV_DIR = getattr(a, "conv_dir", None)
    if a.cmd == "batch":
        return batch(a)
    if a.cmd == "recompare":
        return recompare(a)
    index = json.loads((a.work.resolve() / f"{a.run}-private" / "kit_index.json").read_text())
    res = run_one(a, index[a.key], a.key)
    for r, x in res["routes"].items():
        print(f"{r}: {x['rollup']['verdict']}: {x['rollup'].get('why')}")
    print(f"baseline: {res['baseline']}; {res['seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
