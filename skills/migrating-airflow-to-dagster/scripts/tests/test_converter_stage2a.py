"""Converter fixes for the classes found in the stage-2a behaviour comparison (reference/converter-triage.md, "Stage 2a"): repo-local helpers under the `airflow.` namespace,
`__file__`, `ti.map_index`, cross-DAG / prior-run `xcom_pull`. General rules, tested on synthetic code, not on those DAGs."""
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import convert_ir as C  # noqa: E402
from test_convert_ir import _fake_ctx, _runtime  # noqa: E402


def _info(path, name):
    import ast
    src = Path(path).read_text()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == name)
    return {"name": name, "file": str(path), "params": [a.arg for a in node.args.args], "source": ast.get_source_segment(src, node, padded=False)}


def test_a_helper_the_project_keeps_under_the_airflow_namespace_is_inlined_as_plain_python(tmp_path):
    lib = tmp_path / "src" / "airflow" / "example_dags" / "libs"
    lib.mkdir(parents=True)
    (lib / "helper.py").write_text("SCALE = 3\n\n\ndef _inner(x):\n    return x * SCALE\n\n\ndef print_stuff(x):\n    print(_inner(x))\n")
    dagf = tmp_path / "src" / "airflow" / "example_dags" / "d.py"
    dagf.write_text("from airflow.example_dags.libs.helper import print_stuff\n\n\ndef run():\n    print_stuff(2)\n")
    code, support = C.resolve_callable(_info(dagf, "run"))
    text = "\n".join(support)
    assert "airflow" not in text and text.index("SCALE = 3") < text.index("def _inner") < text.index("def print_stuff")
    ns: dict = {}
    exec("\n".join(support) + "\n" + code, ns)
    ns["run"]()


def test_a_real_airflow_import_is_still_refused(tmp_path):
    dagf = tmp_path / "d.py"
    dagf.write_text("from airflow.providers.foo.hooks import FooHook\n\n\ndef run():\n    return FooHook().x()\n")
    with pytest.raises(C.Defer):
        C.resolve_callable(_info(dagf, "run"))


def test_file_is_a_shim_constant_naming_the_original_dag_file_overridable_by_env(tmp_path, monkeypatch):
    dagf = tmp_path / "d.py"
    dagf.write_text("import os\n\n\ndef run():\n    return os.path.basename(__file__)\n")
    code, support = C.resolve_callable(_info(dagf, "run"))
    assert "__file__" not in code and "_AF_DAG_FILE" in code
    ns: dict = {}
    exec("\n".join(support) + "\n" + code, ns)
    assert ns["run"]() == "d.py"
    monkeypatch.setenv("AIRFLOW_DAG_FILE", "/elsewhere/other.py")
    ns2: dict = {}
    exec("\n".join(support) + "\n" + code, ns2)
    assert ns2["run"]() == "other.py"


def test_ti_map_index_is_minus_one_unmapped_and_the_dynamic_mapping_index_when_mapped():
    rt = _runtime()
    assert rt["af_context"](_fake_ctx(), "d", "t", {}, {})["ti"].map_index == -1
    c = _fake_ctx()
    c.get_mapping_key = lambda: "i3"
    assert rt["af_context"](c, "d", "t", {}, {})["ti"].map_index == 3
    C.resolve_callable  # the static check admits ti.map_index
    assert "map_index" in C.CTX_ATTRS["ti"]


class _Inst:
    def __init__(self, mats):
        self.mats = mats

    def get_latest_materialization_event(self, key):
        import dagster as dg
        md = self.mats.get(tuple(key.path))
        return types.SimpleNamespace(asset_materialization=types.SimpleNamespace(metadata={k: dg.MetadataValue.int(v) if isinstance(v, int) else dg.MetadataValue.text(v) for k, v in md.items()})) if md else None


def test_cross_dag_and_prior_run_xcom_pull_read_the_latest_materialization_metadata_or_none():
    rt = _runtime()
    c = _fake_ctx()
    c.instance = _Inst({("other_dag", "get"): {"n": 7}})
    ti = rt["af_context"](c, "d", "t", {}, {})["ti"]
    assert ti.xcom_pull(dag_id="other_dag", task_ids="get", key="n", include_prior_dates=True) == 7
    assert ti.xcom_pull(dag_id="never_ran", task_ids="get", key="n") is None             # Airflow: no such XCom is None
    assert ti.xcom_pull(dag_id="never_ran", task_ids="get", key="n", default="d") == "d"
    with pytest.raises(NotImplementedError, match="another DAG"):                          # a return value of another DAG stays loud, naming the Dagster equivalent
        ti.xcom_pull(dag_id="other_dag", task_ids="get")
