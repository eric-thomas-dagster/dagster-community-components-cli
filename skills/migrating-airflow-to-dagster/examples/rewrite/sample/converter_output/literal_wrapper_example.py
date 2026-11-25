"""Converted from Airflow DAG `literal_wrapper_example` (2-8-example-dags/literal_wrapper_example.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


@dg.asset(
    key=['literal_wrapper_example', 'echo_a_jinja_template'],
    group_name='literal_wrapper_example',
    metadata={'airflow_dag': 'literal_wrapper_example', 'airflow_task': 'echo_a_jinja_template', 'airflow_operator': 'BashOperator'},
    kinds={'shell'},
)
def t_echo_a_jinja_template(
    context: dg.AssetExecutionContext,
) -> None:
    ctx = af_context(context, 'literal_wrapper_example', 'echo_a_jinja_template', {'the_best_number': 231942}, {})
    run_shell(context, render(ctx, 'echo {{ params.the_best_number }}'), env=None, cwd=None, timeout=None)


@dg.asset(
    key=['literal_wrapper_example', 'use_literal_wrapper_to_ignore_jinja_template'],
    group_name='literal_wrapper_example',
    metadata={'airflow_dag': 'literal_wrapper_example', 'airflow_task': 'use_literal_wrapper_to_ignore_jinja_template', 'airflow_operator': 'BashOperator'},
    kinds={'shell'},
)
def t_use_literal_wrapper_to_ignore_jinja_template(
    context: dg.AssetExecutionContext,
) -> None:
    run_shell(context, '{% raw %}echo {{ params.the_best_number }}{% endraw %}', env=None, cwd=None, timeout=None)


literal_wrapper_example_job = dg.define_asset_job('literal_wrapper_example', selection=[dg.AssetKey(k) for k in [['literal_wrapper_example', 'echo_a_jinja_template'], ['literal_wrapper_example', 'use_literal_wrapper_to_ignore_jinja_template']]])
