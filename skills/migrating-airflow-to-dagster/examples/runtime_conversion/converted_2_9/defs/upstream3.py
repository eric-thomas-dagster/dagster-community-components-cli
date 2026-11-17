"""Converted from Airflow DAG `upstream3` (toys/toy_conditional_dataset_scheduling/upstream3.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_update_dataset_3() -> None:
    """
    Update the dataset
    """
    print("Updating dataset 3")


@dg.asset(
    key=['dataset3'],
    group_name='upstream3',
    metadata={'airflow_dag': 'upstream3', 'airflow_task': 'update_dataset_3', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_update_dataset_3(
    context: dg.AssetExecutionContext,
) -> None:
    _af_update_dataset_3()


upstream3_job = dg.define_asset_job('upstream3', selection=[dg.AssetKey(k) for k in [['dataset3']]])
