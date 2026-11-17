"""Assets that converted DAGs read or are triggered by but that no converted DAG produces."""
import dagster as dg


@dg.definitions
def sources() -> dg.Definitions:
    return dg.Definitions(assets=[
        dg.AssetSpec(key=['dataset0']),
    ])
