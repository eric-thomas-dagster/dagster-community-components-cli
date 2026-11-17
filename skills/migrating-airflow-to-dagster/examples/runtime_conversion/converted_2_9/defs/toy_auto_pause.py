"""Converted from Airflow DAG `toy_auto_pause` (toys/toy_auto_pause.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_say_hello() -> None:
    raise (Exception("Hello, I am an exception!"))


@dg.asset(
    key=['toy_auto_pause', 'say_hello'],
    group_name='toy_auto_pause',
    metadata={'airflow_dag': 'toy_auto_pause', 'airflow_task': 'say_hello', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_say_hello(
    context: dg.AssetExecutionContext,
) -> None:
    _af_say_hello()


toy_auto_pause_job = dg.define_asset_job('toy_auto_pause', selection=[dg.AssetKey(k) for k in [['toy_auto_pause', 'say_hello']]])

toy_auto_pause_schedule = dg.ScheduleDefinition(name='toy_auto_pause_schedule', job=toy_auto_pause_job, cron_schedule='0 * * * *', execution_timezone='UTC')
