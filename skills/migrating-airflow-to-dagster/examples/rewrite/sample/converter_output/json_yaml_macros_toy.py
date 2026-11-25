"""Converted from Airflow DAG `json_yaml_macros_toy` (2-6-example-dags/json_yaml_macros_toy.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_print_json_macro(*args, **kwargs):
    # AIRFLOW-CAVEAT: "template uses macros ['json'], which the runtime does not provide"
    return af_caveat("template uses macros ['json'], which the runtime does not provide", skip=False)



@dg.asset(
    key=['json_yaml_macros_toy', 'print_json_macro'],
    group_name='json_yaml_macros_toy',
    metadata={'airflow_dag': 'json_yaml_macros_toy', 'airflow_task': 'print_json_macro', 'airflow_operator': 'PythonOperator'},
    kinds={'python'},
)
def t_print_json_macro(
    context: dg.AssetExecutionContext,
) -> None:
    _af_print_json_macro()


def _af_print_yaml_macro(*args, **kwargs):
    # AIRFLOW-CAVEAT: "template uses macros ['yaml'], which the runtime does not provide"
    return af_caveat("template uses macros ['yaml'], which the runtime does not provide", skip=False)



@dg.asset(
    key=['json_yaml_macros_toy', 'print_yaml_macro'],
    group_name='json_yaml_macros_toy',
    metadata={'airflow_dag': 'json_yaml_macros_toy', 'airflow_task': 'print_yaml_macro', 'airflow_operator': 'PythonOperator'},
    kinds={'python'},
)
def t_print_yaml_macro(
    context: dg.AssetExecutionContext,
) -> None:
    _af_print_yaml_macro()


json_yaml_macros_toy_job = dg.define_asset_job('json_yaml_macros_toy', selection=[dg.AssetKey(k) for k in [['json_yaml_macros_toy', 'print_json_macro'], ['json_yaml_macros_toy', 'print_yaml_macro']]])
