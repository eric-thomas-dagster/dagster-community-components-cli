"""Wait for a partner file, then load it: sensor-driven."""
from datetime import datetime
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.sensors.filesystem import FileSensor

with DAG("partner_drop", schedule="@hourly", start_date=datetime(2024, 1, 1), catchup=False) as dag:
    wait = FileSensor(task_id="wait_for_file", filepath="/data/partner/drop.csv")
    wait2 = FileSensor(task_id="wait_for_manifest", filepath="/data/partner/manifest.json")
    load = PythonOperator(task_id="load", python_callable=lambda: None)
    [wait, wait2] >> load
