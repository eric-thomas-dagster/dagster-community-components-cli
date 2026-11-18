"""Materialize the converted DAGs in Dagster (in-process) and record each task's outcome and returned value."""
import importlib, json, os, signal, sys, tempfile, warnings
warnings.filterwarnings("ignore")
srcdir, ids_file, report_file, out = sys.argv[1:5]
ids_file, report_file, out = os.path.abspath(ids_file), os.path.abspath(report_file), os.path.abspath(out)
sys.path.insert(0, srcdir)
import dagster as dg
ids = json.load(open(ids_file)); report = json.load(open(report_file))["dags"]
import re
safe = lambda n: re.sub(r"\W", "_", n) if not re.sub(r"\W", "_", n)[:1].isdigit() else "_" + re.sub(r"\W", "_", n)
res = {}
class TO(Exception): pass
signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TO()))
os.chdir(tempfile.mkdtemp())
for did in ids:
    rep = report[did]
    try:
        mod = importlib.import_module(f"pkg.defs.{safe(did)}")
        assets = [v for v in vars(mod).values() if isinstance(v, dg.AssetsDefinition)]
        signal.alarm(300)
        r = dg.materialize(assets, raise_on_error=False)
        tasks = {}
        keys_ok = {e.asset_key.to_user_string() for e in r.get_asset_materialization_events()}
        for tid, key in rep["task_keys"].items():
            ak = dg.AssetKey(key.split("/"))
            val = None
            try: val = r.asset_value(ak)
            except Exception: pass
            tasks[tid] = {"state": "success" if key in keys_ok else "not_materialized", "value": repr(val)[:300] if val is not None else "None"}
        res[did] = {"state": "success" if r.success else "failed", "tasks": tasks}
    except TO: res[did] = {"error": "timeout 300s"}
    except BaseException as e: res[did] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally: signal.alarm(0)
json.dump(res, open(out, "w"), indent=1)
print("dagster ran", len(res), "DAGs;", sum(1 for v in res.values() if v.get("state") == "success"), "succeeded;", sum(1 for v in res.values() if "error" in v), "errors")
