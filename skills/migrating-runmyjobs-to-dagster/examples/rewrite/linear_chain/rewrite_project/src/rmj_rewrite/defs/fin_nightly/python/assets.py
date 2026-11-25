import dagster as dg


@dg.asset(deps=["fin_pull"], pool="batch_queue", retry_policy=dg.RetryPolicy(max_retries=1), group_name="fin_nightly")
def fin_transform(context: dg.AssetExecutionContext) -> None:
    context.log.info("transform")
