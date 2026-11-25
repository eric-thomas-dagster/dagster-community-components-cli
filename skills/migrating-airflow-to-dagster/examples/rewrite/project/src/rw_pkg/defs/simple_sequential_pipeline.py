"""Rewrite of Airflow DAG `simple_sequential_pipeline` (fetch, process, save).

Airflow tasks:
- fetch_data: folded into asset `simple_sequential_pipeline/processed_summary` (a plain function call; its dict feeds only the next step and nothing persists it)
- process_data: folded into asset `simple_sequential_pipeline/processed_summary` (same reason)
- save_results: replaced by asset `simple_sequential_pipeline/processed_summary` (its returned line is the one value the DAG produces)

Shape: one asset for the one data product. The three steps are in-memory, pure and take no longer than a blink, so none deserves its own restart point: a failed step restarts from the
start of the asset (Airflow retried a single task). The retry policy is the DAG's `default_args` (3 retries, 5 minutes apart) on the asset.

Registry: searched `dagster-component search "multi-step transform pipeline" --json`: `polars_pipeline`, `pyspark_pipeline`, `snowpark_pipeline` and `warehouse_pipeline` (all tier `live`) take
dataframes or warehouse tables; the steps here are list arithmetic on a dict, so no component fits and the steps stay Python.

Schedule: `0 9 * * *` in UTC, no catchup (Airflow's `catchup=False`; a Dagster schedule does not replay missed ticks).
Dropped: nothing. Not preserved: the per-task run record and log (there is one run and one asset materialization), XCom (plain function calls instead).
"""
import dagster as dg


def _fetch_data(source: str = "api") -> dict:
    return {"source": source, "data": [1, 2, 3, 4, 5]}


def _process_data(data: dict) -> dict:
    return {"source": data["source"], "processed": [x * 2 for x in data["data"]]}


def _save_results(processed: dict) -> str:
    return f"Saved {len(processed['processed'])} items from {processed['source']}"


@dg.asset(
    key=["simple_sequential_pipeline", "processed_summary"],
    group_name="simple_sequential_pipeline",
    retry_policy=dg.RetryPolicy(max_retries=3, delay=300),
    kinds={"python"},
    tags={"airflow_dag": "simple_sequential_pipeline"},
    description="The one-line summary of the processed items; Airflow's fetch, process and save steps run inside this asset.",
)
def processed_summary() -> str:
    return _save_results(_process_data(_fetch_data()))


@dg.asset_check(asset=processed_summary, name="summary_present", blocking=False,
                description="Structural check (not a business check): the summary is a non-empty string.")
def summary_present(value: str) -> dg.AssetCheckResult:
    return dg.AssetCheckResult(passed=bool(value), metadata={"kind": "structural", "length": len(value or "")})


simple_sequential_pipeline_job = dg.define_asset_job("simple_sequential_pipeline", selection=[processed_summary])

simple_sequential_pipeline_schedule = dg.ScheduleDefinition(name="simple_sequential_pipeline_schedule", job=simple_sequential_pipeline_job, cron_schedule="0 9 * * *", execution_timezone="UTC")
