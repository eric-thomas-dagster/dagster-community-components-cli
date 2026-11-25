"""Differential harness: a JavaScript Evaluator script run in QuickJS and, transpiled, in Python must give the same output, errors and state."""
import types

import streamsets_to_dagster.runtime as rt
from streamsets_to_dagster.jstopy import transpile
from streamsets_to_dagster.runtime import Record, Run


def module_for(script, init=""):
    src = transpile(script, init).replace("from ..jsrt import", "from streamsets_to_dagster.jsrt import")
    mod = types.ModuleType("scr")
    exec(compile(src, "<scr>", "exec"), mod.__dict__)
    return mod


def run_both(script, batches, init=""):
    """Returns (quickjs_result, python_result); each is (records [(value, attrs)], error count, events, state)."""
    mod = module_for(script, init)
    results = []
    for kind in ("js", "py"):
        run = Run(None, "t", {})
        got, evs = [], []
        if kind == "js":
            rt.js_processor(run, "S_01", script, init, inputs=["in"], outputs=["out"], events=["ev"])
        else:
            rt.script_native(run, "S_01", mod, inputs=["in"], outputs=["out"], events=["ev"])
        run.add(rt.Stage("sink", lambda recs: got.extend((r.value, r.attrs) for r in recs) or {}, inputs=["out"]))
        run.add(rt.Stage("evs", lambda recs: evs.extend((r.value, r.event) for r in recs) or {}, inputs=["ev"]))
        run.add(rt.Stage("src", gen=lambda: iter([{"in": [Record(dict(v), dict(a)) for v, a in b]} for b in batches]), outputs=["in"]))
        try:
            stats = run.execute()
        except Exception:                          # noqa: BLE001 - a script that throws out of the batch: both engines must do it
            results.append("RAISED")
            continue
        results.append((got, sum(stats["error_records"].values()), evs))
    return results
