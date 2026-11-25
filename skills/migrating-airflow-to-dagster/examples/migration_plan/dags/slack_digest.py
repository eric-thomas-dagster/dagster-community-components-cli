"""Notification only."""
from datetime import datetime
from airflow import DAG
from airflow.operators.empty import EmptyOperator
from airflow.operators.email import EmailOperator

with DAG("slack_digest", schedule="0 9 * * 1", start_date=datetime(2024, 1, 1), catchup=False) as dag:
    start = EmptyOperator(task_id="start")
    mail = EmailOperator(task_id="mail", to="team@example.com", subject="weekly digest", html_content="digest")
    start >> mail
