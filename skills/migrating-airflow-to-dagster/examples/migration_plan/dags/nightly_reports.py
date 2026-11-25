"""Plain Python: pull a few numbers, write a report."""
from datetime import datetime
from airflow.decorators import dag, task


@dag(schedule="0 7 * * *", start_date=datetime(2024, 1, 1), catchup=False)
def nightly_reports():
    @task
    def pull():
        return {"rows": 10}

    @task
    def write(stats):
        print(stats)

    write(pull())


nightly_reports()
