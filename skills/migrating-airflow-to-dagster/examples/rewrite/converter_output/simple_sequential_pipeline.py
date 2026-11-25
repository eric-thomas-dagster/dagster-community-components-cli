"""Converted from Airflow DAG `simple_sequential_pipeline` (simple_sequential.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_fetch_data(source: str = "api") -> dict:
    """Fetch data from a source."""
    print(f"Fetching data from {source}")
    return {"source": source, "data": [1, 2, 3, 4, 5]}


@dg.asset(
    key=['simple_sequential_pipeline', 'fetch_data'],
    group_name='simple_sequential_pipeline',
    retry_policy=dg.RetryPolicy(max_retries=3, delay=300),
    metadata={'airflow_dag': 'simple_sequential_pipeline', 'airflow_task': 'fetch_data', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_fetch_data(
    context: dg.AssetExecutionContext,
):
    return _af_fetch_data()


def _af_process_data(data: dict) -> dict:
    """Process the fetched data."""
    print(f"Processing data from {data['source']}")
    processed = {
        "source": data["source"],
        "processed": [x * 2 for x in data["data"]]
    }
    return processed


@dg.asset(
    key=['simple_sequential_pipeline', 'process_data'],
    group_name='simple_sequential_pipeline',
    ins={'in_0': dg.AssetIn(key=['simple_sequential_pipeline', 'fetch_data'])},
    retry_policy=dg.RetryPolicy(max_retries=3, delay=300),
    metadata={'airflow_dag': 'simple_sequential_pipeline', 'airflow_task': 'process_data', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_process_data(
    context: dg.AssetExecutionContext,
    in_0,
):
    return _af_process_data(in_0)


def _af_save_results(processed: dict) -> str:
    """Save the processed results."""
    result = f"Saved {len(processed['processed'])} items from {processed['source']}"
    print(result)
    return result


@dg.asset(
    key=['simple_sequential_pipeline', 'save_results'],
    group_name='simple_sequential_pipeline',
    ins={'in_0': dg.AssetIn(key=['simple_sequential_pipeline', 'process_data'])},
    retry_policy=dg.RetryPolicy(max_retries=3, delay=300),
    metadata={'airflow_dag': 'simple_sequential_pipeline', 'airflow_task': 'save_results', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_save_results(
    context: dg.AssetExecutionContext,
    in_0,
) -> None:
    _af_save_results(in_0)


@dg.asset_check(asset=dg.AssetKey(['simple_sequential_pipeline', 'fetch_data']), name='returned_value_present', blocking=False,
                description="Structural check generated from the Airflow DAG (not a business check): the task's return value is consumed downstream, so it must not be None; an empty list, dict, string or DataFrame is a warning.")
def chk_fetch_data_value(value) -> dg.AssetCheckResult:
    if value is None:
        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.ERROR, metadata={'kind': 'structural', 'why': 'the value is None but a downstream task consumes it'})
    size = structural_size(value)
    if size == 0:
        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.WARN, metadata={'kind': 'structural', 'size': 0, 'why': 'the value is empty'})
    return dg.AssetCheckResult(passed=True, metadata={'kind': 'structural', 'type': type(value).__name__, **({'size': size} if size is not None else {})})


@dg.asset_check(asset=dg.AssetKey(['simple_sequential_pipeline', 'process_data']), name='returned_value_present', blocking=False,
                description="Structural check generated from the Airflow DAG (not a business check): the task's return value is consumed downstream, so it must not be None; an empty list, dict, string or DataFrame is a warning.")
def chk_process_data_value(value) -> dg.AssetCheckResult:
    if value is None:
        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.ERROR, metadata={'kind': 'structural', 'why': 'the value is None but a downstream task consumes it'})
    size = structural_size(value)
    if size == 0:
        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.WARN, metadata={'kind': 'structural', 'size': 0, 'why': 'the value is empty'})
    return dg.AssetCheckResult(passed=True, metadata={'kind': 'structural', 'type': type(value).__name__, **({'size': size} if size is not None else {})})


simple_sequential_pipeline_job = dg.define_asset_job('simple_sequential_pipeline', selection=[dg.AssetKey(k) for k in [['simple_sequential_pipeline', 'fetch_data'], ['simple_sequential_pipeline', 'process_data'], ['simple_sequential_pipeline', 'save_results']]])

simple_sequential_pipeline_schedule = dg.ScheduleDefinition(name='simple_sequential_pipeline_schedule', job=simple_sequential_pipeline_job, cron_schedule='0 9 * * *', execution_timezone='UTC')
