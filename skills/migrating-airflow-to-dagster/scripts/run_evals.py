#!/usr/bin/env python3
"""Skill-level evals: convert a small set of fixture IRs and check pass criteria on the output.

    python3 run_evals.py [--cases ../evals/cases.json] [--only NAME]

Criteria (per case, in `evals/cases.json`):
  converted            the DAG converted (not deferred)
  definitions_load     the generated project loads: `dg check defs` when `dg` is installed, otherwise the definitions are loaded with dagster (the result says which)
  generated_tests      the generated `tests/test_<dag>.py` pass (a skip with a stated reason counts as a pass; a failure does not)
  no_invented_types    every component `type:` in generated YAML resolves to a class that exists (none generated: pass)
  no_secret_values     none of the case's `forbid` strings (fake secrets planted in the IR), and no credential-looking literal, appears in any generated file or in the report
Exit status is nonzero if any criterion of any case fails. The evals use fixtures only: they need dagster and pytest, not Airflow.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
SECRET_LITERAL = re.compile(r"""(?i)\b\w*(?:password|passwd|secret|token|api_?key|private_?key)\w*\s*[:=]\s*(['"])(?!<)([^'"\n]{6,})\1""")
ENV_OK = re.compile(r"(?i)env_var|EnvVar|environ|getenv|\{\{\s*env")


def project(tmp: Path, pkg: str = "ev_pkg") -> Path:
    (tmp / "src" / pkg / "defs").mkdir(parents=True)
    (tmp / "src" / pkg / "__init__.py").write_text("")
    (tmp / "pyproject.toml").write_text(f'[project]\nname = "{pkg}"\nversion = "0.0.1"\n[tool.dg]\ndirectory_type = "project"\n[tool.dg.project]\nroot_module = "{pkg}"\ndefs_module = "{pkg}.defs"\n'
                                        f'code_location_target_module = "{pkg}.definitions"\n[tool.dagster]\nmodule_name = "{pkg}.definitions"\ncode_location_name = "{pkg}"\n')
    (tmp / "src" / pkg / "definitions.py").write_text("from pathlib import Path\n\nfrom dagster import definitions, load_from_defs_folder\n\n\n@definitions\ndef defs():\n    return load_from_defs_folder(path_within_project=Path(__file__).parent)\n")
    return tmp


def check_load(root: Path) -> tuple[bool, str]:
    env = {**os.environ, "PYTHONPATH": f"{root / 'src'}:{root}"}
    if shutil.which("dg"):
        r = subprocess.run(["dg", "check", "defs"], cwd=root, capture_output=True, text=True, env=env)
        return r.returncode == 0, "dg check defs" + ("" if r.returncode == 0 else ": " + (r.stdout + r.stderr)[-300:])
    code = "import dagster as dg\nfrom ev_pkg.definitions import defs\nd = defs()\nd.validate_loadable(d) if hasattr(d, 'validate_loadable') else None\n"
    r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, env=env)
    return r.returncode == 0, "`dg` not installed: definitions loaded with dagster" + ("" if r.returncode == 0 else ": " + r.stderr[-300:])


def run_case(case: dict, tmp: Path) -> list[dict]:
    root = project(tmp)
    ir_text = (ROOT / "evals" / case["ir"]).read_text().replace("@@DAGS@@", str(ROOT / "examples" / "rewrite" / "airflow_src"))
    (tmp / "ir.json").write_text(ir_text)
    r = subprocess.run([sys.executable, str(HERE / "convert_ir.py"), str(tmp / "ir.json"), "--out-dir", str(root / "src" / "ev_pkg" / "defs"), "--report", str(tmp / "report.json"), *case["flags"]],
                       capture_output=True, text=True)
    report = json.loads((tmp / "report.json").read_text()) if (tmp / "report.json").exists() else {"dags": {}}
    out = [{"criterion": "converted", "passed": r.returncode == 0 and bool(report["dags"]) and all(v["status"] == "converted" for v in report["dags"].values()),
            "detail": "; ".join(f"{k}: {v['status']}" for k, v in report["dags"].items()) or r.stderr[-200:]}]
    for c in case["criteria"]:
        if c == "converted":
            continue
        if c == "definitions_load":
            ok, d = check_load(root)
        elif c == "generated_tests":
            t = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore", str(root / "tests")], cwd=root, capture_output=True, text=True,
                               env={**os.environ, "PYTHONPATH": f"{root / 'src'}:{root}"})
            ok, d = t.returncode == 0, ([ln for ln in t.stdout.strip().splitlines() if ln.strip()] or [t.stderr[-200:]])[-1]
        elif c == "no_invented_types":
            bad = []
            for y in root.rglob("defs.yaml"):
                for m in re.finditer(r"^type:\s*([\w.]+)\s*$", y.read_text(), re.M):
                    mod, _, cls = m.group(1).rpartition(".")
                    try:
                        if not hasattr(importlib.import_module(mod), cls):
                            bad.append(m.group(1))
                    except Exception:  # noqa: BLE001
                        bad.append(m.group(1))
            ok, d = not bad, "no component YAML generated or every type resolves" if not bad else f"unresolved: {bad}"
        elif c == "no_secret_values":
            hits = []
            for p in [*root.rglob("*"), tmp / "report.json"]:
                if p.is_file() and p.name != "ir.json" and p.suffix in (".py", ".yaml", ".yml", ".json", ".txt", ".md") and "__pycache__" not in str(p):       # ir.json is the input
                    text = p.read_text(errors="ignore")
                    hits += [f"{p.name}: planted secret" for s in case.get("forbid", []) if s in text]
                    hits += [f"{p.name}: credential-looking literal" for ln in text.splitlines() if SECRET_LITERAL.search(ln) and not ENV_OK.search(ln)]
            ok, d = not hits, "none found" if not hits else "; ".join(sorted(set(hits))[:4])
        else:
            ok, d = False, f"unknown criterion {c}"
        out.append({"criterion": c, "passed": ok, "detail": d})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", type=Path, default=ROOT / "evals" / "cases.json")
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    failed = 0
    for case in json.loads(a.cases.read_text())["cases"]:
        if a.only and case["name"] != a.only:
            continue
        with tempfile.TemporaryDirectory() as t:
            res = run_case(case, Path(t))
        for r in res:
            print(f"{'PASS' if r['passed'] else 'FAIL'}  {case['name']}: {r['criterion']}: {r['detail']}")
            failed += not r["passed"]
    print(f"{'all criteria passed' if not failed else str(failed) + ' criterion/criteria failed'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
