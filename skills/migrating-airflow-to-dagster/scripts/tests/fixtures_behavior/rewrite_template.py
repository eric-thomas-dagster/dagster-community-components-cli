"""Rewrite of `fixture_pipeline` (a fixture for the harness's own tests).

t_extract: replaced by asset `fixture_pipeline/extracted`.
t_write: replaced by asset `fixture_pipeline/report`.
t_load: replaced by asset `fixture_pipeline/loaded`.
t_stamp: replaced by asset `fixture_pipeline/stamp`.
t_notify: replaced by asset `fixture_pipeline/notified`.
"""
import datetime
import json
import os
import smtplib
import sqlite3
import subprocess
from email.message import EmailMessage

import dagster as dg
import requests

RETRY = dg.RetryPolicy(max_retries=@RETRIES@, delay=30)


def _db() -> str:
    return os.environ["AIRFLOW_CONN_SQLITE_DEFAULT"].split("sqlite:///", 1)[1]


@dg.asset(key=["fixture_pipeline", "extracted"], retry_policy=RETRY)
def extracted():
    return {"rows": @ROWS@}


@dg.asset(key=["fixture_pipeline", "report"], retry_policy=RETRY)
def report():
    subprocess.run("@COMMAND@", shell=True, check=True)


@dg.asset(key=["fixture_pipeline", "loaded"], deps=[extracted], retry_policy=RETRY)
def loaded():
    con = sqlite3.connect(_db())
    con.execute("create table if not exists items (id integer, name text)")
    con.executemany("insert into items values (?, ?)", @INSERTS@)
    con.commit()
    con.close()


@dg.asset(key=["fixture_pipeline", "stamp"], retry_policy=RETRY)
def stamp():
    with open("stamp.txt", "w") as f:
        f.write(datetime.datetime.now().isoformat() + "Z")


@dg.asset(key=["fixture_pipeline", "notified"], deps=[loaded, report, stamp], retry_policy=RETRY)
def notified():
    if @HTTP@:
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


ASSETS = [extracted, report, loaded, stamp, notified]
fixture_pipeline = dg.define_asset_job("fixture_pipeline", selection=ASSETS)
fixture_pipeline_schedule = dg.ScheduleDefinition(job=fixture_pipeline, cron_schedule="@CRON@", execution_timezone="UTC")
