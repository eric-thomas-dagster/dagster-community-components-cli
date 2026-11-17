"""Converted from Airflow DAGs `producer_pipeline` and `consumer_pipeline`
(airflow_src/dag_with_datasets_producer.py and dag_with_datasets_consumer.py).

Airflow Datasets become asset keys: s3://bucket/processed_data/ -> s3/bucket/processed_data.
The producer's dataset outlet belongs to its last task (the source declares `outlets=` on the DAG,
which Airflow itself rejects; the intent is that save_processed_data produces the dataset).
The consumer's `schedule=[processed_data_dataset]` becomes an automation condition on its assets.
"""
import dagster as dg

PRODUCER, CONSUMER = "producer_pipeline", "consumer_pipeline"
PROCESSED = dg.AssetKey(["s3", "bucket", "processed_data"])
ANALYTICS = dg.AssetKey(["s3", "bucket", "analytics"])


# ---- producer_pipeline: schedule @daily ------------------------------------------------------
@dg.asset(key=[PRODUCER, "fetch_raw_data"], group_name=PRODUCER)
def fetch_raw_data() -> dict:
    """Fetch raw data from source."""
    print("Fetching raw data from API")
    data = {"records": [1, 2, 3, 4, 5]}
    print(f"Fetched {len(data['records'])} records")
    return data


@dg.asset(key=[PRODUCER, "process_data"], group_name=PRODUCER,
          ins={"data": dg.AssetIn(key=[PRODUCER, "fetch_raw_data"])})
def process_data(data: dict) -> dict:
    """Process raw data."""
    print("Processing data...")
    processed = [x * 2 for x in data["records"]]
    print(f"Processed {len(processed)} records")
    return {"processed": processed}


@dg.asset(key=PROCESSED, group_name=PRODUCER,
          ins={"data": dg.AssetIn(key=[PRODUCER, "process_data"])})
def save_processed_data(data: dict) -> dict:
    """Save processed data - produces a dataset."""
    print(f"Saving {len(data['processed'])} records to processed dataset")
    # In reality, this would write to S3, database, etc.
    return {"status": "saved", "count": len(data["processed"])}


producer_pipeline_job = dg.define_asset_job(PRODUCER, selection=dg.AssetSelection.groups(PRODUCER))

producer_pipeline_schedule = dg.ScheduleDefinition(
    name=f"{PRODUCER}_schedule", job=producer_pipeline_job, cron_schedule="0 0 * * *", execution_timezone="UTC",
)


# ---- consumer_pipeline: schedule=[processed_data_dataset] ------------------------------------
@dg.asset(key=[CONSUMER, "load_and_analyze"], group_name=CONSUMER, deps=[PROCESSED],
          automation_condition=dg.AutomationCondition.eager())
def load_and_analyze() -> dict:
    """Load data from the processed dataset and run analytics."""
    print("Loading data from processed dataset")
    print("Running analytics...")
    return {"analytics_complete": True, "insights": ["pattern_1", "pattern_2"]}


@dg.asset(key=ANALYTICS, group_name=CONSUMER,
          ins={"results": dg.AssetIn(key=[CONSUMER, "load_and_analyze"])},
          automation_condition=dg.AutomationCondition.eager())
def publish_results(results: dict) -> str:
    """Publish analytics results."""
    print(f"Publishing analytics results: {results}")
    print(f"Found {len(results.get('insights', []))} insights")
    return "Analytics published"
