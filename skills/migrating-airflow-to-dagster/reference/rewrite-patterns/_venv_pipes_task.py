"""The code that runs inside the task's interpreter (it imports only dagster_pipes)."""
from dagster_pipes import open_dagster_pipes

with open_dagster_pipes() as pipes:
    pipes.log.info("running in the task's own interpreter")
    pipes.report_asset_materialization(metadata={"rows": 3})
