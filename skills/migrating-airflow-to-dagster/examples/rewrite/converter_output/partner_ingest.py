"""Converted from Airflow DAG `partner_ingest` (partner_ingest.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_wait_for_export(*args, **kwargs):
    # AIRFLOW-CAVEAT: 'HttpSensor: response_check is a function (unusual_prefix_fe4965ea2b0dd5558582e422850ae884dbae0f18_partner_ingest._export_ready); the converter does not carry it'
    return af_caveat('HttpSensor: response_check is a function (unusual_prefix_fe4965ea2b0dd5558582e422850ae884dbae0f18_partner_ingest._export_ready); the converter does not carry it into the step', skip=False)



@dg.asset(
    key=['partner_ingest', 'wait_for_export'],
    group_name='partner_ingest',
    retry_policy=dg.RetryPolicy(max_retries=2, delay=600),
    metadata={'airflow_dag': 'partner_ingest', 'airflow_task': 'wait_for_export', 'airflow_operator': 'PythonOperator'},
    kinds={'python'},
)
def t_wait_for_export(
    context: dg.AssetExecutionContext,
) -> None:
    _af_wait_for_export()


@dg.asset(
    key=['partner_ingest', 'remove_tmp'],
    group_name='partner_ingest',
    deps=[dg.AssetKey(['partner_ingest', 'publish'])],
    retry_policy=dg.RetryPolicy(max_retries=2, delay=600),
    metadata={'airflow_dag': 'partner_ingest', 'airflow_task': 'remove_tmp', 'airflow_operator': 'BashOperator'},
    kinds={'shell'},
)
def t_remove_tmp(
    context: dg.AssetExecutionContext,
) -> None:
    run_shell(context, 'rm -rf /tmp/partner_export_tmp', env=None, cwd=None, timeout=None)


def _af_fetch_export(*args, **kwargs):
    # AIRFLOW-CAVEAT: 'callable depends on an Airflow import (airflow.hooks.base)'
    return af_caveat('callable depends on an Airflow import (airflow.hooks.base)', skip=False)



@dg.asset(
    key=['partner_ingest', 'fetch_export'],
    group_name='partner_ingest',
    deps=[dg.AssetKey(['partner_ingest', 'wait_for_export'])],
    retry_policy=dg.RetryPolicy(max_retries=2, delay=600),
    metadata={'airflow_dag': 'partner_ingest', 'airflow_task': 'fetch_export', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_fetch_export(
    context: dg.AssetExecutionContext,
):
    return _af_fetch_export()


def _af_reshape(rows: list[dict]) -> dict:
    """One record per customer, amounts summed, rows without an id dropped."""
    out: dict[str, float] = {}
    for r in rows:
        if r.get("customer_id"):
            out[r["customer_id"]] = out.get(r["customer_id"], 0.0) + float(r.get("amount", 0))
    return {"customers": len(out), "total": round(sum(out.values()), 2), "by_customer": out}


@dg.asset(
    key=['partner_ingest', 'reshape'],
    group_name='partner_ingest',
    ins={'in_0': dg.AssetIn(key=['partner_ingest', 'fetch_export'])},
    retry_policy=dg.RetryPolicy(max_retries=2, delay=600),
    metadata={'airflow_dag': 'partner_ingest', 'airflow_task': 'reshape', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_reshape(
    context: dg.AssetExecutionContext,
    in_0,
):
    return _af_reshape(in_0)


def _af_publish(summary: dict) -> str:
    line = f"partner export: {summary['customers']} customers, total {summary['total']}"
    print(line)
    return line


@dg.asset(
    key=['partner_ingest', 'publish'],
    group_name='partner_ingest',
    ins={'in_0': dg.AssetIn(key=['partner_ingest', 'reshape'])},
    retry_policy=dg.RetryPolicy(max_retries=2, delay=600),
    metadata={'airflow_dag': 'partner_ingest', 'airflow_task': 'publish', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_publish(
    context: dg.AssetExecutionContext,
    in_0,
) -> None:
    _af_publish(in_0)


@dg.asset_check(asset=dg.AssetKey(['partner_ingest', 'fetch_export']), name='returned_value_present', blocking=False,
                description="Structural check generated from the Airflow DAG (not a business check): the task's return value is consumed downstream, so it must not be None; an empty list, dict, string or DataFrame is a warning.")
def chk_fetch_export_value(value) -> dg.AssetCheckResult:
    if value is None:
        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.ERROR, metadata={'kind': 'structural', 'why': 'the value is None but a downstream task consumes it'})
    size = structural_size(value)
    if size == 0:
        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.WARN, metadata={'kind': 'structural', 'size': 0, 'why': 'the value is empty'})
    return dg.AssetCheckResult(passed=True, metadata={'kind': 'structural', 'type': type(value).__name__, **({'size': size} if size is not None else {})})


@dg.asset_check(asset=dg.AssetKey(['partner_ingest', 'reshape']), name='returned_value_present', blocking=False,
                description="Structural check generated from the Airflow DAG (not a business check): the task's return value is consumed downstream, so it must not be None; an empty list, dict, string or DataFrame is a warning.")
def chk_reshape_value(value) -> dg.AssetCheckResult:
    if value is None:
        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.ERROR, metadata={'kind': 'structural', 'why': 'the value is None but a downstream task consumes it'})
    size = structural_size(value)
    if size == 0:
        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.WARN, metadata={'kind': 'structural', 'size': 0, 'why': 'the value is empty'})
    return dg.AssetCheckResult(passed=True, metadata={'kind': 'structural', 'type': type(value).__name__, **({'size': size} if size is not None else {})})


partner_ingest_job = dg.define_asset_job('partner_ingest', selection=[dg.AssetKey(k) for k in [['partner_ingest', 'wait_for_export'], ['partner_ingest', 'remove_tmp'], ['partner_ingest', 'fetch_export'], ['partner_ingest', 'reshape'], ['partner_ingest', 'publish']]])

partner_ingest_schedule = dg.ScheduleDefinition(name='partner_ingest_schedule', job=partner_ingest_job, cron_schedule='0 */6 * * *', execution_timezone='UTC')
