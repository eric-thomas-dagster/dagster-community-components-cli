"""Converted from Airflow DAG `upstream2` (toys/toy_conditional_dataset_scheduling/upstream2.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_update_dataset_2() -> None:
    """
    Update the dataset
    """
    print("Updating dataset 2")


@dg.asset(
    key=['dataset2'],
    group_name='upstream2',
    metadata={'airflow_dag': 'upstream2', 'airflow_task': 'update_dataset_2', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_update_dataset_2(
    context: dg.AssetExecutionContext,
) -> None:
    _af_update_dataset_2()


upstream2_job = dg.define_asset_job('upstream2', selection=[dg.AssetKey(k) for k in [['dataset2']]])
