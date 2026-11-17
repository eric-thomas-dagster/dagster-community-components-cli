"""Converted from Airflow DAG `toy_downstream3_dataset_and_time_schedule` (toys/toy_conditional_dataset_scheduling/downstream3_dataset_and_time_schedule.py) by convert_ir.py.

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
    key=['toy_downstream3_dataset_and_time_schedule', 'say_hello'],
    group_name='toy_downstream3_dataset_and_time_schedule',
    deps=[dg.AssetKey(['dataset3']), dg.AssetKey(['dataset4'])],
    automation_condition=dg.AutomationCondition.eager(),
    metadata={'airflow_dag': 'toy_downstream3_dataset_and_time_schedule', 'airflow_task': 'say_hello', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_say_hello(
    context: dg.AssetExecutionContext,
) -> None:
    _af_say_hello()


toy_downstream3_dataset_and_time_schedule_job = dg.define_asset_job('toy_downstream3_dataset_and_time_schedule', selection=[dg.AssetKey(k) for k in [['toy_downstream3_dataset_and_time_schedule', 'say_hello']]])
