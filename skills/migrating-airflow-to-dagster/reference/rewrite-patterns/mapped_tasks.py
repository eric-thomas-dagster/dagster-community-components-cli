"""Pattern: dynamic task mapping (`.expand(...)`).

FALLBACK: a plain loop inside the asset that owns the mapped task: one pass per item, results collected into a list (what Airflow's XCom list of the mapped instances is). Retries apply to the
asset, not to one item; say so. An EMPTY expansion: Airflow marks the mapped task `skipped` and its all_success downstream skipped; here the loop does nothing and returns [], so downstream
code must handle [] (`if not items: return` to reproduce the skip).
NATIVE (preferred): a graph asset with `DynamicOut(is_required=False)`, `.map` and `.collect` (below): one step per item, decided at run time, a retry per item. Verified: with ZERO items the dynamic output is
not yielded, the steps below `.map()` / `.collect()` are SKIPPED, the run SUCCEEDS and the asset is not materialized: Airflow's skip on an empty `expand`. (With the default `DynamicOut()` the collecting step instead
runs with `[]`: `fan_out_required` below.) The loop is the fallback when the native form is not verified for the case or failed a gate, or when the items are trivial and share one failure boundary.
"""
import dagster as dg


def process(item: int) -> int:
    return item * 2


@dg.asset
def mapped_in_python() -> list[int]:
    items = [1, 2, 3]                                                  # the expand() argument: a literal or an upstream value
    return [process(i) for i in items]


ITEMS = {"v": [1, 2, 3]}                                                  # the expand() argument (the tests change it)


@dg.op(out=dg.DynamicOut(is_required=False))
def fan_out():
    for i in ITEMS["v"]:
        yield dg.DynamicOutput(i, mapping_key=str(i))                       # nothing yielded when ITEMS is empty: everything below is skipped


@dg.op(out=dg.DynamicOut())
def fan_out_required():
    for i in ITEMS["v"]:
        yield dg.DynamicOutput(i, mapping_key=str(i))


@dg.op(retry_policy=dg.RetryPolicy(max_retries=2))                    # per-item retries
def process_item(i: int) -> int:
    return process(i)


@dg.op
def collect_items(results: list[int]) -> list[int]:
    return sorted(results)


@dg.graph_asset
def mapped_in_graph() -> list[int]:
    return collect_items(fan_out().map(process_item).collect())


@dg.op(out=dg.DynamicOut())
def fan_out_failing():
    for i in [1, 2, 3]:
        yield dg.DynamicOutput(i, mapping_key=str(i))


@dg.op
def process_item_failing(i: int) -> int:
    if i == 2:
        raise RuntimeError("item 2 failed")                              # Airflow: that mapped instance fails, the others run, the task after is upstream_failed
    return i


@dg.graph_asset
def mapped_with_a_failure() -> list[int]:
    return collect_items(fan_out_failing().map(process_item_failing).collect())


# ALTERNATIVE when the items are known ahead and each should be a tracked, re-runnable unit: DynamicPartitionsDefinition (a sensor or the code adds the keys).
items = dg.DynamicPartitionsDefinition(name="mapped_items")


@dg.asset(partitions_def=items)
def per_item(context: dg.AssetExecutionContext) -> str:
    return f"processed {context.partition_key}"


@dg.graph_asset
def mapped_in_graph_runs_on_empty() -> list:
    return collect_items(fan_out_required().map(process_item).collect())   # the default form: with zero items the collecting step still runs, with []


defs = dg.Definitions(assets=[mapped_in_python, mapped_in_graph, mapped_in_graph_runs_on_empty, mapped_with_a_failure, per_item])
