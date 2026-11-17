"""Converted from Airflow DAG `downstream1_on_any` (toys/toy_conditional_dataset_scheduling/downstream1_on_any.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_say_hello() -> None:
    """
    Print Hello
    """
    import time 
    time.sleep(10)
    print("Hello")


@dg.asset(
    key=['downstream1_on_any', 'say_hello'],
    group_name='downstream1_on_any',
    deps=[dg.AssetKey(['dataset1']), dg.AssetKey(['dataset2']), dg.AssetKey(['dataset3']), dg.AssetKey(['dataset4'])],
    automation_condition=dg.AutomationCondition.eager(),
    metadata={'airflow_dag': 'downstream1_on_any', 'airflow_task': 'say_hello', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_say_hello(
    context: dg.AssetExecutionContext,
) -> None:
    _af_say_hello()


downstream1_on_any_job = dg.define_asset_job('downstream1_on_any', selection=[dg.AssetKey(k) for k in [['downstream1_on_any', 'say_hello']]])
