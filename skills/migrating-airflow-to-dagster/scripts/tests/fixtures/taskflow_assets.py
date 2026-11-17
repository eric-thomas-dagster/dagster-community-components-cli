from datetime import datetime

from airflow.sdk import Asset, dag, task

raw = Asset("s3://bucket/raw")
clean = Asset("s3://bucket/clean")


@dag(schedule=[raw], start_date=datetime(2024, 1, 1), catchup=True, max_active_runs=1)
def clean_pipeline():
    @task(outlets=[clean], retries=3)
    def clean_rows():
        return [1, 2, 3]

    @task
    def summarize(rows):
        return len(rows)

    rows = clean_rows()
    summarize(rows)


clean_pipeline()
