"""Converted from Airflow DAG `toy_task_duration_page` (toys/toy_task_duration_page.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403

import time
import random


def _af_two_second_plus_delay_task() -> None:
    delay = random.randint(0, 10)
    time.sleep(2 + delay)


@dg.asset(
    key=['toy_task_duration_page', 'two_second_plus_delay_task'],
    group_name='toy_task_duration_page',
    metadata={'airflow_dag': 'toy_task_duration_page', 'airflow_task': 'two_second_plus_delay_task', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_two_second_plus_delay_task(
    context: dg.AssetExecutionContext,
) -> None:
    _af_two_second_plus_delay_task()


def _af_five_second_plus_delay_task() -> None:
    delay = random.randint(0, 10)
    time.sleep(5 + delay)


@dg.asset(
    key=['toy_task_duration_page', 'five_second_plus_delay_task'],
    group_name='toy_task_duration_page',
    metadata={'airflow_dag': 'toy_task_duration_page', 'airflow_task': 'five_second_plus_delay_task', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_five_second_plus_delay_task(
    context: dg.AssetExecutionContext,
) -> None:
    _af_five_second_plus_delay_task()


def _af_ten_second_plus_delay_task() -> None:
    delay = random.randint(0, 10)
    time.sleep(10 + delay)


@dg.asset(
    key=['toy_task_duration_page', 'ten_second_plus_delay_task'],
    group_name='toy_task_duration_page',
    metadata={'airflow_dag': 'toy_task_duration_page', 'airflow_task': 'ten_second_plus_delay_task', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_ten_second_plus_delay_task(
    context: dg.AssetExecutionContext,
) -> None:
    _af_ten_second_plus_delay_task()


def _af_twenty_second_plus_delay_task() -> None:
    delay = random.randint(0, 10)
    time.sleep(20 + delay)


@dg.asset(
    key=['toy_task_duration_page', 'twenty_second_plus_delay_task'],
    group_name='toy_task_duration_page',
    metadata={'airflow_dag': 'toy_task_duration_page', 'airflow_task': 'twenty_second_plus_delay_task', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_twenty_second_plus_delay_task(
    context: dg.AssetExecutionContext,
) -> None:
    _af_twenty_second_plus_delay_task()


def _af_thirty_second_plus_delay_task() -> None:
    delay = random.randint(0, 10)
    time.sleep(30 + delay)


@dg.asset(
    key=['toy_task_duration_page', 'thirty_second_plus_delay_task'],
    group_name='toy_task_duration_page',
    metadata={'airflow_dag': 'toy_task_duration_page', 'airflow_task': 'thirty_second_plus_delay_task', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_thirty_second_plus_delay_task(
    context: dg.AssetExecutionContext,
) -> None:
    _af_thirty_second_plus_delay_task()


toy_task_duration_page_job = dg.define_asset_job('toy_task_duration_page', selection=[dg.AssetKey(k) for k in [['toy_task_duration_page', 'two_second_plus_delay_task'], ['toy_task_duration_page', 'five_second_plus_delay_task'], ['toy_task_duration_page', 'ten_second_plus_delay_task'], ['toy_task_duration_page', 'twenty_second_plus_delay_task'], ['toy_task_duration_page', 'thirty_second_plus_delay_task']]])

toy_task_duration_page_schedule = dg.ScheduleDefinition(name='toy_task_duration_page_schedule', job=toy_task_duration_page_job, cron_schedule='0 0 * * *', execution_timezone='UTC')
