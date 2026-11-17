from templates import build_load_dag, Config

build_load_dag(Config(dag_id="one_off", schedule="@daily", table="T1"))

LOADS = {
    "customers": {"schedule": "@daily", "table": "CUSTOMERS"},
    "orders": {"schedule": "0 * * * *", "table": "ORDERS"},
}
for name, cfg in sorted(LOADS.items()):
    dag = build_load_dag(Config(dag_id=f"gen_{name}", warehouse="WH", **cfg))
    globals()[dag.dag_id] = dag
