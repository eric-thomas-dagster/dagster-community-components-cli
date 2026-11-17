"""Converted from Airflow DAG `upstream4` (toys/toy_conditional_dataset_scheduling/upstream4.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_update_dataset_4() -> None:
    """
    Update the dataset
    """
    print("Updating dataset 4")


@dg.asset(
    key=['dataset4'],
    group_name='upstream4',
    metadata={'airflow_dag': 'upstream4', 'airflow_task': 'update_dataset_4', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_update_dataset_4(
    context: dg.AssetExecutionContext,
) -> None:
    _af_update_dataset_4()


upstream4_job = dg.define_asset_job('upstream4', selection=[dg.AssetKey(k) for k in [['dataset4']]])
