"""Converted from Airflow DAG `basic_dataset_schedule_upstream_2` (2-10-example-dags/datasets/basic_dataset_schedule/basic_dataset_schedule_upstream_2.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_write_to_s3(*args, **kwargs):
    # AIRFLOW-CAVEAT: "'t_log' (used by the callable) depends on Airflow"
    return af_caveat("'t_log' (used by the callable) depends on Airflow", skip=False)



@dg.asset(
    key=['s3', 'basic_datasets', 'output_2_txt'],
    group_name='basic_dataset_schedule_upstream_2',
    metadata={'airflow_dag': 'basic_dataset_schedule_upstream_2', 'airflow_task': 'write_to_s3', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_write_to_s3(
    context: dg.AssetExecutionContext,
) -> None:
    _af_write_to_s3()


basic_dataset_schedule_upstream_2_job = dg.define_asset_job('basic_dataset_schedule_upstream_2', selection=[dg.AssetKey(k) for k in [['s3', 'basic_datasets', 'output_2_txt']]])
