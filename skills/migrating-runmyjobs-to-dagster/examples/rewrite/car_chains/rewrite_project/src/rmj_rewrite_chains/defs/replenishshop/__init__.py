"""RunMyJobs chain ReplenishShop, rewritten from its spec (rewrite route).

Every call is the built-in no-op System_DoNothing: the chain is an orchestration skeleton. It becomes four placeholder assets in order (so the graph, and later real jobs, have a place),
each logging its step; nothing runs on a shell.

- ReplenishShop/wait for truck: replaced by `wait_for_truck` (reimplemented: the no-op becomes a logged placeholder)
- ReplenishShop/calculate replenishment: replaced by `calculate_replenishment` (reimplemented as a logged placeholder)
- ReplenishShop/pick orders: replaced by `pick_orders` (reimplemented as a logged placeholder)
- ReplenishShop/confirmation truck loaded: replaced by `confirmation_truck_loaded` (reimplemented as a logged placeholder)

The chain parameters P_REGION and P_AREA have no value in the export and are not used by any call, so nothing reads them.
"""
import dagster as dg


def _placeholder(context: dg.AssetExecutionContext, step: str) -> None:
    context.log.info(f"{step}: placeholder for a RunMyJobs step that does nothing")


@dg.asset(group_name="replenishshop")
def wait_for_truck(context: dg.AssetExecutionContext) -> None:
    _placeholder(context, "wait for truck")


@dg.asset(deps=[wait_for_truck], group_name="replenishshop")
def calculate_replenishment(context: dg.AssetExecutionContext) -> None:
    _placeholder(context, "calculate replenishment")


@dg.asset(deps=[calculate_replenishment], group_name="replenishshop")
def pick_orders(context: dg.AssetExecutionContext) -> None:
    _placeholder(context, "pick orders")


@dg.asset(deps=[pick_orders], group_name="replenishshop")
def confirmation_truck_loaded(context: dg.AssetExecutionContext) -> None:
    _placeholder(context, "confirmation truck loaded")
