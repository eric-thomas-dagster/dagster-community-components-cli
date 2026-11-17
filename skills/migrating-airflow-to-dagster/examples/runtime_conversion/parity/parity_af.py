"""Run converted DAGs with AIRFLOW's own dag.test() in a sandbox and record each task's state and returned value."""
import json, os, signal, sys, tempfile, warnings
warnings.filterwarnings("ignore")
repo, dags_folder, ids_file, out = sys.argv[1:5]
ids_file, out = os.path.abspath(ids_file), os.path.abspath(out)
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp(); os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
sys.path.insert(0, repo)
import airflow
if airflow.__version__.startswith("2."):
    from airflow.utils.db import initdb; initdb()
    from airflow.models.dagbag import DagBag
else:
    from airflow.dag_processing.dagbag import DagBag
ids = json.load(open(ids_file))
class _Merged:                                   # several DAG folders (e.g. apache/airflow's two example folders)
    dags = {}
for folder in dags_folder.split(":"):
    try: b = DagBag(dag_folder=folder, include_examples=False, safe_mode=False)
    except TypeError: b = DagBag(dag_folder=folder, safe_mode=False)
    _Merged.dags.update(b.dags)
bag = _Merged
res = {}
class TO(Exception): pass
def alarm(*_): raise TO()
signal.signal(signal.SIGALRM, alarm)
work = tempfile.mkdtemp(); os.chdir(work)
for did in ids:
    dag = bag.dags.get(did)
    if dag is None: res[did] = {"error": "not in DagBag"}; continue
    signal.alarm(60)
    try:
        dr = dag.test()
        tasks = {}
        for ti in dr.get_task_instances():
            val = None
            try:
                if airflow.__version__.startswith("2."):
                    from airflow.models.xcom import XCom
                    val = XCom.get_one(run_id=ti.run_id, task_id=ti.task_id, dag_id=ti.dag_id, key="return_value")
                else:
                    val = "xcom-unavailable-3x"
            except Exception as e: val = f"xcom-error:{type(e).__name__}"
            tasks[ti.task_id] = {"state": str(ti.state), "value": repr(val)[:300]}
        res[did] = {"state": str(dr.state), "tasks": tasks}
    except TO: res[did] = {"error": "timeout 60s"}
    except BaseException as e: res[did] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally: signal.alarm(0)
json.dump(res, open(out, "w"), indent=1)
print("airflow ran", len(res), "DAGs;", sum(1 for v in res.values() if v.get("state") == "success"), "succeeded;", sum(1 for v in res.values() if "error" in v), "errors")
