"""Converted from Airflow DAG `upstream1` (toys/toy_conditional_dataset_scheduling/upstream1.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_update_dataset_1() -> None:
    """
    Update the dataset
    """
    import time

    time.sleep(30)
    print("Updating dataset 1")


@dg.asset(
    key=['dataset1'],
    group_name='upstream1',
    deps=[dg.AssetKey(['dataset0'])],
    automation_condition=dg.AutomationCondition.eager(),
    metadata={'airflow_dag': 'upstream1', 'airflow_task': 'update_dataset_1', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_update_dataset_1(
    context: dg.AssetExecutionContext,
) -> None:
    _af_update_dataset_1()


upstream1_job = dg.define_asset_job('upstream1', selection=[dg.AssetKey(k) for k in [['dataset1']]])
