"""The AWS-blog dag-factory pattern: N identical DAGs that differ only in one argument (a country), as ONE asset
partitioned by that argument, with ONE schedule that requests every partition."""
import dagster as dg

COUNTRIES = ["Australia", "Brazil", "India", "Japan"]          # the op_args that vary across the YAML's DAG blocks
PDEF = dg.StaticPartitionsDefinition(COUNTRIES)
CALLS = []

def process_s3_data(country):                                    # the same callable the YAML points at
    CALLS.append(country)
    print(f"processing {country}")

@dg.asset(partitions_def=PDEF, retry_policy=dg.RetryPolicy(max_retries=1, delay=300), group_name="module3")
def processed_country_data(context: dg.AssetExecutionContext) -> None:
    process_s3_data(context.partition_key)

job = dg.define_asset_job("module3_dynamic_dag", selection=[processed_country_data], partitions_def=PDEF)
@dg.schedule(job=job, cron_schedule="*/10 * * * *", name="module3_dynamic_dag_schedule")
def schedule(context: dg.ScheduleEvaluationContext):
    return [dg.RunRequest(partition_key=c, run_key=f"{context.scheduled_execution_time.isoformat()}:{c}") for c in COUNTRIES]
