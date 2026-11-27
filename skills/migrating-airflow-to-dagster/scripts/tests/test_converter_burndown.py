"""Converter burn-down (reference/converter-triage.md, "Burn-down, converter"). Each test has real Airflow parse a small DAG, converts what the extractor wrote, and RUNS the emitted Dagster code."""
import json
import sys
from pathlib import Path

import pytest

dg = pytest.importorskip("dagster")
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import convert_ir as C  # noqa: E402,F401
from test_converter_stage2a_v6 import graph_assets, values  # noqa: E402
from test_converter_stage2a_v8 import extract_and_convert, load_dag, run_assets  # noqa: E402


_N = [0]


def run(tmp_path, source, dag_id="d", major=2, flags=(), collect=True):
    _N[0] += 1                                    # a package name per call: modules are cached by name
    irs, rep, pkg = extract_and_convert(tmp_path, source, major=major, pkg=f"bdpk{_N[0]}", flags=flags)
    mod = load_dag(pkg, dag_id)
    res = run_assets(mod)
    return irs, rep[dag_id], mod, res, (values(res, mod) if collect else None)


# ---- F-BR: a branch / short-circuit asset's value is what Airflow's XCom holds ------------------------------------------------------------------------------------

BRANCHES = '''
    import json, pendulum
    from airflow.decorators import dag, task
    from airflow.operators.python import ShortCircuitOperator

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task.branch
        def pick_one():
            return "left"

        @task.branch
        def pick_many():
            return ["a1", "a2"]

        @task
        def left(): print("LEFT")
        @task
        def right(): print("RIGHT")
        @task
        def a1(): print("A1")
        @task
        def a2(): print("A2")
        @task
        def a3(): print("A3")

        @task.short_circuit
        def gate_false():
            return False

        @task.short_circuit
        def gate_true():
            return True

        @task
        def after_false(): print("AFTER_FALSE")
        @task
        def after_true(): print("AFTER_TRUE")

        o = pick_one(); o >> [left(), right()]
        m = pick_many(); m >> [a1(), a2(), a3()]
        gate_false() >> after_false()
        gate_true() >> after_true()
    d()
'''


def test_branch_and_short_circuit_assets_hold_the_plain_value_and_still_gate(tmp_path, capfd):
    _, _, mod, res, vals = run(tmp_path, BRANCHES)
    assert res.success
    got = {k: v for k, v in vals.items()}
    assert json.dumps(got["d/pick_one"]) == '"left"'
    assert json.loads(json.dumps(got["d/pick_many"])) == ["a1", "a2"]            # a plain-JSON value: the comparison no longer sees an object repr
    assert json.dumps(got["d/gate_false"]) == "false" and json.dumps(got["d/gate_true"]) == "true"
    out = capfd.readouterr().out
    assert "LEFT" in out and "RIGHT" not in out and "A1" in out and "A2" in out and "A3" not in out
    assert "AFTER_FALSE" not in out and "AFTER_TRUE" in out


# ---- F-CTX: a mapped task inside a task-group loop (Airflow 3.4) --------------------------------------------------------------------------------------------------

MAPPED_LOOP = '''
    from airflow.sdk import dag, task, task_group

    @dag(dag_id="d", schedule=None, catchup=False)
    def d():
        @task_group
        def process_batch():
            @task
            def choose_items(*, loop):
                return [1, 2] if loop.index == 1 else [value + 1 for value in loop.previous]

            @task
            def process(value, *, loop, ti):
                return value + loop.index + 100 * ti.map_index

            process.expand(value=choose_items())

        def batch_ready(*, loop):
            return min(loop.result) >= 3

        process_batch.loop(max_iterations=3, until=batch_ready)
    d()
'''


def test_a_mapped_task_in_a_loop_runs_per_item_per_iteration_with_loop_and_map_index(tmp_path):
    _, rep, mod, res, vals = run(tmp_path, MAPPED_LOOP, major=3)
    assert res.success and not rep.get("caveats"), rep.get("caveats")
    assert list(vals.values())[0] == [5, 206], vals                # iteration 1: [2, 103] (min < 3); iteration 2: choose [3, 104] -> [5, 206]


# ---- F-CTX: test_mode, task.params, an Airflow-style run_id, one logical date per run --------------------------------------------------------------------------------

CTX_NAMES = '''
    from airflow.decorators import dag, task

    @dag(dag_id="d", schedule=None, catchup=False)
    def d():
        @task
        def show(params, test_mode=None, task=None, run_id=None, ti=None, dag_run=None, logical_date=None):
            return {"test_mode": test_mode, "miff": task.params["miff"], "p": params["miff"], "run_id": run_id, "ti_run_id": ti.run_id, "dr": dag_run.run_id, "ld": logical_date.isoformat()}

        @task
        def second(logical_date=None, run_id=None):
            return {"ld": logical_date.isoformat(), "run_id": run_id}

        show(); second()
    d()
'''


def test_context_has_test_mode_false_task_params_and_airflow_style_run_id_shared_by_a_run(tmp_path):
    src = CTX_NAMES.replace("def show(", "def show(").replace("        @task\n        def show", "        @task(params={'miff': 'agg'})\n        def show")
    _, rep, mod, res, vals = run(tmp_path, src, flags=())
    assert res.success and not rep.get("caveats"), (rep.get("caveats"), vals)
    a, b = (res.asset_value(dg.AssetKey(["d", n])) for n in ("show", "second"))
    assert a["test_mode"] is False and a["miff"] == "agg" and a["p"] == "agg"
    assert a["run_id"].startswith("manual__") and a["run_id"] == a["ti_run_id"] == a["dr"] == b["run_id"]
    assert a["ld"] == b["ld"] and a["run_id"] == "manual__" + a["ld"]          # one logical date for every task of the run, as in Airflow


# ---- F-XPULL: ti.xcom_pull by task id (and map_indexes) in a graph-form DAG; a mapped group's failed member; task objects named in a callable --------------------------------

PULL_GRAPH = '''
    from airflow.decorators import dag, task, task_group
    from airflow.operators.empty import EmptyOperator
    import pendulum
    from datetime import timedelta

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False, default_args={"retries": 2, "retry_delay": timedelta(seconds=0)})
    def d():
        @task_group(group_id="g")
        def tg(n):
            @task
            def add_42(num):
                if num == 3:
                    raise RuntimeError("item 3 failed")
                return num + 42
            add_42(n)

        @task
        def pull(**context):
            ti = context["ti"]
            return {"idx": ti.xcom_pull(task_ids=["g.add_42"], map_indexes=[0, 2]), "one": ti.xcom_pull(task_ids="g.add_42", map_indexes=1),
                    "lazy": repr(ti.xcom_pull(task_ids=["g.add_42"], map_indexes=[0, 2]))}

        @task.branch
        def choose():
            return other.operator.task_id

        other = EmptyOperator(task_id="other")
        mapped = tg.expand(n=[1, 2, 4])
        mapped >> pull()
        choose() >> other
    d()
'''


def test_xcom_pull_by_task_id_and_map_indexes_in_a_graph_form_dag_and_a_named_task_object(tmp_path):
    irs, rep, mod, res, vals = run(tmp_path, PULL_GRAPH)
    assert res.success and not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]
    p = vals["d/pull"]
    assert p["idx"] == [43, 46] and p["one"] == 44                    # the members' values over the items: add_42 of 1, 2, 4
    assert p["lazy"] == "LazySelectSequence([2 items])"                 # Airflow 2 returns a lazy sequence for several task ids / indexes
    assert vals["d/choose"] == "other"                                  # `other.operator.task_id` inside the callable names the task it was built for


def test_a_failed_member_of_a_mapped_group_fails_only_itself_and_the_run_and_declares_its_retries(tmp_path):
    src = PULL_GRAPH.replace("[1, 2, 4]", "[1, 3, 4]")
    irs, rep, mod, res, vals = run(tmp_path, src, collect=False)
    assert not res.success                                              # the run fails at the end, as Airflow's does
    meta = {ev.asset_key.to_user_string(): getattr(ev.materialization.metadata.get("airflow/state"), "value", None) for ev in res.get_asset_materialization_events()}
    assert meta.get("d/g/add_42") == "failed" and meta.get("d/pull") == "upstream_failed"
    text = (tmp_path / f"bdpk{_N[0]}" / "defs" / "d.py").read_text()
    assert "retry_policy=dg.RetryPolicy(max_retries=2" in text          # declared per op even though a failure guard raises the retry itself


# ---- F-TPL: templates_dict / templates_exts file templating --------------------------------------------------------------------------------------------------------

TPL = '''
    import pendulum
    from airflow import DAG
    from airflow.operators.python import PythonOperator

    def show(templates_dict=None, **kwargs):
        return templates_dict["query"]

    with DAG("d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False) as d:
        PythonOperator(task_id="t", python_callable=show, templates_dict={"query": "sql/q.sql"}, templates_exts=[".sql"])
'''


def test_templates_dict_file_is_read_and_rendered_and_a_missing_file_fails_like_airflow(tmp_path):
    (tmp_path / "ok" / "dags" / "sql").mkdir(parents=True)
    # a present file: its text, rendered, reaches the callable (the extractor reads it from the DAG folder)
    d = tmp_path / "ok"
    (d / "dags" / "sql" / "q.sql").write_text("select '{{ params.x }}' as v")
    import textwrap
    (d / "dags" / "d.py").write_text(textwrap.dedent(TPL).replace('catchup=False)', 'catchup=False, params={"x": "7"})'))
    import subprocess
    out = d / "ir.json"
    from test_converter_stage2a_v8 import af_python
    subprocess.run([af_python(2), "-W", "ignore", str(HERE.parent / "airflow_ir.py"), str(d / "dags"), "--out", str(out), "--pythonpath", str(d / "dags")], check=True, capture_output=True, timeout=300)
    ir = json.loads(out.read_text())["dags"][0]
    assert ir["tasks"][0]["template_files"] == {"templates_dict.query": "select '{{ params.x }}' as v"}
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(out), "--out-dir", str(d / "pkok" / "defs"), "--report", str(d / "r.json"), "--return-all"], check=True, capture_output=True)
    (d / "pkok" / "__init__.py").write_text("")
    sys.path.insert(0, str(d))
    import importlib
    okmod = importlib.import_module("pkok.defs.d")
    okres = run_assets(okmod)
    assert okres.success and values(okres, okmod)["d/t"] == "select '7' as v"        # the file's text rendered with the task's params, as Airflow hands it to the callable
    # a missing file: the task fails with TemplateNotFound before the callable runs
    irs, rep, mod, res, _ = run(tmp_path / "missing", TPL, collect=False) if (tmp_path / "missing").mkdir() is None else None
    assert not res.success and not rep.get("caveats")
    assert any("TemplateNotFound" in str(e.event_specific_data) or "sql/q.sql" in str(e.event_specific_data) for e in res.all_events if e.event_type_value == "STEP_FAILURE")


# ---- F-MAPARG: expand(op_args=..., op_kwargs=...) --------------------------------------------------------------------------------------------------------------------

OPARGS = '''
    import pendulum
    from airflow import DAG
    from airflow.operators.python import PythonOperator

    def add_num(x, y, word):
        return f"{x+y}: {word}"

    def add3(x, y, z):
        return x + y + z

    with DAG("d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False) as d:
        PythonOperator.partial(task_id="mix", python_callable=add_num).expand(op_args=list(zip([1, 2, 3], [10, 20, 30])), op_kwargs=[{"word": "hi"}, {"word": "bye"}])
        PythonOperator.partial(task_id="zipped", python_callable=add3).expand(op_args=list(zip([1, 2, 3], [10, 20, 30], [100, 200, 300])))
'''


def test_mapped_op_args_and_op_kwargs_cross_product_matches_airflows_order(tmp_path):
    irs, rep, mod, res, vals = run(tmp_path, OPARGS)
    assert res.success and not rep.get("caveats"), rep
    assert vals["d/mix"] == ["11: hi", "11: bye", "22: hi", "22: bye", "33: hi", "33: bye"]      # what real Airflow 2.10 records (product order, first argument slowest; the DAG's own comment lists another)
    assert vals["d/zipped"] == [111, 222, 333]


# ---- F-OPX: a user class that only overrides __init__ is the stock operator it extends ---------------------------------------------------------------------------------

SUBCLASS = '''
    import pendulum
    from airflow import DAG
    from airflow.operators.bash import BashOperator

    class Loud(BashOperator):
        def __init__(self, **kwargs):
            self.deferrable = True
            super().__init__(**kwargs)

    with DAG("d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False) as d:
        Loud(task_id="t", bash_command="echo subclass-ran")
'''


def test_a_subclass_that_only_overrides_init_runs_as_the_stock_operator(tmp_path):
    irs, rep, mod, res, vals = run(tmp_path, SUBCLASS)
    assert res.success and not rep.get("caveats"), rep
    assert vals["d/t"] == "subclass-ran"
    assert C.stock_base({"type": "X", "operator_class": {"source": "class X(BashOperator):\n    def execute(self, context):\n        return 1\n", "mro": ["BashOperator"]}}) is None      # its own behaviour: not the stock one


# ---- F-DEP: product folding must not add ordering Airflow does not have ------------------------------------------------------------------------------------------------

FOLD = '''
    import pendulum
    from airflow import DAG
    from airflow.operators.bash import BashOperator

    with DAG("d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False) as d:
        a, b = (BashOperator(task_id=n, bash_command=f"echo {n}", retries=1) for n in ("a", "b"))        # own retries: each stays an asset
        step_a, step_b, leaf = (BashOperator(task_id=n, bash_command=f"echo {n}") for n in ("step_a", "step_b", "leaf"))
        a >> step_a >> leaf
        b >> step_b >> leaf
'''


def test_folding_never_makes_a_step_wait_for_an_upstream_airflow_does_not_give_it(tmp_path):
    irs, rep, mod, res, vals = run(tmp_path, FOLD, flags=("--granularity", "product"))
    dec = rep["granularity"]["decisions"]
    assert res.success
    # step_a and step_b each depend on a different root; folding both into `leaf` would make step_a wait for b (and step_b for a): at most one of them may fold
    folded = [t for t in ("step_a", "step_b") if dec[t]["role"] == "step"]
    assert len(folded) < 2 and any("wait for" in dec[t]["reason"] for t in ("step_a", "step_b") if dec[t]["role"] == "product"), dec


# ---- F-OBJ: a relative file:// object-storage path -----------------------------------------------------------------------------------------------------------------------

OBJ = '''
    import pendulum
    from airflow.decorators import dag, task
    from airflow.io.path import ObjectStoragePath

    base = ObjectStoragePath("file://rel/bears")

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False)
    def d():
        @task
        def put(base):
            base.mkdir(parents=True, exist_ok=True)
            f = base / "bear.txt"
            f.write_text("x")
            f.replace("bear.txt")            # Airflow 2 (universal_pathlib 0.2): a relative target is a sibling
            return str(f)
        put(base)
    d()
'''


def test_a_relative_file_object_path_resolves_against_the_working_directory_and_replace_keeps_airflow_2_semantics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    irs, rep, mod, res, vals = run(tmp_path, OBJ)
    assert res.success, rep
    assert (tmp_path / "rel" / "bears" / "bear.txt").read_text() == "x" and not (tmp_path / "bear.txt").exists()
    ir_arg = json.dumps(irs["d"]["tasks"][0].get("op_args"))
    assert "file://rel/bears" in ir_arg and str(tmp_path) not in ir_arg            # recorded as written, not as the extractor's absolute path


# ---- F-MTG: a trigger rule on a member of a mapped task group -------------------------------------------------------------------------------------------------------------

MTG = '''
    import pendulum
    from datetime import timedelta
    from airflow.decorators import dag, task, task_group
    from airflow.operators.python import PythonOperator

    def boom():
        raise RuntimeError("upstream failed")

    @dag(dag_id="d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False, default_args={"retries": 0})
    def d():
        bad = PythonOperator(task_id="bad", python_callable=boom)

        @task_group
        def grp(n):
            @task(trigger_rule="all_done")
            def always(n):
                return n * 10

            @task
            def plain(v):
                return v + 1

            plain(always(n))

        bad >> grp.expand(n=[1, 2])
    d()
'''


def test_a_trigger_rule_member_of_a_mapped_group_runs_after_a_failed_upstream_and_its_plain_sibling_follows(tmp_path):
    irs, rep, mod, res, _ = run(tmp_path, MTG, collect=False)
    assert not [c for c in rep["caveats"] if c["kind"] == "stub"], rep["caveats"]
    meta = {ev.asset_key.to_user_string(): getattr(ev.materialization.metadata.get("airflow/state"), "value", None) for ev in res.get_asset_materialization_events()}
    assert meta.get("d/bad") == "failed"
    assert meta.get("d/grp/always") is None and meta.get("d/grp/plain") is None             # all_done ran although `bad` failed; the plain member after it ran too
    assert res.success                                                                        # Airflow decides the run by its leaves: the group's tasks succeeded, so the run did (`bad` is failed, not a leaf)
