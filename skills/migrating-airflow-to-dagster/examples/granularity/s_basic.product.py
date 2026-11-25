"""Converted from Airflow DAG `s_basic` (s_basic.py) by convert_ir.py.

NOTE: granularity=product: 1 step task(s) were folded into the asset they feed (a into b); they restart with that asset (plain Python steps restart from the beginning), so they no longer have asset keys of their own

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_b(msg="hi"):
    print(msg)
    return msg


@dg.asset(
    key=['s_basic', 'b'],
    group_name='s_basic',
    retry_policy=dg.RetryPolicy(max_retries=2, delay=300),
    metadata={'airflow_dag': 's_basic', 'airflow_task': 'b', 'airflow_operator': 'PythonOperator'},
    kinds={'python'},
)
def t_b(
    context: dg.AssetExecutionContext,
):
    _v_a = run_shell(context, 'echo hello', env=None, cwd=None, timeout=None)      # folded from Airflow task 'a'
    return _af_b(msg='x')


s_basic_job = dg.define_asset_job('s_basic', selection=[dg.AssetKey(k) for k in [['s_basic', 'b']]])

s_basic_schedule = dg.ScheduleDefinition(name='s_basic_schedule', job=s_basic_job, cron_schedule='0 0 * * *', execution_timezone='UTC')
