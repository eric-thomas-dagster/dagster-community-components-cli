"""Pattern: Jinja templates, params, Variables, connections. Dagster has no Jinja: translate each template field into code (the converter's set: ds, ds_nodash, ts,
logical_date, data_interval_start/end, ds_add, ds_format; use the same translations, not new ones).
  {{ ds }}, {{ data_interval_start }}/_end  -> `context.partition_key` and `context.partition_time_window` (verified) of a time-partitioned asset: ADD a `DailyPartitionsDefinition` when the DAG depends on the interval;
                                              unpartitioned assets only have the run time (`datetime.now(utc)` or the schedule tick tag): say that the interval is not the same thing.
  {{ params.x }}                            -> a `dg.Config` subclass argument (verified), with the DAG's param default as the field default.
  {{ var.value.x }}, Variable.get("x")      -> an environment variable read by name (`dg.EnvVar("X")` in a resource or config), never a value in code.
  connections                               -> a `ConfigurableResource` whose fields are `dg.EnvVar`s, injected as an asset argument and replaceable by a mock in tests (verified). Names only; no secret value.
"""
import datetime as dt
import os

import dagster as dg

daily = dg.DailyPartitionsDefinition(start_date="2024-01-01")


@dg.asset(partitions_def=daily)
def interval_asset(context: dg.AssetExecutionContext) -> dict:
    tw = context.partition_time_window
    return {"ds": context.partition_key, "ds_add_7": (tw.start + dt.timedelta(days=7)).strftime("%Y-%m-%d"), "data_interval_start": tw.start.isoformat(), "data_interval_end": tw.end.isoformat()}


class RunParams(dg.Config):
    include_roster: bool = True                                         # params.include_roster, default from the DAG


@dg.asset
def parametrised(config: RunParams) -> bool:
    return config.include_roster


class PartnerApi(dg.ConfigurableResource):
    base_url: str                                                       # the Airflow connection's host
    token: str                                                          # its password: always from the environment

    def describe(self) -> str:
        return self.base_url


@dg.asset
def uses_connection(partner_api: PartnerApi) -> str:
    return partner_api.describe()


resources = {"partner_api": PartnerApi(base_url=dg.EnvVar("PARTNER_API_BASE_URL"), token=dg.EnvVar("PARTNER_API_PASSWORD"))}
defs = dg.Definitions(assets=[interval_asset, parametrised, uses_connection], resources=resources)
