"""Converted from Airflow DAG `babel_format` (2-10-example-dags/other/babel_format.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_welcher_tag_ist_heute(*args, **kwargs):
    # AIRFLOW-CAVEAT: "template uses macros ['ds_format_locale'], which the runtime does not provide"
    return af_caveat("template uses macros ['ds_format_locale'], which the runtime does not provide", skip=False)



@dg.asset(
    key=['babel_format', 'welcher_tag_ist_heute'],
    group_name='babel_format',
    metadata={'airflow_dag': 'babel_format', 'airflow_task': 'welcher_tag_ist_heute', 'airflow_operator': 'PythonOperator'},
    kinds={'python'},
)
def t_welcher_tag_ist_heute(
    context: dg.AssetExecutionContext,
) -> None:
    _af_welcher_tag_ist_heute()


babel_format_job = dg.define_asset_job('babel_format', selection=[dg.AssetKey(k) for k in [['babel_format', 'welcher_tag_ist_heute']]])
