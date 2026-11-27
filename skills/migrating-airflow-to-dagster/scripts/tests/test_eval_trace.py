"""The observation layer (parity/call_trace.py, parity/trace_compare.py): the user's own functions and the commands a run executed, recorded in the sandbox on both sides and compared.

Unit tests use synthetic functions; the end-to-end tests run real Airflow (set EVAL_AIRFLOW_PYTHON to an Airflow 2 interpreter) against the converter's output: a DAG whose only observable effect is the
arguments its functions receive is `thin` without the layer and substantive with it, and a changed argument in the converted code is caught.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "parity"))
import behavior_compare as B  # noqa: E402
import call_trace as CT  # noqa: E402
import trace_compare as TC  # noqa: E402

needs_monitoring = pytest.mark.skipif(not hasattr(sys, "monitoring"), reason="the call trace needs Python 3.12 (sys.monitoring)")
AF_PY = os.environ.get("EVAL_AIRFLOW_PYTHON")
needs_airflow = pytest.mark.skipif(not AF_PY, reason="set EVAL_AIRFLOW_PYTHON to an Airflow 2 interpreter to run the end-to-end trace tests")


# ---- value text ------------------------------------------------------------------------------------------------------------------------------------------
def test_plain_values_are_stable_text_and_the_rest_is_opaque():
    import datetime
    assert CT.plain({"b": [1, 2.5, None], "a": ("x",)}) == "{'a': ('x',), 'b': [1, 2.5, None]}"
    assert CT.plain({3, 1, 2}) == "{1, 2, 3}"
    assert CT.plain(datetime.date(2024, 1, 2)) == "<date 2024-01-02>"
    assert CT.plain(object()) == "<obj>"
    assert CT.plain([object(), 1]) == "[<obj>, 1]"

    class Frame:
        shape, columns = (2, 3), ["a"]
    assert CT.plain(Frame()) == "<Frame (2, 3)>"
    big = CT.plain(list(range(500)))
    assert "...(len 500)" in big and len(big) < 400
    assert "...(len 1000)" in CT.plain("x" * 1000)
    ctx = {"ti": object(), "ds": "2024-01-01", "dag": object(), "run_id": "r"}
    assert CT.plain(ctx) == "{'dag': <obj>, 'ds': '2024-01-01', 'run_id': 'r', 'ti': <obj>}"          # a plain dict is data
    from collections.abc import MutableMapping

    class Context(dict, MutableMapping):
        pass
    c = Context(ctx)
    assert CT.plain(c) == "<airflow-context>"


def test_credentials_in_a_command_are_masked():
    assert "hunter2" not in CT.mask_secrets("curl -H 'Authorization: Bearer abc123' https://x?password=hunter2")
    assert CT.mask_secrets("psql postgres://user:s3cret@host/db") == "psql postgres://user:<masked>@host/db"
    assert CT.mask_secrets("echo API_KEY=abc def") == "echo API_KEY=<masked> def"
    assert CT.mask_secrets("echo hello") == "echo hello"


# ---- the tracer ------------------------------------------------------------------------------------------------------------------------------------------
MOD = '''
import os


def add(a, b=2, *rest, flag=False, **kw):
    return a + b


def boom(x):
    raise ValueError("no")


def gen(n):
    for i in range(n):
        yield i


def outer(x):
    return add(x, 1) + add(x, b=10)


def helper_lambda():
    f = lambda y: y + 1
    return [f(1), [z for z in range(3)]]


class K:
    def __init__(self, v):
        self.v = v

    def get(self, scale):
        return self.v * scale
'''


@pytest.fixture
def user_module(tmp_path):
    (tmp_path / "usermod.py").write_text(MOD)
    sys.path.insert(0, str(tmp_path))
    import importlib
    sys.modules.pop("usermod", None)
    mod = importlib.import_module("usermod")
    yield mod, tmp_path
    sys.path.remove(str(tmp_path))
    sys.modules.pop("usermod", None)


@needs_monitoring
def test_user_calls_are_recorded_with_arguments_by_name_returns_and_exceptions(user_module):
    mod, root = user_module
    tr = CT.Tracer([str(root)]).start()
    try:
        assert mod.outer(5) == 6 + 15
        with pytest.raises(ValueError):
            mod.boom(1)
        list(mod.gen(2))
        assert mod.add(1, 2, 3, flag=True, k="v") == 3
        mod.helper_lambda()
        assert mod.K(3).get(4) == 12
    finally:
        tr.stop()
    snap = tr.snapshot()
    by = {}
    for c in snap["calls"]:
        by.setdefault(c["fn"], []).append(c)
    assert [c["args"] for c in by["outer"]] == [{"x": "5"}] and by["outer"][0]["ret"] == "21"
    assert {c["ret"] for c in by["add"]} == {"6", "15", "3"}
    last = by["add"][-1]
    assert last["args"] == {"a": "1", "b": "2", "flag": "True", "*rest": "(3,)", "**k": "'v'"}
    assert by["boom"][0]["exc"] == "ValueError" and by["boom"][0]["ret"] is None
    assert by["gen"][0]["ret"] == "<generator>" and by["gen"][0]["args"] == {"n": "2"}
    assert by["get"][0]["args"] == {"scale": "4"} and by["get"][0]["ret"] == "12"                  # `self` is not recorded
    assert "<lambda>" not in by and "__init__" not in by and "<listcomp>" not in by and "<module>" not in by
    assert all(c["file"] == "usermod" and len(c["fp"]) == 12 for c in snap["calls"])


@needs_monitoring
def test_only_files_under_the_roots_count_and_nothing_is_recorded_when_stopped(user_module, tmp_path_factory):
    mod, root = user_module
    other = tmp_path_factory.mktemp("elsewhere")
    tr = CT.Tracer([str(other)]).start()
    mod.add(1)
    tr.stop()
    assert tr.snapshot()["calls"] == []                                       # the module is not under the root
    tr2 = CT.Tracer([str(root)]).start()
    tr2.stop()
    mod.add(1)
    assert tr2.snapshot()["calls"] == []                                      # stopped: nothing


@needs_monitoring
def test_the_event_cap_is_a_count_not_a_crash(user_module):
    mod, root = user_module
    tr = CT.Tracer([str(root)], cap=3).start()
    for i in range(10):
        mod.add(i)
    tr.stop()
    s = tr.snapshot()
    assert len(s["calls"]) == 3 and s["dropped"]["calls"] == 7 and s["cap"] == 3
    why = TC._incomplete(s, "the route")
    assert why and "cap" in why


@needs_monitoring
def test_commands_are_recorded_with_exit_code_and_without_env_values(tmp_path):
    tr = CT.Tracer([str(tmp_path)]).start()
    try:
        subprocess.run(["bash", "-c", "echo hi && exit 3"], env={**os.environ, "EVAL_TRACE_SECRET_X": "value"})
        subprocess.run("echo token=abc123", shell=True, capture_output=True)
        subprocess.run(["true"])
        os.system("exit 4")
    finally:
        tr.stop()
    cmds = tr.snapshot()["commands"]
    assert [c["cmd"] for c in cmds] == ["echo hi && exit 3", "echo token=<masked>", "true", "exit 4"]
    assert [c["rc"] for c in cmds] == [3, 0, 0, 4]
    assert cmds[0]["env_added"] == ["EVAL_TRACE_SECRET_X"] and "value" not in json.dumps(cmds)         # names only, never values
    assert cmds[1]["shell"] is True and cmds[2]["shell"] is False


# ---- the comparison (pure) -------------------------------------------------------------------------------------------------------------------------------
def call(fn, args=None, ret="None", exc=None, fp=None):
    return {"fn": fn, "file": "m", "fp": fp or f"fp_{fn}", "args": args or {}, "ret": ret, "exc": exc}


def trace(calls=(), commands=(), dropped=0):
    return {"engine": "sys.monitoring", "cap": 100, "dropped": {"calls": dropped, "commands": 0}, "calls": list(calls), "commands": list(commands)}


SPEC = {"dag_id": "d", "tasks": [{"task_id": "load", "callable": {"name": "load_rows"}, "downstream": [], "upstream": []}, {"task_id": "prep", "callable": {"name": "prep_rows"}, "downstream": [], "upstream": []}]}


def cc(base, route, base2=None, spec=SPEC, kind="converter", excused=None, partition_key=None):
    a1 = {"calltrace": trace(base), "run_epoch": None}
    a2 = {"calltrace": trace(base2 if base2 is not None else base)}
    r = {"calltrace": trace(route), "partition_key": partition_key}
    return TC.compare_calls(spec, a1, a2, r, {}, kind, excused)


def test_equal_calls_match_and_name_the_converter_wrappers_it_ignored():
    base = [call("load_rows", {"n": "3"}, "[1, 2, 3]"), call("helper", {"x": "1"}, "2")]
    route = [call("t_load", {"context": "<obj>"}, "[1, 2, 3]"), call("_af_cb_x", {"n": "3"}, "[1, 2, 3]", fp="fp_load_rows"), call("helper", {"x": "1"}, "2")]
    d = cc(base, route)
    assert d["verdict"] == "match" and d["functions"] == 2 and d["calls"] == 2
    assert d["evidence"]["converter_wrappers_ignored"] == ["t_load"]
    assert d["evidence"]["paired_by"]["_af_cb_x->load_rows"] == "body"


def test_the_converters_task_name_pairs_with_the_tasks_callable_even_when_the_body_changed():
    base = [call("load_rows", {"n": "3"}, "7")]
    route = [call("_af_load", {"n": "3"}, "7", fp="rewritten-body")]
    d = cc(base, route)
    assert d["verdict"] == "match" and d["evidence"]["paired_by"] == {"_af_load->load_rows": "task"}


def test_a_changed_argument_is_a_difference_and_the_evidence_shows_both_calls():
    d = cc([call("load_rows", {"n": "3"}, "7")], [call("_af_load", {"n": "4"}, "7", fp="x")])
    assert d["verdict"] == "differ"
    row = d["evidence"]["differences"][0]
    assert row["function"] == "load_rows" and "n=3" in row["missing_in_route"][0] and "n=4" in row["extra_in_route"][0]


def test_a_changed_return_value_exception_or_call_count_is_a_difference():
    base = [call("f", {"x": "1"}, "2")]
    assert cc(base, [call("f", {"x": "1"}, "3")])["verdict"] == "differ"
    assert cc(base, [call("f", {"x": "1"}, None, exc="KeyError")])["verdict"] == "differ"
    assert cc(base, base + base)["verdict"] == "differ"
    assert cc(base + base, base)["verdict"] == "differ"


def test_a_function_the_route_never_calls_is_a_difference_unless_its_tasks_are_excused_and_never_for_a_rewrite():
    base = [call("prep_rows", {}, "1"), call("f", {}, "1")]
    route = [call("f", {}, "1")]
    d = cc(base, route)
    assert d["verdict"] == "differ" and d["evidence"]["differences"][0]["function"] == "prep_rows" and d["evidence"]["differences"][0]["tasks"] == ["prep"]
    ok = cc(base, route, excused={"prep": "dropped by the route by design (glue)"})
    assert ok["verdict"] == "match" and "prep_rows" in ok["evidence"]["excused"][0]
    rw = cc(base, route, kind="rewrite")
    assert rw["verdict"] == "match" and rw["evidence"]["unaccounted"][0]["function"] == "prep_rows"


def test_route_only_functions_are_listed_never_blamed():
    d = cc([call("f", {}, "1")], [call("f", {}, "1"), call("shim_helper", {}, "1"), call("t_x", {}, "1"), call("t_x", {}, "1")])
    assert d["verdict"] == "match" and d["evidence"]["route_only"] == {"shim_helper": 1} and d["evidence"]["converter_wrappers_ignored"] == ["t_x"]


def test_a_value_that_differs_between_the_baselines_two_runs_is_not_compared_but_its_call_count_is():
    r1, r2 = [call("f", {"x": "0.1"}, "a")], [call("f", {"x": "0.2"}, "a")]
    assert cc(r1, [call("f", {"x": "0.9"}, "a")], base2=r2)["verdict"] == "match"
    d = cc(r1, [], base2=r2)
    assert d["verdict"] == "differ"                                           # never called on the route
    d = cc(r1, r1 + r1, base2=r2)
    assert d["verdict"] == "differ" and d["evidence"]["differences"][0]["difference"].startswith("call count")
    assert cc(r1 + r1, r1, base2=r2 + r2)["verdict"] == "differ"
    excluded = cc(r1, [call("f", {"x": "9"}, "a")] * 3, base2=r2 + r2)
    assert excluded["verdict"] == "not-compared" and "nondeterministic" in excluded["reason"]


def test_timestamps_of_the_run_and_run_ids_are_masked_but_a_fixed_business_date_is_not():
    import time
    now = time.time()
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now))
    t2 = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now + 7))
    ev = lambda v: [call("f", {"when": repr(v)}, "None")]          # noqa: E731
    a1 = {"calltrace": trace(ev(t)), "run_epoch": now}
    r = {"calltrace": trace(ev(t2))}
    assert TC.compare_calls(SPEC, a1, None, r, {}, "converter")["verdict"] == "match"
    a1 = {"calltrace": trace(ev("2020-01-01 10:00:00")), "run_epoch": now}
    assert TC.compare_calls(SPEC, a1, None, {"calltrace": trace(ev("2020-01-01 11:00:00"))}, {}, "converter")["verdict"] == "differ"
    rid = lambda x: [call("f", {"run_id": f"'manual__{x}'"}, "None")]         # noqa: E731
    a1 = {"calltrace": trace(rid("2026-11-27T10:00:00+00:00")), "run_epoch": now}
    assert TC.compare_calls(SPEC, a1, None, {"calltrace": trace(rid("2026-11-27T10:00:07+00:00"))}, {}, "converter")["verdict"] == "match"


def test_an_opaque_value_matches_a_plain_one_except_none_and_context_kwargs_are_not_compared():
    base = [call("f", {"ti": "<obj>", "n": "1", "**ds": "'2026-11-27'", "**extra": "5"}, "<obj>")]
    assert cc(base, [call("f", {"ti": "'anything'", "n": "1", "**extra": "5"}, "[1]")])["verdict"] == "match"
    assert cc(base, [call("f", {"ti": "None", "n": "1", "**extra": "5"}, "[1]")])["verdict"] == "differ"          # None where Airflow passes an object
    assert cc(base, [call("f", {"ti": "<obj>", "n": "1", "**extra": "6"}, "<obj>")])["verdict"] == "differ"       # a real kwarg
    base = [call("f", {"ctx": "<airflow-context>"}, "None")]
    assert cc(base, [call("f", {"ctx": "{'a': 1}"}, "None")])["verdict"] == "match"


def test_the_partition_key_is_read_as_none():
    base = [call("f", {"key": "None"}, "None")]
    assert cc(base, [call("f", {"key": "'2024-01-03'"}, "'2024-01-03'")], partition_key="2024-01-03")["verdict"] == "match"
    assert cc(base, [call("f", {"key": "'2024-01-03'"}, "'x'")], partition_key="2024-01-03")["verdict"] == "differ"


def test_a_missing_incomplete_or_empty_trace_is_not_compared_with_the_reason():
    assert "no call trace" in TC.compare_calls(SPEC, {}, None, {"calltrace": trace()}, {}, "converter")["reason"]
    assert "no call trace" in TC.compare_calls(SPEC, {"calltrace": trace([call("f")])}, None, {}, {}, "converter")["reason"]
    d = TC.compare_calls(SPEC, {"calltrace": trace([call("f")])}, None, {"calltrace": trace([call("f")], dropped=2)}, {}, "converter")
    assert d["verdict"] == "not-compared" and "cap" in d["reason"]
    d = cc([], [call("f")])
    assert d["verdict"] == "not-compared" and "no user function" in d["reason"]


def cmd(text, rc=0):
    return {"cmd": text, "shell": True, "cwd": "/x", "env_added": [], "rc": rc}


def cm(base, route, base2=None, kind="converter", spec=None):
    return TC.compare_commands({"calltrace": trace(commands=base)}, {"calltrace": trace(commands=base2 if base2 is not None else base)}, {"calltrace": trace(commands=route)}, kind, spec)


def test_commands_compare_by_text_and_exit_code_and_extra_route_commands_are_listed():
    assert cm([cmd("echo hi > /tmp/airflowtmpab12/x.txt")], [cmd("echo hi > /var/folders/ab/T/tmpzz/x.txt")])["verdict"] == "match"       # temporary paths are masked
    d = cm([cmd("echo hi")], [cmd("echo bye")])
    assert d["verdict"] == "differ" and d["evidence"]["missing_in_route"] == ["echo hi (x1)"] and d["evidence"]["extra_in_route"] == ["echo bye (x1)"]
    assert cm([cmd("false", 1)], [cmd("false", 0)])["verdict"] == "differ"
    e = cm([cmd("echo hi")], [cmd("echo hi"), cmd("/venv/bin/python run.py")])
    assert e["verdict"] == "match" and e["evidence"]["extra_in_route"] == ["/venv/bin/python run.py (x1)"]
    assert cm([cmd("git status")], [])["verdict"] == "not-compared"                                                                         # the framework's own command is dropped
    assert cm([cmd("echo $RANDOM")], [cmd("echo hi")], base2=[cmd("echo other")])["verdict"] == "not-compared"                              # only commands both baseline runs ran
    assert cm([cmd("echo hi")], [], kind="rewrite")["verdict"] == "not-compared"                                                              # a rewrite may do it in Python
    assert TC.compare_commands({}, None, {"calltrace": trace()}, "converter")["verdict"] == "not-compared"


def test_functions_that_share_one_body_pair_as_a_pool_with_the_copies_the_converter_renamed():
    body = "fp_add10"
    base = [call("plus_10_TF", {"x": "1"}, "11", fp=body), call("plus_10_TF", {"x": "2"}, "12", fp=body), call("plus_10_traditional", {"x": "1"}, "11", fp=body)]
    route = [call("_af_plus_10_task", {"x": "1"}, "11", fp=body), call("_af_plus_10_task", {"x": "2"}, "12", fp=body), call("_af_plus_10_other", {"x": "1"}, "11", fp=body)]
    d = cc(base, route, spec={"dag_id": "d", "tasks": []})
    assert d["verdict"] == "match" and d["evidence"]["paired_by"]["_af_plus_10_task->pool(plus_10_TF, plus_10_traditional)"].startswith("body (pooled")
    bad = cc(base, route[:2] + [call("_af_plus_10_other", {"x": "9"}, "19", fp=body)], spec={"dag_id": "d", "tasks": []})
    assert bad["verdict"] == "differ" and "pooled" in bad["evidence"]["differences"][0]["difference"]


def test_representation_rules_are_applied_and_named_not_silent():
    base = [call("f", {"z": "(1, 10, 100)"}, "111")]
    d = cc(base, [call("_af_f", {"z": "[1, 10, 100]"}, "111", fp="x")], spec={"dag_id": "d", "tasks": [{"task_id": "f", "callable": {"name": "f"}}]})
    assert d["verdict"] == "match" and "tuple argument" in d["evidence"]["representation_rules_applied"][0]
    assert cc(base, [call("f", {"z": "[1, 10, 101]"}, "111")])["verdict"] == "differ"                                                          # different items stay a difference
    assert TC.tuples_as_lists("{'a': ('x, (y)', 1,), 'b': (2,)}") == "{'a': ['x, (y)', 1], 'b': [2]}"
    assert cc([call("f", {"d": "<DateTime 2026-11-27T10:00:00>"})], [call("f", {"d": "<datetime 2026-11-27T10:00:00>"})])["verdict"] == "match"              # pendulum is a datetime
    assert cc([call("f", {"test_mode": "None"})], [call("f", {"test_mode": "False"})])["verdict"] == "match"
    assert cc([call("f", {"test_mode": "None"})], [call("f", {"test_mode": "True"})])["verdict"] == "differ"
    ind = cc([call("f", {}, "'\\n        set -e\\n        ls\\n'")], [call("f", {}, "'\\n    set -e\\n    ls\\n'")])
    assert ind["verdict"] == "match" and "whitespace" in ind["evidence"]["representation_rules_applied"][0]
    assert cc([call("f", {}, "'\\n    set -e\\n    ls\\n'")], [call("f", {}, "'\\n    set -e\\n    rm\\n'")])["verdict"] == "differ"
    a = "<path file:///w/sandboxes/t/airflow1/cwd/include/bears>"
    assert cc([call("f", {"base": a})], [call("f", {"base": "<path include/bears>"})])["verdict"] == "match"                                        # a file:// URL against the relative path it names


def test_an_exit_code_that_differs_between_the_baseline_runs_is_not_compared_and_a_launcher_is_not_a_command():
    r = cm([cmd("exit $((RANDOM % 2))", 0)], [cmd("exit $((RANDOM % 2))", 1)], base2=[cmd("exit $((RANDOM % 2))", 1)])
    assert r["verdict"] == "match" and r["evidence"]["exit_code_nondeterministic"] == ["exit $((RANDOM % 2))"]
    same = cm([cmd("echo a && exit $((RANDOM % 2))", 0), cmd("echo a && exit $((RANDOM % 2))", 1)], [cmd("echo a && exit $((RANDOM % 2))", 0)])        # the two baseline runs agree by chance: the text draws a random number
    assert same["verdict"] == "match" and same["evidence"]["exit_code_nondeterministic"]
    spec = {"tasks": [{"task_id": "v", "operator": "PythonVirtualenvOperator"}]}
    d = cm([cmd("python script.py in out")], [cmd("uv venv x"), cmd("python run.py")], spec=spec)
    assert d["verdict"] == "not-compared" and "launcher" in d["reason"]


# ---- the verdict ------------------------------------------------------------------------------------------------------------------------------------------
SPEC1 = {"dag_id": "d", "schedule": {"kind": "manual"}, "tasks": [{"task_id": "t", "downstream": [], "upstream": [], "callable": {"name": "f"}, "reads_value_from": []}]}
TASKS = {"t": {"state": "success", "xcom": {}}}


def verdict(base_calls, route_calls):
    a = {"state": "success", "tasks": TASKS, "observed": {}, "stdout_lines": []}
    r = {"state": "success", "assets": {"a": {"state": "success", "value": None}}, "structure": {"assets": {"a": {"deps": []}}}, "observed": {}, "stdout_lines": []}
    a1 = {**a, "calltrace": trace(base_calls)} if base_calls is not None else a
    rr = {**r, "calltrace": trace(route_calls)} if route_calls is not None else r
    return B.compare(SPEC1, a1, {**a1}, rr, {"t": {"mode": "replaced", "asset": "a"}}, "converter")


def test_matching_calls_make_a_thin_dag_substantive_and_a_call_difference_makes_it_different():
    thin = verdict(None, None)["rollup"]
    assert thin["verdict"] == "equivalent" and thin["thin"] and "calls" in thin["unobserved"] and "commands" in thin["unobserved"]
    sub = verdict([call("f", {"n": "1"})], [call("_af_t", {"n": "1"}, fp="fp_f"), call("t_t", {"context": "<obj>"})])["rollup"]
    assert sub["verdict"] == "equivalent" and not sub["thin"] and "calls" in sub["matching"] and sub["unobserved"] == []
    bad = verdict([call("f", {"n": "1"})], [call("_af_t", {"n": "2"}, fp="fp_f")])["rollup"]
    assert bad["verdict"] == "different" and bad["differing"] == ["calls"] and bad["why"] == "behaviour differs in: calls"


# ---- end to end, real Airflow against the converter ---------------------------------------------------------------------------------------------------------
THIN_DAG = '''
    import pendulum
    from airflow import DAG
    from airflow.operators.python import PythonOperator

    def check(n, label="x"):
        assert n == 21, n

    def report(rows, scale=1):
        pass

    with DAG("d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False) as dag:
        a = PythonOperator(task_id="check", python_callable=check, op_kwargs={"n": 21, "label": "first"})
        b = PythonOperator(task_id="report", python_callable=report, op_args=[[1, 2, 3]], op_kwargs={"scale": 7})
        a >> b
'''


def converted(tmp_path, source):
    """real Airflow parses the DAG, the converter converts it; returns (spec, ir dag, converter src dir, report entry, dag file)."""
    import dag_spec
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / "d.py").write_text(textwrap.dedent(source))
    ir_path = tmp_path / "ir.json"
    r = subprocess.run([AF_PY, "-W", "ignore", str(SCRIPTS / "airflow_ir.py"), str(dags), "--out", str(ir_path), "--pythonpath", str(dags)], capture_output=True, text=True, timeout=300)
    assert ir_path.exists(), r.stderr[-1500:]
    ir = json.loads(ir_path.read_text())
    out = tmp_path / "conv"
    (out / "src" / "pkg" / "defs").mkdir(parents=True)
    (out / "src" / "pkg" / "__init__.py").write_text("")
    r = subprocess.run([sys.executable, str(SCRIPTS / "convert_ir.py"), str(ir_path), "--out-dir", str(out / "src" / "pkg" / "defs"), "--report", str(out / "report.json"), "--return-all"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-1500:]
    spec = json.loads(json.dumps(dag_spec.build(ir["dags"][0], None, None, None, ir["airflow_version"]), default=str))
    return spec, out / "src", json.loads((out / "report.json").read_text())["dags"]["d"], dags / "d.py"


def execute(tmp_path, tag, spec, src, entry, dag_file):
    import eval_behavior as E
    return E.execute(tmp_path / f"out_{tag}", tmp_path / "sandboxes" / tag, spec, "d", dag_file, dag_file.parent, AF_PY, sys.executable, {"src": src, "report": entry}, tmp_path / "none", ["converter"], 180)


@needs_airflow
@needs_monitoring
def test_a_thin_dag_becomes_substantive_and_a_changed_argument_in_the_converted_code_is_caught(tmp_path):
    spec, src, entry, dag_file = converted(tmp_path, THIN_DAG)
    res = execute(tmp_path, "same", spec, src, entry, dag_file)
    x = res["routes"]["converter"]
    assert res["baseline"]["class"][0] == "ok"
    d = x["dimensions"]
    assert d["calls"]["verdict"] == "match", d["calls"]
    assert d["calls"]["functions"] == 2 and d["calls"]["nontrivial_calls"] >= 1
    assert d["values"]["verdict"] == "not-compared" and d["stdout"]["verdict"] == "not-compared" and d["files"]["verdict"] == "not-compared"          # nothing else was observable
    assert x["rollup"]["verdict"] == "equivalent" and x["rollup"]["thin"] is False and "calls" in x["rollup"]["matching"]
    stats = res["trace_stats"]
    assert stats["airflow1"]["calls"] >= 2 and stats["converter"]["calls"] >= 2 and stats["converter"]["bytes"] > 0
    off = tmp_path / "off"
    off.mkdir()
    os.environ["EVAL_TRACE"] = "0"
    try:
        thin = execute(off, "off", spec, src, entry, dag_file)["routes"]["converter"]["rollup"]
    finally:
        del os.environ["EVAL_TRACE"]
    assert thin["verdict"] == "equivalent" and thin["thin"] is True               # the same DAG without the layer: only outcome and structure

    # the same conversion with one literal argument changed in the generated module
    gen = src / "pkg" / "defs" / "d.py"
    text = gen.read_text()
    assert "'scale': 7" in text or "scale=7" in text, text[-1500:]
    gen.write_text(text.replace("'scale': 7", "'scale': 8").replace("scale=7", "scale=8"))
    bad = execute(tmp_path, "bad", spec, src, entry, dag_file)["routes"]["converter"]
    assert bad["dimensions"]["calls"]["verdict"] == "differ", bad["dimensions"]["calls"]
    row = bad["dimensions"]["calls"]["evidence"]["differences"][0]
    assert row["function"] == "report" and "scale=7" in row["missing_in_route"][0] and "scale=8" in row["extra_in_route"][0]
    assert bad["rollup"]["verdict"] == "different" and bad["rollup"]["differing"] == ["calls"]


BASH_DAG = '''
    import pendulum
    from airflow import DAG
    from airflow.operators.bash import BashOperator

    with DAG("d", schedule=None, start_date=pendulum.datetime(2024, 1, 1), catchup=False) as dag:
        a = BashOperator(task_id="greet", bash_command="echo hello-from-bash")
        b = BashOperator(task_id="count", bash_command="exit 0")
        a >> b
'''


@needs_airflow
@needs_monitoring
def test_a_bash_dag_is_compared_by_what_it_ran_and_a_changed_command_is_caught(tmp_path):
    spec, src, entry, dag_file = converted(tmp_path, BASH_DAG)
    res = execute(tmp_path, "same", spec, src, entry, dag_file)
    x = res["routes"]["converter"]
    assert x["dimensions"]["commands"]["verdict"] == "match", x["dimensions"]["commands"]
    assert x["dimensions"]["commands"]["commands"] == 2 and x["rollup"]["thin"] is False
    gen = src / "pkg" / "defs" / "d.py"
    text = gen.read_text()
    assert "echo hello-from-bash" in text
    gen.write_text(text.replace("echo hello-from-bash", "echo hello-from-elsewhere"))
    bad = execute(tmp_path, "bad", spec, src, entry, dag_file)["routes"]["converter"]
    c = bad["dimensions"]["commands"]
    assert c["verdict"] == "differ" and c["evidence"]["missing_in_route"] == ["echo hello-from-bash (x1)"] and c["evidence"]["extra_in_route"] == ["echo hello-from-elsewhere (x1)"]


# ---- the route's working directory ----------------------------------------------------------------------------------------------------------------------------
def test_the_dagster_run_has_the_seeded_project_files_in_its_working_directory_and_traces_its_calls(tmp_path):
    """found by the trace: `run_dagster.py` never passed the seed to the sandbox, so a route reading `include/...` failed where Airflow (which had it) did not."""
    src = tmp_path / "src"
    (src / "pkg" / "defs").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("")
    (src / "pkg" / "defs" / "__init__.py").write_text("")
    (src / "pkg" / "defs" / "d.py").write_text("import dagster as dg\n\n\ndef read_it(name):\n    return open(name).read()\n\n\n@dg.asset\ndef a():\n    return read_it('include/x.txt')\n")
    data = tmp_path / "x.txt"
    data.write_text("hello")
    cfg = {"kind": "converter", "dag_id": "d", "repo_root": str(tmp_path), "out": str(tmp_path / "out.json"), "timeout_s": 60, "converter": {"src": str(src), "pkg": "pkg"},
           "sandbox": {"root": str(tmp_path / "sb"), "connections": {}, "variables": {}, "sleep_cap": None, "seed": {"include/x.txt": str(data)}, "trace": {"roots": [str(src / "pkg" / "defs" / "d.py")], "exclude": [], "cap": 100}}}
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    subprocess.run([sys.executable, str(SCRIPTS / "parity" / "run_dagster.py"), str(tmp_path / "cfg.json")], check=True, capture_output=True, cwd=tmp_path)
    res = json.loads((tmp_path / "out.json").read_text())
    assert res["state"] == "success" and res["assets"]["a"]["value"] == '"hello"'
    tr = json.loads((tmp_path / "out.trace.json").read_text())
    assert [(c["fn"], c["args"], c["ret"]) for c in tr["calls"] if c["fn"] == "read_it"] == [("read_it", {"name": "'include/x.txt'"}, "'hello'")]


def test_a_long_string_re_indented_by_the_converter_is_compared_by_its_common_prefix_and_a_jinja_template_function_is_framework():
    a, b = CT.plain("\n" + "".join(" " * 12 + f"line {i} of the script with some words\n" for i in range(30))), CT.plain("\n" + "".join(" " * 8 + f"line {i} of the script with some words\n" for i in range(30)))
    assert "...(len" in a and a != b
    assert cc([call("f", {}, a)], [call("f", {}, b)])["verdict"] == "match"
    c = CT.plain("\n" + "".join(" " * 8 + f"line {i} of the script with other words\n" for i in range(30)))
    assert cc([call("f", {}, a)], [call("f", {}, c)])["verdict"] == "differ"
    jinja = call("root", {"context": "<obj>", "environment": "<obj>", "missing": "<obj>"}, "<generator>")
    assert cc([call("f", {}, "1"), jinja], [call("f", {}, "1")])["verdict"] == "match"
