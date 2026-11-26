"""Pattern: Airflow pools (`pool="x"`, `pool_slots`, `max_active_tis_per_dag(run)`).

NATIVE (verified present on the installed Dagster): the first-class `pool=` argument on `@dg.asset`, `@dg.multi_asset` and `@dg.op` (NOT on `@dg.graph_asset`: put it on the ops inside). The pool's LIMIT is
instance configuration, not code: `dagster instance concurrency set <pool> <n>` (the CLI exists, `dagster instance concurrency --help`); Airflow keeps the slot count in its metadata database, so the spec
does not carry it: take it from the source's comments or ask. `op_tags={"dagster/concurrency_key": ...}` is a different, older op tag (verified: it does not set `pool`); `tag_concurrency_limits` of the QueuedRunCoordinator works on RUN tags (run level; docs only, not run here). Use `pool=` for task pools.
`pool_slots` above 1 and `max_active_tis_per_dag(run)` have no one-to-one form: a per-asset limit is a pool of that size shared by every run (what the converter does), and a per-run limit is not expressible.
"""
import dagster as dg


@dg.asset(pool="launch_pad", retry_policy=dg.RetryPolicy(max_retries=2, delay=5))
def launch_iss_mission() -> str:
    return "launched"


@dg.op(pool="launch_pad")
def launch_op() -> str:
    return "launched"


@dg.graph_asset
def launch_graph() -> str:
    return launch_op()                                                  # the pool goes on the op, not on the graph_asset


defs = dg.Definitions(assets=[launch_iss_mission, launch_graph])
