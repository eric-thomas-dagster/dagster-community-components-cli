"""One component replacing a family of Airflow DAGs built from the same S3-to-Snowflake template.

Airflow source: the factory function `build_s3_to_snowflake_dag`, the TaskGroup builder
`s3_to_snowflake_task_group`, the Blueprint `S3ToSnowflake`, and the dag-factory YAML, all of which
produce: S3ListOperator -> build_load_manifest -> log_load_manifest -> CopyFromExternalStageToSnowflakeOperator
(-> optional SQL step). Each DAG becomes one instance of this component in a `defs.yaml`.

Mapping decisions (see MIGRATION.md in the worked example):
  list_landing_files + build_load_manifest + log_load_manifest + copy_into_snowflake
                          -> ONE asset `<database>/<schema>/<table>` (the table it produces). The listing and manifest are
                             steps that produce no data product of their own: the manifest (file count, file names) is
                             recorded as metadata on the materialization.
  the landed files        -> a SHARED upstream source asset `s3/<bucket>/<prefix root>` that every instance references.
                             Many definitions may depend on one asset; only one definition may produce it.
  post_load_transform     -> asset `<name>/post_load_transform`, downstream of the table
  Airflow connections     -> Dagster resources whose keys equal the connection ids (`aws_default`, ...)
  `{{ ds_nodash }}`       -> the partition key: a daily or hourly partition per Airflow logical date
"""
import re
from datetime import datetime, timezone as _tz
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import dagster as dg

_PRESETS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *"}
_TEMPLATE = re.compile(r"\{\{\s*(?:context\.)?(ds_nodash|ds)\s*\}\}")


def logical_date_ds(partition_key: str, tz: str) -> str:
    """Airflow's `ds` for a partition: the UTC date of the start of the partition window (the run's logical date)."""
    zone = ZoneInfo(tz)
    if len(partition_key) > 10:                                       # hourly: YYYY-MM-DD-HH:MM
        start = datetime.strptime(partition_key, "%Y-%m-%d-%H:%M").replace(tzinfo=zone)
    else:                                                             # daily: YYYY-MM-DD
        start = datetime.strptime(partition_key, "%Y-%m-%d").replace(tzinfo=zone)
    return start.astimezone(_tz.utc).strftime("%Y-%m-%d")


def render_template(text: Optional[str], partition_key: Optional[str], tz: str = "UTC") -> Optional[str]:
    """Render the only Airflow templates these DAGs use ({{ ds }}, {{ ds_nodash }}); refuse anything else."""
    if text is None:
        return None
    if "{{" in _TEMPLATE.sub("", text):
        raise ValueError(f"unsupported Airflow template in {text!r}; only {{{{ ds }}}} and {{{{ ds_nodash }}}} are mapped")
    if _TEMPLATE.search(text) and partition_key is None:
        raise ValueError(f"{text!r} uses a date template but this instance has no partitions")
    if partition_key is None:
        return text
    ds = logical_date_ds(partition_key, tz)
    return _TEMPLATE.sub(lambda m: ds.replace("-", "") if m.group(1) == "ds_nodash" else ds, text)


def enclose_param(param: str) -> str:
    return f"""'{param.replace("'", "''")}'"""


def build_load_manifest(keys: Optional[list[str]]) -> dict[str, object]:
    filtered_keys = sorted(key for key in keys or [] if not key.endswith("/"))
    return {"file_count": len(filtered_keys), "files": filtered_keys}


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


class S3ToSnowflakeLoad(dg.Component, dg.Model, dg.Resolvable):
    """Load files from an S3 prefix into a Snowflake table through an external stage."""

    name: str                                   # the Airflow dag_id
    stage: str
    table: str
    file_format: str
    schedule: Optional[str] = None              # cron or @daily/@hourly, as in the Airflow DAG
    partitions: Optional[Literal["daily", "hourly"]] = None
    start_date: str = "2026-07-01"
    airflow_semantics: Literal["trigger", "data_interval"] = "trigger"   # Airflow 3 cron strings: a run at tick T has logical date T; Airflow 2: it covers the interval that just ended
    timezone: str = "UTC"
    s3_bucket: str = "example-landing-bucket"
    prefix: Optional[str] = None
    s3_prefix: Optional[str] = None             # prefix for the S3 listing, when it differs from the COPY prefix
    snowflake_schema: Optional[str] = None
    database: Optional[str] = None
    warehouse: Optional[str] = None
    role: Optional[str] = None
    pattern: Optional[str] = None
    files: Optional[list[str]] = None
    copy_options: Optional[str] = None
    validation_mode: Optional[str] = None
    post_load_sql_file: Optional[str] = None    # path relative to this instance's directory
    aws_conn_id: str = "aws_default"            # becomes the resource key `aws_default`
    snowflake_conn_id: str = "snowflake_default"
    retries: int = 2
    group_name: Optional[str] = None
    source_asset_key: Optional[str] = None      # shared upstream asset for the landed files; default s3/<bucket>/<first prefix segment>
    create_job: bool = True                     # False when a wrapping module defines the DAG-level job (example 3)
    upstream: Optional[list[str]] = None        # asset keys ("a/b") that must run before the listing, e.g. a prepare step

    # -- helpers ---------------------------------------------------------------------------------------------------
    @property
    def cron(self) -> Optional[str]:
        return _PRESETS.get(self.schedule, self.schedule) if self.schedule else None

    @property
    def table_key(self) -> dg.AssetKey:
        parts = [p.lower() for p in (self.database, self.snowflake_schema, self.table) if p]
        return dg.AssetKey(parts)

    def _partitions_def(self):
        off = 1 if self.airflow_semantics == "trigger" else 0     # trigger: the partition starting at the tick exists at the tick
        if self.partitions == "daily":
            return dg.DailyPartitionsDefinition(start_date=self.start_date, timezone=self.timezone, end_offset=off)
        if self.partitions == "hourly":
            return dg.HourlyPartitionsDefinition(start_date=f"{self.start_date}-00:00", timezone=self.timezone, end_offset=off)
        return None

    def tick_partition_key(self, tick: datetime) -> str:
        """The partition a tick runs: the one starting at the tick (Airflow 3), as `build_schedule_from_partitioned_job` does for the previous one."""
        local = tick.astimezone(ZoneInfo(self.timezone))
        return local.strftime("%Y-%m-%d") if self.partitions == "daily" else local.strftime("%Y-%m-%d-%H:00")

    def _run_sql(self, context: dg.AssetExecutionContext, *statements: str) -> None:
        """Run statements in one Snowflake session.

        `context.resources.<conn id>` is what dagster-snowflake attaches for a SnowflakeResource: a SnowflakeConnection.
        Airflow's operator-level warehouse/database/role/schema override the connection's; here they become USE
        statements at the start of the session, which has the same effect.
        """
        use = [f"USE {kind} {value}" for kind, value in
               (("ROLE", self.role), ("WAREHOUSE", self.warehouse), ("DATABASE", self.database)) if value]
        if self.snowflake_schema:
            use.append(f"USE SCHEMA {self.snowflake_schema}")
        with getattr(context.resources, self.snowflake_conn_id).get_connection() as conn:
            cur = conn.cursor()
            for stmt in [*use, *statements]:
                cur.execute(stmt)

    # -- definitions -----------------------------------------------------------------------------------------------
    def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
        group = self.group_name or self.name
        pdef = self._partitions_def()
        retry = dg.RetryPolicy(max_retries=self.retries, delay=300)    # Airflow's default retry_delay is 300 s
        sql_path = (context.path / self.post_load_sql_file) if self.post_load_sql_file else None
        for t in (self.prefix, self.s3_prefix):               # fail at load time, not at 3 a.m.
            render_template(t, "2000-01-01" if pdef else None, self.timezone)
        me = self
        source_key = dg.AssetKey.from_user_string(self.source_asset_key) if self.source_asset_key else \
            dg.AssetKey(["s3", self.s3_bucket, (self.prefix or "").split("/")[0]])

        @dg.asset(key=self.table_key, group_name=group, partitions_def=pdef, retry_policy=retry, kinds={"snowflake", "s3"},
                  deps=[source_key] + [dg.AssetKey.from_user_string(u) for u in self.upstream or []],
                  required_resource_keys={self.aws_conn_id, self.snowflake_conn_id})
        def load_table(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
            """list_landing_files, build_load_manifest, log_load_manifest and copy_into_snowflake, as one asset."""
            pk = context.partition_key if pdef else None
            list_prefix = render_template(me.s3_prefix or me.prefix, pk, me.timezone)
            client = getattr(context.resources, me.aws_conn_id)     # dagster-aws attaches the boto client itself
            keys: list[str] = []
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=me.s3_bucket, Prefix=list_prefix or ""):
                keys += [o["Key"] for o in page.get("Contents", [])]
            manifest = build_load_manifest(keys)
            context.log.info(f"Preparing to load {manifest['file_count']} files into Snowflake")
            sql = copy_into_sql(table=me.table, stage=me.stage, file_format=me.file_format, schema=me.snowflake_schema,
                                prefix=render_template(me.prefix, pk, me.timezone), files=me.files, pattern=me.pattern,
                                copy_options=me.copy_options, validation_mode=me.validation_mode)
            me._run_sql(context, sql)
            return dg.MaterializeResult(metadata={"file_count": manifest["file_count"],
                                                  "files": dg.MetadataValue.json(manifest["files"]),
                                                  "copy_sql": dg.MetadataValue.md(f"```sql{sql}```")})

        assets = [load_table]
        if sql_path is not None:
            @dg.asset(key=dg.AssetKey([self.name, "post_load_transform"]), group_name=group, partitions_def=pdef,
                      retry_policy=retry, kinds={"snowflake"}, deps=[self.table_key],
                      required_resource_keys={self.snowflake_conn_id})
            def post_load_transform(context: dg.AssetExecutionContext) -> None:
                """post_load_transform: the SQL file the Airflow DAG ran after the load."""
                me._run_sql(context, sql_path.read_text())

            assets.append(post_load_transform)

        if not self.create_job:
            return dg.Definitions(assets=assets)
        job = dg.define_asset_job(self.name, selection=[a.key for a in assets], partitions_def=pdef)
        schedules = []
        if self.cron and pdef is not None and self.airflow_semantics == "trigger":
            schedules.append(dg.ScheduleDefinition(
                name=f"{self.name}_schedule", job=job, cron_schedule=self.cron, execution_timezone=self.timezone,
                execution_fn=lambda ctx: dg.RunRequest(partition_key=me.tick_partition_key(ctx.scheduled_execution_time))))
        elif self.cron and pdef is not None:
            schedules.append(dg.build_schedule_from_partitioned_job(job, name=f"{self.name}_schedule"))
        elif self.cron:
            schedules.append(dg.ScheduleDefinition(name=f"{self.name}_schedule", job=job, cron_schedule=self.cron,
                                                   execution_timezone=self.timezone))
        return dg.Definitions(assets=assets, jobs=[job], schedules=schedules)
