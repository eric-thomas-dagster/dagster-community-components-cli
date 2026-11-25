"""Load daily files from S3 into Snowflake: ELT/transfer."""
from datetime import datetime
from airflow import DAG
from airflow.providers.amazon.aws.operators.s3 import S3ListOperator
from airflow.providers.snowflake.transfers.copy_into_snowflake import CopyFromExternalStageToSnowflakeOperator

with DAG("s3_to_warehouse", schedule="@daily", start_date=datetime(2024, 1, 1), catchup=False) as dag:
    files = S3ListOperator(task_id="list_files", bucket="raw", prefix="orders/")
    load = CopyFromExternalStageToSnowflakeOperator(task_id="copy_orders", table="ORDERS_RAW", stage="RAW_STAGE", file_format="(type = 'CSV')", snowflake_conn_id="snowflake_default")
    files >> load
