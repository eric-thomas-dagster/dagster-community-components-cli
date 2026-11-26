#!/usr/bin/env python3
"""Pre-flight: run the ORIGINAL DAG in real Airflow (twice) for every DAG that looks eligible, before any sampling, and record whether the baseline is usable.

    python3 eval_baseline.py --work DIR --manifest corpus/manifest.json --venv-2 PY --venv-3 PY [--jobs 6] [--timeout 150] [--only dag_id ...] [--out corpus/baselines.json]

Why: a DAG whose own baseline fails for an environmental reason (a missing package, an unreachable service, a response body the stand-in does not have, a wait longer than the timeout) cannot be
measured, and a worker would spend a rewrite on it for nothing. `eval_sample.py --baselines corpus/baselines.json` then samples only DAGs whose baseline is `ok` or `by-design-failure`.
The DAGs dropped here are not hidden: they stay in the corpus manifest and are listed with the reason in the baselines file and in the report's corpus section.

Classes (see `parity/behavior_compare.py`): ok, by-design-failure (the DAG's own code fails, reproducibly), baseline-failed (environmental), unstable (two runs disagree).
Each DAG runs twice in separate sandboxes (the same two runs the behaviour comparison later uses), so this is also the nondeterminism check. Resumable: a DAG with a result is skipped.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import eval_behavior as E
import eval_common as C


def one(a: argparse.Namespace, row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    work = a.work.resolve()
    spec, _ = C.make_spec(work, row)
    base = work / "baselines" / C.safe(row["key"])
    res = E.execute(base / "out", base / "sandboxes", spec, row["dag_id"], C.dag_file(work, a.manifest_data, row), work / "repos" / row["repo"],
                    a.venv_2 if row["airflow"] == "2" else a.venv_3, sys.executable, None, base / "none", [], a.timeout)
    cls, why = res["baseline"]["class"]
    a1 = json.loads((base / "out" / "airflow1.json").read_text()) if (base / "out" / "airflow1.json").exists() else {}
    return row["key"], {"class": cls, "reason": why[:300], "seconds": res["seconds"], "state": a1.get("state"), "tasks": {t: v.get("state") for t, v in (a1.get("tasks") or {}).items()}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--venv-2", required=True)
    ap.add_argument("--venv-3", required=True)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=150)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--rerun-not-ok", action="store_true", help="run again every DAG whose recorded class is not ok (after the harness changed)")
    a = ap.parse_args(argv)
    a.manifest_data = C.load_manifest(a.manifest)
    out = a.out or a.work / "corpus" / "baselines.json"
    done = json.loads(out.read_text()) if out.exists() else {}
    if a.rerun_not_ok:
        done = {k: v for k, v in done.items() if v["class"] == "ok"}
    todo = [r for r in a.manifest_data["dags"] if r.get("eligible") and r["key"] not in done and (not a.only or r["dag_id"] in a.only)]
    print(f"{len(todo)} baseline(s) to run ({len(done)} already recorded), {a.jobs} at a time, timeout {a.timeout}s ...", flush=True)

    def safe_one(r: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            return one(a, r)
        except Exception as e:  # noqa: BLE001
            return r["key"], {"class": "baseline-failed", "reason": f"harness error: {type(e).__name__}: {e}"[:300], "seconds": 0, "state": None, "tasks": {}}
    n = 0
    with ThreadPoolExecutor(a.jobs) as ex:
        for k, v in ex.map(safe_one, todo):
            done[k] = v
            n += 1
            if n % 10 == 0 or n == len(todo):
                out.write_text(json.dumps(done, indent=1, sort_keys=True) + "\n")
                print(f"  [{n}/{len(todo)}] {dict(collections.Counter(x['class'] for x in done.values()))}", flush=True)
    out.write_text(json.dumps(done, indent=1, sort_keys=True) + "\n")
    print("baseline classes:", dict(collections.Counter(x["class"] for x in done.values())), f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
