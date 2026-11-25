"""Pull a partner's export once their status endpoint says it is ready, reshape it, and publish a summary line.

A realistic mix for the converter: the sensor takes a `response_check` function (the converter refuses it), and the fetch step reads the Airflow connection through `BaseHook`
(the converter stubs a callable that uses the Airflow API). Core Airflow only, so the DAG parses with a plain Airflow install."""
from datetime import datetime, timedelta

import requests
from airflow.decorators import dag, task
from airflow.hooks.base import BaseHook
from airflow.operators.bash import BashOperator
from airflow.providers.http.sensors.http import HttpSensor


def _export_ready(response) -> bool:
    return response.json().get("status") == "ready"


@dag(dag_id="partner_ingest", start_date=datetime(2024, 1, 1), schedule="0 */6 * * *", catchup=False, tags=["partner", "ingest"],
     default_args={"owner": "data-eng", "retries": 2, "retry_delay": timedelta(minutes=10)})
def partner_ingest():
    wait = HttpSensor(task_id="wait_for_export", http_conn_id="partner_api", endpoint="export/status", response_check=_export_ready, poke_interval=120, timeout=6 * 3600)

    @task
    def fetch_export() -> list[dict]:
        conn = BaseHook.get_connection("partner_api")
        resp = requests.get(f"{conn.host}/export/latest", headers={"Authorization": f"Bearer {conn.password}"}, timeout=60)
        resp.raise_for_status()
        return resp.json()["rows"]

    @task
    def reshape(rows: list[dict]) -> dict:
        """One record per customer, amounts summed, rows without an id dropped."""
        out: dict[str, float] = {}
        for r in rows:
            if r.get("customer_id"):
                out[r["customer_id"]] = out.get(r["customer_id"], 0.0) + float(r.get("amount", 0))
        return {"customers": len(out), "total": round(sum(out.values()), 2), "by_customer": out}

    @task
    def publish(summary: dict) -> str:
        line = f"partner export: {summary['customers']} customers, total {summary['total']}"
        print(line)
        return line

    cleanup = BashOperator(task_id="remove_tmp", bash_command="rm -rf /tmp/partner_export_tmp")
    fetched = fetch_export()
    summary = publish(reshape(fetched))
    wait >> fetched
    summary >> cleanup


partner_ingest()
