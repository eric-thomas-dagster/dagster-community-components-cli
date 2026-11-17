"""Converted from Airflow DAG `simple_sequential_pipeline` (airflow_src/simple_sequential.py).

Airflow: @dag, schedule "0 9 * * *", catchup=False, default_args retries=3, retry_delay=5 min.
Task bodies are unchanged; XCom edges become asset dependencies loaded through the IO manager.
"""
import dagster as dg

GROUP = "simple_sequential_pipeline"
# default_args: retries=3, retry_delay=timedelta(minutes=5)
RETRY = dg.RetryPolicy(max_retries=3, delay=300)


@dg.asset(key=[GROUP, "fetch_data"], group_name=GROUP, retry_policy=RETRY)
def fetch_data() -> dict:
    """Fetch data from a source."""
    source = "api"  # Airflow task parameter default; Dagster assets take no free parameters
    print(f"Fetching data from {source}")
    return {"source": source, "data": [1, 2, 3, 4, 5]}


@dg.asset(key=[GROUP, "process_data"], group_name=GROUP, retry_policy=RETRY,
          ins={"data": dg.AssetIn(key=[GROUP, "fetch_data"])})
def process_data(data: dict) -> dict:
    """Process the fetched data."""
    print(f"Processing data from {data['source']}")
    processed = {
        "source": data["source"],
        "processed": [x * 2 for x in data["data"]],
    }
    return processed


@dg.asset(key=[GROUP, "save_results"], group_name=GROUP, retry_policy=RETRY,
          ins={"processed": dg.AssetIn(key=[GROUP, "process_data"])})
def save_results(processed: dict) -> str:
    """Save the processed results."""
    result = f"Saved {len(processed['processed'])} items from {processed['source']}"
    print(result)
    return result


simple_sequential_pipeline_job = dg.define_asset_job(GROUP, selection=dg.AssetSelection.groups(GROUP))

simple_sequential_pipeline_schedule = dg.ScheduleDefinition(
    name=f"{GROUP}_schedule",
    job=simple_sequential_pipeline_job,
    cron_schedule="0 9 * * *",
    execution_timezone="UTC",  # Airflow evaluates in UTC unless the DAG start_date carries a timezone
)
