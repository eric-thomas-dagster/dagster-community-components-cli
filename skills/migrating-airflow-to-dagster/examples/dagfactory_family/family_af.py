import json, os, sys, tempfile, warnings
warnings.filterwarnings("ignore")
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp(); os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
root = os.path.abspath("dagfactory_fx/dags"); sys.path.insert(0, root)
from airflow.dag_processing.dagbag import DagBag
bag = DagBag(dag_folder=root, safe_mode=False)
rows = {}
for dag_id, dag in bag.dags.items():
    if not dag_id.startswith("module3_dynamic_dag_"): continue
    t = dag.task_dict["task_process_s3_data"]
    rows[dag_id] = {"op_args": list(t.op_args), "callable": t.python_callable.__name__, "cron": getattr(dag.timetable, "_expression", None) or getattr(dag.timetable, "expression", None), "retries": t.retries}
json.dump(rows, open("family_af.json", "w"), indent=1); print(len(rows), "family DAGs from dag-factory:", sorted(a["op_args"][0] for a in rows.values()))
