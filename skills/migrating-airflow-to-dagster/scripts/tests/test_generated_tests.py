"""The pytest files and structural asset checks convert_ir.py writes for each converted DAG. The generated tests are RUN here (pytest on the converter's output)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from test_convert_ir import _py, dag, task  # noqa: E402

dg = pytest.importorskip("dagster")


def convert(tmp_path, tasks, pkg="gpkg", kind="cron_trigger", *extra):
    ir = {"airflow_version": "2.10.5", "dags": [dag(tasks, kind=kind)]}
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    r = subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / pkg / "defs"), "--report", str(tmp_path / "r.json"), *extra],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    (tmp_path / pkg / "__init__.py").write_text("")
    return json.loads((tmp_path / "r.json").read_text())


def pytest_run(tmp_path, *args, env=None):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore", *args, str(tmp_path / "tests")], cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(tmp_path), **(env or {})})


def chain():
    return [_py("numbers", "numbers", retries=1, retry_delay_s=5.0), _py("double", "double", up=["numbers"])] and [
        _py("numbers", "numbers", retries=1, retry_delay_s=5.0),
        task("double", up=["numbers"], callable=__import__("test_convert_ir").fn("double"), op_args=[{"xcom_from": "numbers", "key": "return_value"}], op_kwargs={})]


def test_python_dag_gets_a_test_file_that_passes_and_checks_that_run(tmp_path):
    rep = convert(tmp_path, chain())
    d = rep["dags"]["d1"]
    assert d["generated_tests"]["file"] == "tests/test_d1.py" and d["generated_tests"]["materializes"] is True
    assert d["asset_checks"] == [{"name": "returned_value_present", "asset": "d1/numbers", "kind": "returned_value"}]
    assert rep["generated"] == {"test_files": 1, "test_functions": 5, "materialize_tests": 1, "materialize_skipped": 0, "asset_checks": 1}
    r = pytest_run(tmp_path)
    assert r.returncode == 0 and "5 passed" in r.stdout, r.stdout + r.stderr          # load, shape, job and schedule, checks, and a materialize that ran the checks


def test_the_generated_test_catches_a_hand_edit_that_drifts_from_the_airflow_dag(tmp_path):
    convert(tmp_path, chain())
    p = tmp_path / "gpkg" / "defs" / "d1.py"
    p.write_text(p.read_text().replace("max_retries=1", "max_retries=7"))
    r = pytest_run(tmp_path)
    assert r.returncode != 0 and "test_asset_keys_dependencies_pool_and_retries_match_the_airflow_dag" in r.stdout


def test_a_shell_dag_does_not_run_its_commands_unless_asked(tmp_path):
    convert(tmp_path, [task("sh", "BashOperator", template_fields={"bash_command": "echo hi", "env": None, "cwd": None}, args={}, do_xcom_push=True)])
    r = pytest_run(tmp_path, "-rs")
    assert r.returncode == 0 and "1 skipped" in r.stdout and "CONVERTED_TESTS_RUN_SHELL=1" in r.stdout, r.stdout
    r2 = pytest_run(tmp_path, env={"CONVERTED_TESTS_RUN_SHELL": "1"})
    assert r2.returncode == 0 and "5 passed" in r2.stdout, r2.stdout


def test_an_external_dag_gets_shape_tests_and_a_visible_skip_with_the_reason(tmp_path):
    tf = {"aws_conn_id": "aws_default", "s3_bucket": "b", "s3_key": "out/k.txt", "data": "hi", "replace": True, "encoding": None}
    rep = convert(tmp_path, [task("put", "S3CreateObjectOperator", template_fields=tf, args={})])
    d = rep["dags"]["d1"]
    assert d["generated_tests"]["materializes"] is False and "external systems" in d["generated_tests"]["materialize_skip_reason"]
    assert d["asset_checks"] == [{"name": "s3_object_exists", "asset": "d1/put", "kind": "s3_object_exists"}]
    r = pytest_run(tmp_path, "-rs")
    assert r.returncode == 0 and "4 passed, 1 skipped" in r.stdout and "external systems" in r.stdout, r.stdout


def test_the_s3_check_passes_when_the_object_exists_and_fails_when_it_does_not(tmp_path):
    tf = {"aws_conn_id": "aws_default", "s3_bucket": "b", "s3_key": "out/k.txt", "data": "hi", "replace": True, "encoding": None}
    convert(tmp_path, [task("put", "S3CreateObjectOperator", template_fields=tf, args={})])
    sys.path.insert(0, str(tmp_path))
    import importlib
    mod = importlib.import_module("gpkg.defs.d1")

    class Client:
        def __init__(self, present):
            self.present = present
            self.puts = []

        def put_object(self, **kw):
            self.puts.append(kw)

        def head_object(self, **kw):
            if not self.present:
                raise RuntimeError("NoSuchKey")

    assert [c.check_keys for c in [mod.chk_put_s3]][0]
    for present, want in ((True, True), (False, False)):
        defs = dg.Definitions(assets=[mod.t_put], asset_checks=[mod.chk_put_s3], resources={"aws_default": Client(present)})
        r = defs.resolve_implicit_global_asset_job_def().execute_in_process(raise_on_error=False)
        ev = [e.event_specific_data for e in r.all_events if e.event_type_value == "ASSET_CHECK_EVALUATION"]
        assert r.success and len(ev) == 1 and ev[0].passed is want                          # non-blocking: a failed check does not fail the run
        assert dict(ev[0].metadata)["kind"].value == "structural"
    sys.path.remove(str(tmp_path))


def test_checks_are_structural_non_blocking_and_say_so(tmp_path):
    convert(tmp_path, chain())
    src = (tmp_path / "gpkg" / "defs" / "d1.py").read_text()
    assert "blocking=False" in src and "not a business check" in src and "'kind': 'structural'" in src
