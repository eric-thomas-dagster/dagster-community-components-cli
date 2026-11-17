"""Per-file S3 load: Airflow dynamic task mapping replaced by Dagster dynamic partitions.

Airflow source: example_04_s3_to_snowflake_dynamic_task_mapping. Each day it lists the landed files, then runs
(mapped, once per file): COPY INTO the table, copy the file to an archive prefix, delete the landed file.

Mapping decisions:
  the file listing plus the kwargs-builder tasks  -> a schedule that lists the day's prefix, registers each file as a
                                                     dynamic partition and requests one run per file
  one mapped task instance per file               -> one partition per file; the partition key is the S3 key
  copy_file_into_snowflake (mapped)               -> the table asset, partitioned by file
  archive_landing_file + delete_landing_file      -> one asset, `archived files`: copy to the archive prefix, then delete
                                                     the landed file (a move). Neither step is a data product by itself.
  the landed files                                -> a shared upstream source asset referenced by the table asset

Behavior that differs from Airflow (also listed in MIGRATION.md): per-file independence instead of all-or-nothing,
and dynamic partitions that accumulate.
"""
import re
from datetime import datetime, timezone as _tz
from typing import Optional
from zoneinfo import ZoneInfo

import dagster as dg

_PRESETS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *"}


def enclose_param(param: str) -> str:
    return f"""'{param.replace("'", "''")}'"""


def copy_into_sql(*, table: str, stage: str, file_format: str, schema: Optional[str] = None,
                  prefix: Optional[str] = None, files: Optional[list[str]] = None, pattern: Optional[str] = None,
                  copy_options: Optional[str] = None, validation_mode: Optional[str] = None) -> str:
    """The COPY INTO statement, built exactly as Airflow's CopyFromExternalStageToSnowflakeOperator builds it."""
    into = f"{schema}.{table}" if schema else table
    return f"""
        COPY INTO {into}
             FROM  @{stage}/{prefix or ""}
        {"FILES=(" + ",".join(map(enclose_param, files)) + ")" if files else ""}
        {"PATTERN=" + enclose_param(pattern) if pattern else ""}
        FILE_FORMAT={file_format}
        {copy_options or ""}
        {validation_mode or ""}
        """


def landed_files(keys: list[str]) -> list[str]:
    """Airflow's copy_file_batches filter: sorted keys, folder placeholders (ending in /) removed."""
    return sorted(key for key in keys or [] if not key.endswith("/"))


def logical_ds(tick: datetime, tz: str) -> str:
    """Airflow's `ds` for a run at `tick` with a trigger timetable: the UTC date of the logical date (= the tick)."""
    return tick.astimezone(_tz.utc).strftime("%Y-%m-%d")


class S3PerFileLoad(dg.Component, dg.Model, dg.Resolvable):
    """Process each file landed under a daily prefix independently: COPY into Snowflake, then archive it."""

    name: str                                   # the Airflow dag_id
    stage: str
    table: str
    file_format: str
    schedule: str                               # cron or @daily, as in the Airflow DAG
    timezone: str = "UTC"
    s3_bucket: str = "example-landing-bucket"
    landing_prefix: str = "orders/"             # files land under <landing_prefix><yyyymmdd>/
    archive_prefix: str = "processed/orders/"
    snowflake_schema: Optional[str] = None
    database: Optional[str] = None
    warehouse: Optional[str] = None
    role: Optional[str] = None
    copy_options: Optional[str] = None
    aws_conn_id: str = "aws_default"
    snowflake_conn_id: str = "snowflake_default"
    retries: int = 2

    # -- helpers ---------------------------------------------------------------------------------------------------
    @property
    def cron(self) -> str:
        return _PRESETS.get(self.schedule, self.schedule)

    @property
    def partitions_name(self) -> str:
        return f"{self.name}_files"

    @property
    def table_key(self) -> dg.AssetKey:
        return dg.AssetKey([p.lower() for p in (self.database, self.snowflake_schema, self.table) if p])

    @property
    def source_key(self) -> dg.AssetKey:
        return dg.AssetKey(["s3", self.s3_bucket, *self.landing_prefix.strip("/").split("/")])

    @property
    def archive_asset_key(self) -> dg.AssetKey:
        return dg.AssetKey(["s3", self.s3_bucket, *self.archive_prefix.strip("/").split("/")])

    def archive_key_for(self, key: str) -> str:
        return key.replace(self.landing_prefix, self.archive_prefix, 1)      # as the Airflow archive_kwargs task does

    def day_prefix(self, tick: datetime) -> str:
        return f"{self.landing_prefix}{logical_ds(tick, self.timezone).replace('-', '')}/"

    def _run_sql(self, context: dg.AssetExecutionContext, *statements: str) -> None:
        use = [f"USE {kind} {value}" for kind, value in
               (("ROLE", self.role), ("WAREHOUSE", self.warehouse), ("DATABASE", self.database)) if value]
        if self.snowflake_schema:
            use.append(f"USE SCHEMA {self.snowflake_schema}")
        with getattr(context.resources, self.snowflake_conn_id).get_connection() as conn:   # a SnowflakeConnection
            cur = conn.cursor()
            for stmt in [*use, *statements]:
                cur.execute(stmt)

    # -- definitions -----------------------------------------------------------------------------------------------
    def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
        pdef = dg.DynamicPartitionsDefinition(name=self.partitions_name)
        retry = dg.RetryPolicy(max_retries=self.retries, delay=300)
        me = self

        @dg.asset(key=self.table_key, group_name=self.name, partitions_def=pdef, retry_policy=retry, kinds={"snowflake"},
                  deps=[self.source_key], required_resource_keys={self.snowflake_conn_id})
        def load_file(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
            """copy_file_into_snowflake: COPY one landed file into the table."""
            key = context.partition_key
            sql = copy_into_sql(table=me.table, stage=me.stage, file_format=me.file_format, schema=me.snowflake_schema,
                                files=[key], copy_options=me.copy_options)
            me._run_sql(context, sql)
            return dg.MaterializeResult(metadata={"file": key, "copy_sql": dg.MetadataValue.md(f"```sql{sql}```")})

        @dg.asset(key=self.archive_asset_key, group_name=self.name, partitions_def=pdef, retry_policy=retry, kinds={"s3"},
                  deps=[self.table_key], required_resource_keys={self.aws_conn_id})
        def archive_file(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
            """archive_landing_file + delete_landing_file: copy the file to the archive prefix, then delete the original."""
            key = context.partition_key
            dest = me.archive_key_for(key)
            client = getattr(context.resources, me.aws_conn_id)      # dagster-aws attaches the boto client itself
            client.copy_object(Bucket=me.s3_bucket, Key=dest,
                               CopySource={"Bucket": me.s3_bucket, "Key": key, "VersionId": None}, ACL="private")
            resp = client.delete_objects(Bucket=me.s3_bucket, Delete={"Objects": [{"Key": key}]})
            if "Errors" in resp:                                    # Airflow's hook raises here too
                raise dg.Failure(description=f"errors deleting landed file: {[e['Key'] for e in resp['Errors']]}")
            return dg.MaterializeResult(metadata={"archived_to": dest})

        job = dg.define_asset_job(self.name, selection=[self.table_key, self.archive_asset_key], partitions_def=pdef)

        @dg.schedule(name=f"{self.name}_schedule", job=job, cron_schedule=self.cron, execution_timezone=self.timezone,
                     required_resource_keys={self.aws_conn_id})
        def discover_and_run(context: dg.ScheduleEvaluationContext):
            """list_landing_files + the mapping: register each landed file as a partition and run it."""
            prefix = me.day_prefix(context.scheduled_execution_time)
            client = getattr(context.resources, me.aws_conn_id)
            keys: list[str] = []
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=me.s3_bucket, Prefix=prefix):
                keys += [o["Key"] for o in page.get("Contents", [])]
            files = landed_files(keys)
            if not files:
                return dg.SkipReason(f"no files under {prefix}")
            existing = set(context.instance.get_dynamic_partitions(me.partitions_name))
            new = [f for f in files if f not in existing]
            if new:
                context.instance.add_dynamic_partitions(me.partitions_name, new)
            return [dg.RunRequest(run_key=f"{context.scheduled_execution_time.isoformat()}:{f}", partition_key=f) for f in files]

        return dg.Definitions(assets=[load_file, archive_file], jobs=[job], schedules=[discover_and_run])
