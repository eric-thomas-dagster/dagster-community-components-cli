"""A container job on Kubernetes."""
from datetime import datetime
from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator

with DAG("feature_build", schedule="0 3 * * *", start_date=datetime(2024, 1, 1), catchup=False) as dag:
    KubernetesPodOperator(task_id="build", name="build", image="registry.example/features:1", cmds=["python", "build.py"])
