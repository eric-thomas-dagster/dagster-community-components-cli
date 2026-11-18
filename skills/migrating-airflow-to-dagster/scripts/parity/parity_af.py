"""Run converted DAGs with AIRFLOW's own dag.test() in a sandbox and record each task's state and returned value."""
import json, os, signal, sys, tempfile, warnings
warnings.filterwarnings("ignore")
repo, dags_folder, ids_file, out = sys.argv[1:5]
ids_file, out = os.path.abspath(ids_file), os.path.abspath(out)
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp(); os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
sys.path.insert(0, repo)
import airflow
from airflow.utils.db import initdb; initdb()          # dag.test() writes task instances: both Airflow 2 and 3 need the throwaway DB
if airflow.__version__.startswith("2."):
    from airflow.models.dagbag import DagBag
else:
    try:
        from airflow.dag_processing.dagbag import DagBag           # Airflow 3.2 and later
    except ImportError:
        from airflow.models.dagbag import DagBag                   # Airflow 3.0 and 3.1
ids = json.load(open(ids_file))
_bags = []
class _Merged:                                   # several DAG folders (e.g. apache/airflow's two example folders)
    dags = {}
for folder in dags_folder.split(":"):
    try: b = DagBag(dag_folder=folder, include_examples=False, safe_mode=False)
    except TypeError: b = DagBag(dag_folder=folder, safe_mode=False)
    _Merged.dags.update(b.dags); _bags.append(b)
bag = _Merged
if not airflow.__version__.startswith("2."):
    try:
        from airflow.dag_processing.dagbag import sync_bag_to_db      # Airflow 3 serialises DAGs before dag.test() can create a run
    except ImportError:
        try:
            from airflow.models.dagbag import sync_bag_to_db           # Airflow 3.1
        except ImportError:                                           # Airflow 3.0 keeps it on the DagBag
            def sync_bag_to_db(bag, bundle, version):
                bag.sync_to_db(bundle_name=bundle, bundle_version=version)
    from airflow.dag_processing.bundles.manager import DagBundlesManager
    DagBundlesManager().sync_bundles_to_db()                       # the bundle row must exist (foreign key on bundle_name)
    for _b in _bags:
        for _k in [k for k in _b.dags if k not in ids]:             # serialise only the DAGs under test (others may need plugins)
            del _b.dags[_k]
        sync_bag_to_db(_b, "dags-folder", None)
res = {}
class TO(Exception): pass
def alarm(*_): raise TO()
signal.signal(signal.SIGALRM, alarm)
work = tempfile.mkdtemp(); os.chdir(work)
for did in ids:
    dag = bag.dags.get(did)
    if dag is None: res[did] = {"error": "not in DagBag"}; continue
    signal.alarm(300)
    try:
        dr = dag.test()
        if dr is None:                                                # Airflow 2.5: test() returns nothing; read the run it created
            from airflow.models import DagRun
            dr = sorted(DagRun.find(dag_id=did), key=lambda r: r.start_date or r.execution_date)[-1]
        tasks = {}
        for ti in dr.get_task_instances():
            val = None
            try:
                if airflow.__version__.startswith("2."):
                    from airflow.models.xcom import XCom
                    val = XCom.get_one(run_id=ti.run_id, task_id=ti.task_id, dag_id=ti.dag_id, key="return_value")
                else:
                    from airflow.models.xcom import XComModel
                    from airflow.utils.session import create_session
                    with create_session() as session:
                        rows = session.scalars(XComModel.get_many(run_id=ti.run_id, key="return_value", task_ids=ti.task_id, dag_ids=ti.dag_id)).all()
                        val = XComModel.deserialize_value(rows[0]) if rows else None
            except Exception as e: val = f"xcom-error:{type(e).__name__}"
            tasks[ti.task_id] = {"state": str(ti.state), "value": repr(val)[:300]}
        res[did] = {"state": str(dr.state), "tasks": tasks}
    except TO: res[did] = {"error": "timeout 300s"}
    except BaseException as e: res[did] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally: signal.alarm(0)
json.dump(res, open(out, "w"), indent=1)
print("airflow ran", len(res), "DAGs;", sum(1 for v in res.values() if v.get("state") == "success"), "succeeded;", sum(1 for v in res.values() if "error" in v), "errors")
