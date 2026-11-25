"""Converted from Airflow DAG `pool_example` (2-8-example-dags/pool_example.py) by convert_ir.py.

NOTE: dynamic task mapping became dynamic outputs inside one graph asset: the whole DAG runs as one Dagster run, mapped tasks are mapped ops and a task after them receives the collected list. Differences from Airflow: retries and pools apply per op; individual tasks cannot be re-run on their own (the asset is the unit), and an empty list skips the mapped step and gives the next step an empty list.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import itertools
import dagster as dg

from ..runtime import *  # noqa: F401,F403

import time


def _af_count_guests(**context):
    num_guests = context["params"]["num_guests"]
    print(f"There are {num_guests} people at my garden party!")
    print("They all want to jump into the pool!")
    return [i for i in range(num_guests)]


@dg.op(name='pool_example__count_guests')
def op_count_guests(context: dg.OpExecutionContext):
    ctx = af_context(context, 'pool_example', 'count_guests', {'num_guests': 23}, {})
    return call_with_context(_af_count_guests, [], {}, ctx)


def _af_jump_into_the_pool(guest):
    print(f"Guest number {guest} is jumping into the pool!")
    print(f"Guest number {guest} is swimming in the pool!")
    swim_time = 10
    time.sleep(swim_time)
    print(
        f"After {swim_time} seconds, guest number {guest} is getting out of the pool!"
    )


@dg.op(name='pool_example__jump_into_the_pool', pool='my_garden_pool')
def op_jump_into_the_pool(context: dg.OpExecutionContext, item):
    combo = {'guest': item}
    return _af_jump_into_the_pool(*[], **{**{}, **combo})


@dg.op(name='pool_example__fan_jump_into_the_pool', out=dg.DynamicOut())
def fan_jump_into_the_pool(src_0):
    srcs = [src_0]
    if any(isinstance(x, Skipped) for x in srcs):
        return
    if not all(isinstance(x, (list, tuple)) for x in srcs):
        raise dg.Failure(description='expected a list of items to map over')
    lists = [src_0]
    if not all(isinstance(x, (list, tuple)) for x in lists):
        raise dg.Failure(description='expected a list of items to map over')
    for n, vals in enumerate(itertools.product(*lists)):
        yield dg.DynamicOutput(vals[0], mapping_key=f'i{n}')


@dg.op(name='pool_example__collect_jump_into_the_pool')
def collect_jump_into_the_pool(context, items):
    items = list(items)
    return items if items else af_skip(context, False)      # Airflow skips a mapped task that expands to nothing


@dg.graph_multi_asset(name='pool_example_graph', group_name='pool_example', outs={
        'o0': dg.AssetOut(key=['pool_example', 'count_guests'], kinds={'python'}, metadata={'airflow_dag': 'pool_example', 'airflow_task': 'count_guests', 'airflow_operator': '_PythonDecoratedOperator'}),
        'o1': dg.AssetOut(key=['pool_example', 'jump_into_the_pool'], kinds={'python'}, metadata={'airflow_dag': 'pool_example', 'airflow_task': 'jump_into_the_pool', 'airflow_operator': '_PythonDecoratedOperator'}),
    })
def pool_example_graph():
    v0 = op_count_guests()
    d1 = fan_jump_into_the_pool(v0).map(lambda item: op_jump_into_the_pool(item))
    v1 = collect_jump_into_the_pool(d1.collect())
    return {'o0': v0, 'o1': v1}


pool_example_job = dg.define_asset_job('pool_example', selection=[dg.AssetKey(k) for k in [['pool_example', 'count_guests'], ['pool_example', 'jump_into_the_pool']]])
