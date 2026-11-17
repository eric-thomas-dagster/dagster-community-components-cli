from datetime import datetime

from airflow import DAG
from airflow.operators.python import BranchPythonOperator, PythonOperator
from airflow.operators.empty import EmptyOperator
from airflow.providers.amazon.aws.sensors.s3 import S3KeySensor
from airflow.operators.trigger_dagrun import TriggerDagRunOperator

dag = DAG("branchy", schedule_interval="0 6 * * 1-5", start_date=datetime(2024, 1, 1))

wait = S3KeySensor(task_id="wait_for_file", bucket_key="s3://b/k", aws_conn_id="aws_default", dag=dag)
choose = BranchPythonOperator(task_id="choose", python_callable=lambda: "a", dag=dag)
a = EmptyOperator(task_id="a", dag=dag)
b = EmptyOperator(task_id="b", dag=dag)
per_item = PythonOperator.partial(task_id="per_item", python_callable=print, dag=dag).expand(op_args=[[1], [2]])
fire = TriggerDagRunOperator(task_id="fire", trigger_dag_id="other", dag=dag)

wait >> choose >> [a, b]
a >> per_item
b.set_downstream(fire)
