"""A DAG with the provider operators the converter maps, written the way users write them. Used to capture a real IR (see ir.json)."""
from datetime import datetime

from airflow import DAG
from airflow.providers.airbyte.operators.airbyte import AirbyteTriggerSyncOperator
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.providers.databricks.operators.databricks import DatabricksRunNowOperator, DatabricksSubmitRunOperator
from airflow.providers.dbt.cloud.operators.dbt import DbtCloudRunJobOperator
from airflow.providers.http.operators.http import HttpOperator
from airflow.providers.http.sensors.http import HttpSensor
from airflow.providers.slack.operators.slack_webhook import SlackWebhookOperator
from airflow.providers.smtp.operators.smtp import EmailOperator
from airflow.providers.ssh.operators.ssh import SSHOperator
from airflow.providers.docker.operators.docker import DockerOperator
from airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator

with DAG("providers", start_date=datetime(2024, 1, 1), schedule="0 6 * * *", catchup=False) as dag:
    wait = HttpSensor(task_id="wait_for_api", http_conn_id="partner_api", endpoint="health", poke_interval=30, timeout=600)
    fetch = HttpOperator(task_id="fetch", http_conn_id="partner_api", endpoint="v1/items/{{ ds }}", method="GET",
                         data={"limit": "10"}, headers={"Accept": "application/json"}, extra_options={"timeout": 20}, log_response=True)
    load = SQLExecuteQueryOperator(task_id="load", conn_id="warehouse_pg", sql=["delete from t where d = %(d)s", "insert into t select 1"],
                                   parameters={"d": "2024-01-01"}, autocommit=True)
    crunch = KubernetesPodOperator(task_id="crunch", name="crunch", namespace="batch", image="registry.example.com/crunch:1.2",
                                   cmds=["python", "-m", "crunch"], arguments=["--day", "{{ ds }}"], env_vars={"MODE": "full"},
                                   service_account_name="crunch-sa", get_logs=True, in_cluster=True)
    mail = EmailOperator(task_id="mail", to=["a@example.com"], subject="done {{ ds }}", html_content="<b>ok</b>", conn_id="smtp_default")
    slack = SlackWebhookOperator(task_id="slack", slack_webhook_conn_id="slack_alerts", message="load finished", channel="#data")
    dbt = DbtCloudRunJobOperator(task_id="dbt", dbt_cloud_conn_id="dbt_cloud_default", job_id=1234, check_interval=15, timeout=3600,
                                 steps_override=["dbt build"], trigger_reason="airflow")
    ab = AirbyteTriggerSyncOperator(task_id="airbyte", airbyte_conn_id="airbyte_default", connection_id="0a1b2c3d-0000-0000-0000-000000000000", timeout=1800, wait_seconds=10)
    dbr = DatabricksRunNowOperator(task_id="dbr_now", databricks_conn_id="databricks_default", job_id=77, notebook_params={"day": "{{ ds }}"})
    dbs = DatabricksSubmitRunOperator(task_id="dbr_submit", databricks_conn_id="databricks_default", run_name="adhoc",
                                      new_cluster={"spark_version": "14.3.x-scala2.12", "node_type_id": "i3.xlarge", "num_workers": 1},
                                      notebook_task={"notebook_path": "/Shared/adhoc"})
    bq = BigQueryInsertJobOperator(task_id="bq", gcp_conn_id="google_cloud_default", location="US",
                                   configuration={"query": {"query": "select 1", "useLegacySql": False}})
    ssh = SSHOperator(task_id="ssh", ssh_conn_id="edge_host", command="echo hi")
    dock = DockerOperator(task_id="dock", image="alpine:3", command="echo hi")
    wait >> fetch >> load >> crunch >> mail >> slack
    load >> [dbt, ab] >> dbr >> dbs
    load >> bq
    load >> ssh >> dock
