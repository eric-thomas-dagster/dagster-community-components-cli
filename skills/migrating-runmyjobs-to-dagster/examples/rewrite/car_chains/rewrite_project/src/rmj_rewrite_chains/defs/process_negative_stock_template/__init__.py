"""RunMyJobs chain Process_Negative_Stock_Template, rewritten from its spec (rewrite route).

Both calls are the built-in no-op System_DoNothing (placeholders); two logged placeholder assets in order.

- Process_Negative_Stock_Template/count negative stock: replaced by `count_negative_stock` (reimplemented as a logged placeholder)
- Process_Negative_Stock_Template/create pids: replaced by `create_pids` (reimplemented as a logged placeholder)
"""
import dagster as dg


@dg.asset(group_name="process_negative_stock")
def count_negative_stock(context: dg.AssetExecutionContext) -> None:
    context.log.info("count negative stock: placeholder for a RunMyJobs step that does nothing")


@dg.asset(deps=[count_negative_stock], group_name="process_negative_stock")
def create_pids(context: dg.AssetExecutionContext) -> None:
    context.log.info("create pids: placeholder for a RunMyJobs step that does nothing")
