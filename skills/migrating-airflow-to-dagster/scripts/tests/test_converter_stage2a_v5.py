"""Converter fixes from the second reading of the stage-2a `different` rows (reference/converter-triage.md, "Stage 2a, pass 2"): general rules on synthetic code."""
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import convert_ir as C  # noqa: E402
from test_convert_ir import _fake_ctx, _runtime  # noqa: E402
from test_converter_stage2a import _info  # noqa: E402


def test_ti_used_only_for_its_own_attributes_pulls_nothing(tmp_path):
    f = tmp_path / "d.py"
    f.write_text("def run(ti=None):\n    return ti.map_index\n\n\ndef pull(ti=None):\n    return ti.xcom_pull(task_ids='a')\n\n\ndef passed(ti=None):\n    return helper(ti)\n")
    m1 = {}
    C.resolve_callable(_info(f, "run"), meta=m1)
    assert m1["needs_ti"] and not m1["needs_xcom"]


def test_xcom_pull_and_passing_ti_on_still_need_the_upstream_values(tmp_path):
    f = tmp_path / "d.py"
    f.write_text("def pull(ti=None):\n    return ti.xcom_pull(task_ids='a')\n\n\ndef helper(x):\n    return x\n\n\ndef passed(ti=None):\n    return helper(ti)\n")
    for name in ("pull", "passed"):
        m = {}
        C.resolve_callable(_info(f, name), meta=m)
        assert m["needs_xcom"], name


def test_task_state_store_is_a_json_file_per_task_instance_kept_across_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRFLOW_TASK_STATE_DIR", str(tmp_path))
    rt = _runtime()
    s1 = rt["af_context"](_fake_ctx(), "d", "t", {}, {})["task_state_store"]
    assert s1.get("k") is None and s1.get("k", 5) == 5
    s1.set("k", {"a": 1})
    s2 = rt["af_context"](_fake_ctx(), "d", "t", {}, {})["task_state_store"]          # a retry: same run, same task
    assert s2.get("k") == {"a": 1}
    other = rt["af_context"](_fake_ctx(), "d", "other", {}, {})["task_state_store"]
    assert other.get("k") is None
    s2.delete("k")
    assert s1.get("k") is None
    assert "task_state_store" in C.SUPPORTED_CTX


def test_an_elided_outlet_task_is_an_asset_that_runs_and_returns_nothing_even_with_an_s3_uri(tmp_path):
    """The asset form used to fall through to the S3 upload branch for an EmptyOperator with an s3:// outlet (a boto upload with no arguments)."""
    from test_convert_ir import task as _task
    from test_granularity import conv
    p, r = conv(tmp_path, [_task("producer", "EmptyOperator", outlets=["s3://dataset/example.csv"]), _task("wait", "BashOperator", template_fields={"bash_command": "true"})], "pe")
    text = (Path(p) / "pk" / "defs" / "d1.py").read_text()
    assert "s3_upload_file" not in text and "aws_default" not in text


def test_component_marker_is_in_the_report_when_components_are_emitted(tmp_path):
    import json
    import subprocess
    from test_convert_ir import dag as _dag, task as _task
    ir = {"dags": [_dag([_task("a", "BashOperator", template_fields={"bash_command": "echo hi"})], kind="once")]}
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pkg" / "defs"), "--report", str(tmp_path / "r.json"), "--output", "components"],
                   check=True, capture_output=True)
    rep = json.loads((tmp_path / "r.json").read_text())["dags"]["d1"]
    assert rep.get("converted_with_component") is True and "ShellCommandAssetComponent" in rep["component_types"]
