#!/usr/bin/env python3
"""Dump what a real Airflow parse of a DAG folder produces: DAG ids, task ids, schedules.

    python3 airflow_truth.py <dags_folder> [<dags_folder> ...] --out truth.json

WARNING: this EXECUTES the DAG files (their top-level code), exactly as Airflow's own parser does.
Run it only on code you trust, in a sandbox, in an environment where Airflow and the DAGs' providers
are installed (a venv of the same Airflow version the project runs). It never touches a metadata DB:
AIRFLOW_HOME points at a temp directory.

Use the output with `reconcile.py` to close the gaps a static scan cannot see (DAGs and tasks built by
factories, loops, or constructs the scanner does not know). Files that fail to import are reported, not hidden.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("truth.json"))
    a = ap.parse_args(argv)
    warnings.filterwarnings("ignore")
    os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp(prefix="af_truth_")
    os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
    try:
        try:
            from airflow.dag_processing.dagbag import DagBag      # Airflow 3
        except ImportError:
            from airflow.models.dagbag import DagBag              # Airflow 2
    except ImportError as e:
        print(f"Airflow is not importable in this environment: {e}", file=sys.stderr)
        return 2

    import airflow
    if airflow.__version__.startswith("2."):
        from airflow.utils.db import initdb     # Airflow 2 reads dataset tables while parsing; give it a throwaway DB
        initdb()

    import logging
    logged: list[str] = []

    class _Catch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            msg = record.getMessage()
            if record.levelno >= logging.ERROR and ("Failed to load dag" in msg or "dag-factory" in msg.lower() or "dagfactory" in record.name):
                exc = f": {record.exc_info[1]}" if record.exc_info and record.exc_info[1] else ""
                logged.append(f"{msg}{exc}"[:400])

    logging.getLogger().addHandler(_Catch())

    dags: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for folder in [f.resolve() for f in a.folders]:   # dag-factory and others require absolute paths
        try:
            bag = DagBag(dag_folder=str(folder), include_examples=False, safe_mode=False)
        except TypeError:
            bag = DagBag(dag_folder=str(folder), safe_mode=False)   # Airflow 3 dropped include_examples
        for path, err in bag.import_errors.items():
            errors[os.path.relpath(path, folder)] = err.strip().splitlines()[-1][:300]
        for dag_id, dag in bag.dags.items():
            sched = getattr(dag, "schedule", None) or getattr(dag, "timetable", None) or getattr(dag, "schedule_interval", None)
            dags[dag_id] = {"file": os.path.relpath(dag.fileloc, folder), "tasks": sorted(dag.task_dict), "schedule": str(sched)[:200]}
    for i, msg in enumerate(logged):          # dag-factory logs a failure and skips the file: no DAG, no import error
        errors[f"(logged failure {i + 1})"] = msg
    a.out.write_text(json.dumps({"dags": dags, "import_errors": errors}, indent=1) + "\n")
    print(f"airflow parsed {len(dags)} DAGs, {sum(len(d['tasks']) for d in dags.values())} tasks; {len(errors)} file(s) failed to import")
    for f, e in list(errors.items())[:10]:
        print(f"  import error: {f}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
