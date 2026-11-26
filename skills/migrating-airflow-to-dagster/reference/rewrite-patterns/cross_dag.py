"""Pattern: TriggerDagRunOperator / ExternalTaskSensor / asset-triggered DAGs.

NATIVE: the other DAG's asset is a dependency (an `AssetSpec` for something another code location owns, or the real asset), and the downstream asset carries `AutomationCondition.eager()` (runs when an
upstream asset updates) or `AutomationCondition.on_cron("<cron>")` (runs on a schedule once upstream has updated). Verified: after the upstream asset materialized, `eager()` requests the downstream asset.
Imperative trigger with conf (TriggerDagRunOperator(conf=...)): a `run_status_sensor` on the triggering job's success that yields `RunRequest(run_config=...)` for the target job. The automation condition
runs a separate run, so Airflow's `wait_for_completion=True` (the trigger blocks until the other DAG finished) is not reproduced; say so.
"""
import dagster as dg

upstream_dag_output = dg.AssetSpec("upstream_dag_output")             # another DAG's asset (external to this code location)


@dg.asset(deps=[upstream_dag_output], automation_condition=dg.AutomationCondition.eager())
def downstream_report() -> str:
    return "ran after the upstream asset updated"


@dg.asset(deps=["downstream_report"], automation_condition=dg.AutomationCondition.on_cron("0 6 * * *"))
def morning_digest() -> str:
    return "ran on the cron once its upstream was updated"


@dg.asset
def source_asset() -> int:
    return 1


source_job = dg.define_asset_job("source_job", selection=[source_asset])


@dg.job
def target_job():
    @dg.op(config_schema={"conf_value": str})
    def use_conf(context):
        return context.op_config["conf_value"]
    use_conf()


@dg.run_status_sensor(run_status=dg.DagsterRunStatus.SUCCESS, monitored_jobs=[source_job], request_job=target_job)
def trigger_target(context: dg.RunStatusSensorContext):
    return dg.RunRequest(run_key=context.dagster_run.run_id, run_config={"ops": {"use_conf": {"config": {"conf_value": "from the trigger"}}}})


defs = dg.Definitions(assets=[downstream_report, morning_digest, source_asset], jobs=[source_job, target_job], sensors=[trigger_target])
