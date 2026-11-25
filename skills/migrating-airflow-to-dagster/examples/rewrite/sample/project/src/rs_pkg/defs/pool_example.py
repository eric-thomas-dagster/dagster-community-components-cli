"""Rewrite of Airflow DAG `pool_example` (count guests from a param, each one "jumps into the pool", limited by a pool).

Airflow tasks:
- count_guests: folded into asset `pool_example/garden_party` (an op that emits one dynamic output per guest; `num_guests` is run config, default 23, the DAG's `params`)
- jump_into_the_pool: replaced by asset `pool_example/garden_party` (a mapped op per guest in the graph asset, with the pool `my_garden_pool`; each takes 10 seconds, as in Airflow)

Shape: one graph-backed asset (`@dg.graph_asset`) because the mapped step has a pool and each guest deserves its own op (a failed guest retries as an op). The pool's slot count lives in Airflow's metadata
database and is not in the spec: set the limit with `dagster instance concurrency set my_garden_pool <slots>` (an unset pool is unlimited, so the parallelism differs until it is set).
Registry: `dynamic_fanout_asset` (tier live) fans out a callable over items as a graph asset and takes a concurrency key, but it needs `module:function` callables and YAML for a loop this small, and the pool is
an op argument here; not used. No schedule (manual DAG in Airflow). Not preserved: per-instance task records (one op result per guest).
"""
import time

import dagster as dg

SWIM_SECONDS = 10


class PartyConfig(dg.Config):
    num_guests: int = 23           # the DAG's `params`


@dg.op(out=dg.DynamicOut())
def count_guests(context: dg.OpExecutionContext, config: PartyConfig):
    context.log.info(f"There are {config.num_guests} people at my garden party! They all want to jump into the pool!")
    for i in range(config.num_guests):
        yield dg.DynamicOutput(i, mapping_key=f"guest_{i}")


@dg.op(pool="my_garden_pool")
def jump_into_the_pool(context: dg.OpExecutionContext, guest: int) -> int:
    context.log.info(f"Guest number {guest} is jumping into the pool! swimming for {SWIM_SECONDS} seconds")
    time.sleep(SWIM_SECONDS)
    context.log.info(f"After {SWIM_SECONDS} seconds, guest number {guest} is getting out of the pool!")
    return guest


@dg.op
def collect_guests(guests: list[int]) -> list[int]:
    return sorted(guests)


@dg.graph_asset(key=["pool_example", "garden_party"], group_name="pool_example", description="Every guest that jumped into the pool (ids), one op per guest under the pool my_garden_pool.")
def garden_party() -> list[int]:
    return collect_guests(count_guests().map(jump_into_the_pool).collect())


@dg.asset_check(asset=garden_party, name="every_guest_swam", blocking=False, description="Structural check (not a business check): the list is not empty.")
def every_guest_swam(value: list) -> dg.AssetCheckResult:
    return dg.AssetCheckResult(passed=bool(value), metadata={"kind": "structural", "guests": len(value or [])})


pool_example_job = dg.define_asset_job("pool_example", selection=[garden_party])
