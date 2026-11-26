"""A small DAG with every side effect the behaviour harness stands in for: a returned value, a shell command that writes a file, SQLite rows, an HTTP POST, a mail, an S3 object,
a file with a timestamp (nondeterministic) and a retry policy. Used by the harness's own tests (Airflow 2 imports)."""
import datetime
import json
import os
import smtplib
import sqlite3
from email.message import EmailMessage

import requests
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator


def _db() -> str:
    return os.environ["AIRFLOW_CONN_SQLITE_DEFAULT"].split("sqlite:///", 1)[1]


def extract():
    return {"rows": [[1, "a"], [2, "b"]]}


def load():
    con = sqlite3.connect(_db())
    con.execute("create table if not exists items (id integer, name text)")
    con.executemany("insert into items values (?, ?)", [(1, "a"), (2, "b")])
    con.commit()
    con.close()


def stamp():
    with open("stamp.txt", "w") as f:
        f.write(datetime.datetime.now().isoformat())


def notify():
    requests.post("http://hooks.example/notify", json={"rows": 2}, timeout=5)
    m = EmailMessage()
    m["Subject"] = "pipeline done"
    m["From"] = "etl@example.com"
    m["To"] = "ops@example.com"
    m.set_content("2 rows loaded")
    with smtplib.SMTP("localhost") as s:
        s.send_message(m)
    import boto3
    c = boto3.client("s3", region_name="us-east-1")
    c.create_bucket(Bucket="fixture-bucket")
    c.put_object(Bucket="fixture-bucket", Key="done.json", Body=json.dumps({"rows": 2}).encode())


with DAG("fixture_pipeline", schedule="0 6 * * *", start_date=datetime.datetime(2024, 1, 1), catchup=False,
         default_args={"retries": 2, "retry_delay": datetime.timedelta(seconds=30)}) as dag:
    t_extract = PythonOperator(task_id="t_extract", python_callable=extract)
    t_write = BashOperator(task_id="t_write", bash_command="echo hello > report.txt")
    t_load = PythonOperator(task_id="t_load", python_callable=load)
    t_stamp = PythonOperator(task_id="t_stamp", python_callable=stamp)
    t_notify = PythonOperator(task_id="t_notify", python_callable=notify)
    t_extract >> t_load >> t_notify
    t_write >> t_notify
    t_stamp >> t_notify
