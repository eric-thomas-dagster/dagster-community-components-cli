"""The skill-level evals (evals/cases.json) run through scripts/run_evals.py."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
pytest.importorskip("dagster")


def test_all_eval_cases_pass():
    r = subprocess.run([sys.executable, str(HERE.parent / "run_evals.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("PASS") == 15 and "FAIL" not in r.stdout


def test_a_planted_secret_in_the_output_fails_the_eval(tmp_path, monkeypatch):
    sys.path.insert(0, str(HERE.parent))
    import run_evals as RE
    case = json.loads((HERE.parent.parent / "evals" / "cases.json").read_text())["cases"][2]
    out = RE.run_case({**case, "forbid": ["secret_args"]}, tmp_path)         # "secret_args" is the DAG id, which does appear in the output: stands in for a leaked value
    assert not {r["criterion"]: r["passed"] for r in out}["no_secret_values"]
