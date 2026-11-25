"""Hand-written parts of NIGHTLY_SALES_LOAD: the reimplemented RedwoodScript step and the notification sensor (no registry component runs or logs a plain command)."""
import dagster as dg


@dg.asset(deps=["fact_sales", "fact_inventory"], group_name="nightly_sales_load")
def sales_margin(context: dg.AssetExecutionContext) -> None:
    context.log.info("margin done")


@dg.run_status_sensor(run_status=dg.DagsterRunStatus.SUCCESS, name="notify_done", default_status=dg.DefaultSensorStatus.STOPPED)
def notify_done(context: dg.RunStatusSensorContext):
    if context.dagster_run.job_name == "nightly_sales_load":
        context.log.info("notifying")
