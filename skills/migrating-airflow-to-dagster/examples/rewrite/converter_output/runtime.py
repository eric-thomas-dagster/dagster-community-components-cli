"""Helpers used by the converted DAGs. Each mirrors what the Airflow operator or hook does."""
from __future__ import annotations

import os
import subprocess
from typing import Any, Optional

import dagster as dg


def run_shell(context: dg.AssetExecutionContext, command: str, env: Optional[dict[str, Any]] = None,
              cwd: Optional[str] = None, timeout: Optional[int] = None, xcom_json: bool = False) -> Any:
    """Run a BashOperator command with `bash -c`, as Airflow does. Returns the last line written to stdout, which is what
    Airflow pushes as the task's XCom (None when the command wrote nothing)."""
    full_env = {**os.environ, **{k: str(v) for k, v in (env or {}).items()}}
    context.log.info(f"$ {command}")
    try:
        proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env=full_env, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as e:           # Airflow raises AirflowTaskTimeout: the task fails
        raise dg.Failure(description=f"execution_timeout of {timeout} s exceeded", metadata={"command": command}) from e
    if proc.stdout:
        context.log.info(proc.stdout)
    if proc.returncode != 0:
        raise dg.Failure(description=f"command exited {proc.returncode}", metadata={"stderr": proc.stderr[-4000:], "command": command})
    lines = proc.stdout.splitlines() if proc.stdout else []
    last = lines[-1].rstrip() if lines else None
    if xcom_json and last is not None:                 # Airflow 3 stores the output as JSON when it parses as JSON (1 -> 1, '{"a": 1}' -> a dict)
        try:
            return json.loads(last)
        except ValueError:
            pass
    return last


import contextvars
import datetime as _dt
import inspect
import json
import types

_MISSING = object()
_AF_CTX: contextvars.ContextVar = contextvars.ContextVar("airflow_context", default=None)


class _TaskInstance:
    """The part of Airflow's TaskInstance converted callables and templates use. Anything else raises."""

    def __init__(self, dag_id, task_id, run_id, try_number, upstream, context=None, keys=None):
        self.dag_id, self.task_id, self.run_id, self.try_number, self._up = dag_id, task_id, run_id, try_number, upstream
        self._ctx, self._keys = context, keys or {}

    def xcom_pull(self, task_ids=None, dag_id=None, key="return_value", include_prior_dates=False, map_indexes=None, default=None):
        if dag_id not in (None, self.dag_id) or include_prior_dates or map_indexes is not None or task_ids is None:
            raise NotImplementedError("xcom_pull is mirrored only for named tasks in this DAG, in this run")
        ids = [task_ids] if isinstance(task_ids, str) else list(task_ids)
        if key != "return_value":                            # a value another task pushed under a key: read from its materialization metadata
            vals = [self._pull_key(i, key, default) for i in ids]
            return vals[0] if isinstance(task_ids, str) else vals
        vals = []
        for i in ids:
            if i not in self._up:          # Airflow returns None for a task id it does not know (often a TaskGroup prefix left out)
                self._warn(i)
            vals.append(self._up.get(i, default))
        return vals[0] if isinstance(task_ids, str) else vals

    def _warn(self, task_id):
        if getattr(self._ctx, "log", None) is not None:
            self._ctx.log.warning(f"xcom_pull of {task_id!r}: no such upstream task in this DAG run, so None was returned (as Airflow does). "
                                  "If the task is in a task group, its id starts with the group id.")

    def _pull_key(self, task_id, key, default):
        asset_key = self._keys.get(task_id)
        if asset_key is None or self._ctx is None:
            self._warn(task_id)
            return default
        records = self._ctx.instance.get_event_records(
            dg.EventRecordsFilter(event_type=dg.DagsterEventType.ASSET_MATERIALIZATION, asset_key=dg.AssetKey(asset_key)), limit=50, ascending=False)
        for r in records:
            if r.event_log_entry.run_id == self.run_id and key in r.asset_materialization.metadata:
                return r.asset_materialization.metadata[key].value
        return default

    def xcom_push(self, key, value=None, **_):
        """A value pushed under a key becomes metadata on this asset's materialization (a return value is the asset's value)."""
        if key == "return_value":
            raise NotImplementedError("xcom_push of return_value: return the value instead")
        try:
            json.dumps(value)
        except TypeError as e:
            raise dg.Failure(description=f"xcom_push of {key!r}: the value is not JSON-serializable ({e})") from e
        meta = dg.MetadataValue.json(value) if isinstance(value, (dict, list)) else value
        self._ctx.add_output_metadata({key: meta})


AF_ISO_DT = False          # set when the runtime is written: Airflow before 2.8 prints datetimes in templates with a T (2026-07-02T05:00:00+00:00)


class _IsoDatetime(_dt.datetime):
    def __str__(self):
        return self.isoformat()


def _iso(x):
    return _IsoDatetime(x.year, x.month, x.day, x.hour, x.minute, x.second, x.microsecond, x.tzinfo) if AF_ISO_DT and isinstance(x, _dt.datetime) else x


def af_context(context, dag_id, task_id, params, upstream, keys=None):
    """Rebuild the Airflow task context from the Dagster run. The logical date comes from run tags that airflow_schedule sets;
    a run without them (a manual run) uses the current time, as Airflow does for a manual trigger. ds is the UTC date of the
    logical date."""
    run = getattr(context, "run", None)
    tags = dict(getattr(run, "tags", None) or {})
    utc = _dt.timezone.utc
    ld = _dt.datetime.fromisoformat(tags["airflow/logical_date"]) if tags.get("airflow/logical_date") else _dt.datetime.now(utc)
    de = _dt.datetime.fromisoformat(tags["airflow/data_interval_end"]) if tags.get("airflow/data_interval_end") else ld
    ld, de = ld.astimezone(utc), de.astimezone(utc)
    ds = ld.strftime("%Y-%m-%d")
    nxt = _dt.datetime.fromisoformat(tags["airflow/next_logical_date"]).astimezone(utc) if tags.get("airflow/next_logical_date") else (de if de != ld else ld)
    prv = _dt.datetime.fromisoformat(tags["airflow/prev_logical_date"]).astimezone(utc) if tags.get("airflow/prev_logical_date") else None
    one = _dt.timedelta(days=1)
    conf = json.loads(tags.get("airflow/conf", "{}"))
    run_id = run.run_id if run is not None else context.run_id        # context.run_id is deprecated in current Dagster
    ti = _TaskInstance(dag_id, task_id, run_id, getattr(context, "retry_number", 0) + 1, upstream, context, keys)
    pkey = pdate = None                                  # the Dagster partition of this run (the key is Dagster's format, the date the window start)
    try:
        if context.has_partition_key:
            pkey = context.partition_key
            pdate = context.partition_time_window.start
    except Exception:                                    # noqa: BLE001
        pass
    if pkey is None:
        pkey = tags.get("airflow/partition_key")           # a run launched for a partition key the producer records itself
    return {"ds": ds, "ds_nodash": ds.replace("-", ""), "ts": ld.isoformat(), "ts_nodash": ld.strftime("%Y%m%dT%H%M%S"),
            "logical_date": _iso(ld), "execution_date": _iso(ld), "data_interval_start": _iso(ld), "data_interval_end": _iso(de),
            "run_id": run_id, "params": dict(params or {}), "conf": conf,
            "next_execution_date": _iso(nxt), "next_ds": nxt.strftime("%Y-%m-%d"), "next_ds_nodash": nxt.strftime("%Y%m%d"),
            "prev_execution_date": _iso(prv), "prev_ds": prv.strftime("%Y-%m-%d") if prv else None, "prev_ds_nodash": prv.strftime("%Y%m%d") if prv else None,
            "tomorrow_ds": (ld + one).strftime("%Y-%m-%d"), "tomorrow_ds_nodash": (ld + one).strftime("%Y%m%d"),
            "yesterday_ds": (ld - one).strftime("%Y-%m-%d"), "yesterday_ds_nodash": (ld - one).strftime("%Y%m%d"),
            "ts_nodash_with_tz": ld.strftime("%Y%m%dT%H%M%S%z"), "task_instance_key_str": f"{dag_id}__{task_id}__{ld.strftime('%Y%m%d')}", "prev_data_interval_start_success": None, "prev_data_interval_end_success": None,
            "dag": types.SimpleNamespace(dag_id=dag_id), "task": types.SimpleNamespace(task_id=task_id), "ti": ti, "task_instance": ti,
            "dag_run": types.SimpleNamespace(run_id=run_id, conf=conf, logical_date=ld, execution_date=ld, dag_id=dag_id, partition_key=pkey, partition_date=pdate),
            "partition_key": pkey, "outlet_events": _OutletEvents(context), "inlet_events": _InletEvents(context)}


def call_with_context(fn, args, kwargs, ctx):
    """PythonOperator's rule: context is merged under op_kwargs; a **kwargs callable gets everything, otherwise only the
    parameters it names (and that op_args did not already fill)."""
    sig = inspect.signature(fn)
    merged = {**ctx, **kwargs}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        call_kwargs = merged
    else:
        bound = set(sig.bind_partial(*args).arguments)
        call_kwargs = {k: merged[k] for k, p in sig.parameters.items()
                       if k in merged and k not in bound and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    token = _AF_CTX.set(ctx)
    try:
        result = fn(*args, **call_kwargs)
        if inspect.isgenerator(result):                      # a task that yields Metadata(asset, extra) and may return a value
            value = None
            try:
                while True:
                    item = next(result)
                    if isinstance(item, Metadata) and "outlet_events" in ctx:
                        ctx["outlet_events"][item.asset].extra.update(item.extra)
            except StopIteration as stop:
                value = stop.value
            result = value
        if "outlet_events" in ctx:
            ctx["outlet_events"].flush()
        return result
    finally:
        _AF_CTX.reset(token)


def get_current_context():
    ctx = _AF_CTX.get()
    if ctx is None:
        raise RuntimeError("get_current_context() called outside a converted task")
    return ctx


class Variable:
    """Airflow Variables from the environment (AIRFLOW_VAR_<KEY>), which is what Airflow's own environment backend reads."""

    @classmethod
    def get(cls, key, default_var=_MISSING, deserialize_json=False, default=_MISSING):
        v = os.environ.get("AIRFLOW_VAR_" + key.upper())
        if v is None:
            d = default if default is not _MISSING else default_var
            if d is _MISSING:
                raise KeyError(f"Variable {key} does not exist")
            return d
        return json.loads(v) if deserialize_json else v

    @classmethod
    def set(cls, *a, **k):
        raise NotImplementedError("Variable.set is not mirrored")


def _ds_add(ds, days):
    return (_dt.datetime.strptime(ds, "%Y-%m-%d") + _dt.timedelta(days=days)).strftime("%Y-%m-%d")


def _ds_format(ds, input_format, output_format):
    return _dt.datetime.strptime(str(ds), input_format).strftime(output_format)


def render(ctx, text):
    """Render an Airflow-style Jinja template: sandboxed, strict about undefined names, with the ds/ts filters and ds macros."""
    import jinja2
    from jinja2.sandbox import SandboxedEnvironment
    env = SandboxedEnvironment(undefined=jinja2.StrictUndefined, extensions=["jinja2.ext.do"], cache_size=0)
    env.filters.update({"ds": lambda v: v.strftime("%Y-%m-%d"), "ds_nodash": lambda v: v.strftime("%Y%m%d"),
                        "ts": lambda v: v.isoformat(), "ts_nodash": lambda v: v.strftime("%Y%m%dT%H%M%S")})
    macros = types.SimpleNamespace(ds_add=_ds_add, ds_format=_ds_format, datetime=_dt.datetime, timedelta=_dt.timedelta)
    return env.from_string(text).render(**ctx, macros=macros)


def airflow_schedule(name, job, cron, tz, semantics):
    """A cron schedule whose runs carry the Airflow logical date as tags. semantics: "trigger" (Airflow 3: logical date = the tick)
    or "data_interval" (Airflow 2: the run covers the interval that ended at the tick, so the logical date is the previous tick)."""
    if semantics == "data_interval":
        try:
            from croniter import croniter       # not a Dagster dependency: the project must install it (converted_requirements.txt lists it)
        except ImportError as e:
            raise ImportError("Airflow 2 data-interval schedules need the `croniter` package") from e

    try:
        from croniter import croniter
    except ImportError:
        croniter = None                         # only the next/prev macros need it outside data-interval schedules

    def execution_fn(ctx):
        tick = ctx.scheduled_execution_time
        logical = croniter(cron, tick).get_prev(_dt.datetime) if semantics == "data_interval" else tick
        utc = _dt.timezone.utc
        tags = {"airflow/logical_date": logical.astimezone(utc).isoformat(), "airflow/data_interval_end": tick.astimezone(utc).isoformat()}
        if croniter is not None:                # the schedule's previous and next logical dates (the prev_ds / next_ds macros)
            tags["airflow/prev_logical_date"] = croniter(cron, logical).get_prev(_dt.datetime).astimezone(utc).isoformat()
            tags["airflow/next_logical_date"] = (tick if semantics == "data_interval" else croniter(cron, tick).get_next(_dt.datetime)).astimezone(utc).isoformat()
        return dg.RunRequest(tags=tags)

    return dg.ScheduleDefinition(name=name, job=job, cron_schedule=cron, execution_timezone=tz, execution_fn=execution_fn)


def copy_into_sql(*, table, stage, file_format, schema=None, prefix=None, files=None, pattern=None, copy_options=None, validation_mode=None):
    """The COPY INTO statement exactly as Airflow's CopyFromExternalStageToSnowflakeOperator builds it."""
    enclose = lambda p: "'" + p.replace("'", "''") + "'"
    into = f"{schema}.{table}" if schema else table
    return f"""
        COPY INTO {into}
             FROM  @{stage}/{prefix or ""}
        {"FILES=(" + ",".join(map(enclose, files)) + ")" if files else ""}
        {"PATTERN=" + enclose(pattern) if pattern else ""}
        FILE_FORMAT={file_format}
        {copy_options or ""}
        {validation_mode or ""}
        """


def snowflake_run(context, conn_id, overrides, statements, split=False):
    """Run SQL on the Snowflake connection `conn_id` (a SnowflakeResource attaches a SnowflakeConnection).

    The operator's warehouse/database/role/schema override the connection's: they become USE statements for the session.
    """
    import io

    use = [f"USE {k.upper()} {overrides[k]}" for k in ("role", "warehouse", "database") if overrides.get(k)]
    if overrides.get("schema"):
        use.append(f"USE SCHEMA {overrides['schema']}")
    stmts: list[str] = []
    for text in statements:
        if split:
            from snowflake.connector.util_text import split_statements
            stmts += [s for s, _ in split_statements(io.StringIO(text)) if s.strip()]
        else:
            stmts.append(text)
    with getattr(context.resources, conn_id).get_connection() as conn:
        cur = conn.cursor()
        for stmt in [*use, *stmts]:
            cur.execute(stmt)


class S3Hook:
    """Compatibility shim for the commonly used part of Airflow's S3Hook, so callables that use it can be copied unchanged.

    Calls boto the way the Airflow hook does. It uses the default AWS credential chain; the connection id is recorded only
    (a callable constructs the hook itself, so it cannot reach a Dagster resource). Anything not mirrored raises
    NotImplementedError instead of behaving differently.
    """

    def __init__(self, aws_conn_id: Optional[str] = "aws_default", **kwargs: Any) -> None:
        if kwargs:
            raise NotImplementedError(f"S3Hook arguments not mirrored: {sorted(kwargs)}")
        self.aws_conn_id = aws_conn_id
        self._client: Any = None

    def get_conn(self) -> Any:
        if self._client is None:
            from dagster_aws.s3 import S3Resource
            self._client = S3Resource().get_client()
        return self._client

    @staticmethod
    def _unsupported(**flags: Any) -> None:
        bad = [k for k, v in flags.items() if v]
        if bad:
            raise NotImplementedError(f"S3Hook option(s) not mirrored: {bad}")

    def check_for_key(self, key: str, bucket_name: Optional[str] = None) -> bool:
        bucket_name, key = s3_bucket_key(bucket_name, key)
        return _s3_head(self.get_conn(), bucket_name, key)

    def load_file(self, filename: Any, key: str, bucket_name: Optional[str] = None, replace: bool = False,
                  encrypt: bool = False, gzip: bool = False, acl_policy: Optional[str] = None) -> None:
        self._unsupported(encrypt=encrypt, gzip=gzip, acl_policy=acl_policy)
        s3_upload_file(self.get_conn(), str(filename), *s3_bucket_key(bucket_name, key), replace)

    def load_bytes(self, bytes_data: bytes, key: str, bucket_name: Optional[str] = None, replace: bool = False,
                   encrypt: bool = False, acl_policy: Optional[str] = None) -> None:
        import io
        self._unsupported(encrypt=encrypt, acl_policy=acl_policy)
        bucket_name, key = s3_bucket_key(bucket_name, key)
        if not replace and _s3_head(self.get_conn(), bucket_name, key):
            raise ValueError(f"The key {key} already exists.")
        self.get_conn().upload_fileobj(io.BytesIO(bytes_data), bucket_name, key, ExtraArgs={}, Config=_transfer_config())

    def load_string(self, string_data: str, key: str, bucket_name: Optional[str] = None, replace: bool = False,
                    encrypt: bool = False, encoding: Optional[str] = None, acl_policy: Optional[str] = None,
                    compression: Optional[str] = None) -> None:
        self._unsupported(compression=compression)
        self.load_bytes(string_data.encode(encoding or "utf-8"), key, bucket_name, replace, encrypt, acl_policy)

    def read_key(self, key: str, bucket_name: Optional[str] = None) -> str:
        raise NotImplementedError("S3Hook.read_key uses boto's resource API and is not mirrored")

    def list_keys(self, bucket_name: Optional[str] = None, prefix: str = "", delimiter: str = "", **kwargs: Any) -> list[str]:
        self._unsupported(**kwargs)
        return s3_list_keys(self.get_conn(), bucket_name, prefix, delimiter or None)

    def delete_objects(self, bucket: str, keys: Any) -> None:
        s3_delete_keys(self.get_conn(), bucket, keys)

    def copy_object(self, source_bucket_key: str, dest_bucket_key: str, source_bucket_name: Optional[str] = None,
                    dest_bucket_name: Optional[str] = None, **kwargs: Any) -> Any:
        self._unsupported(**kwargs)
        return s3_copy_object(self.get_conn(), source_bucket_key, dest_bucket_key, source_bucket_name, dest_bucket_name)


def s3_bucket_key(bucket: Optional[str], key: str) -> tuple[str, str]:
    """S3Hook.get_s3_bucket_key: a full s3:// url may carry the bucket."""
    if bucket is None:
        if not key.startswith("s3://"):
            raise ValueError(f"bucket name missing for key {key!r}")
        rest = key[len("s3://"):]
        b, _, k = rest.partition("/")
        return b, k
    return bucket, key


def s3_list_keys(client: Any, bucket: str, prefix: Optional[str] = None, delimiter: Optional[str] = None) -> list[str]:
    """S3Hook.list_keys without wildcard or date filters: the same paginate call, returning the keys."""
    params = {"Bucket": bucket, "Prefix": prefix or "", "Delimiter": delimiter or "",
              "PaginationConfig": {"PageSize": None, "MaxItems": None}, "StartAfter": ""}
    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(**params):
        keys += [o["Key"] for o in page.get("Contents", [])]
    return keys


def _s3_head(client: Any, bucket: str, key: str) -> bool:
    """S3Hook.head_object: True if present, False on HTTP 404, otherwise raise."""
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except client.exceptions.ClientError as e:
        if e.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
            return False
        raise


def _transfer_config() -> Any:
    from boto3.s3.transfer import TransferConfig
    return TransferConfig()


def s3_copy_object(client: Any, source_key: str, dest_key: str, source_bucket: Optional[str] = None,
                   dest_bucket: Optional[str] = None) -> Any:
    """S3Hook.copy_object with the operator defaults (ACL private)."""
    dbucket, dkey = s3_bucket_key(dest_bucket, dest_key)
    sbucket, skey = s3_bucket_key(source_bucket, source_key)
    return client.copy_object(Bucket=dbucket, Key=dkey, CopySource={"Bucket": sbucket, "Key": skey, "VersionId": None}, ACL="private")


def s3_delete_keys(client: Any, bucket: str, keys: Any) -> None:
    """S3DeleteObjectsOperator with explicit keys: delete in chunks of 1000, raise on any error."""
    keys = [keys] if isinstance(keys, str) else list(keys or [])
    for i in range(0, len(keys), 1000):
        resp = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})
        if "Errors" in resp:
            raise dg.Failure(description=f"errors deleting: {[e['Key'] for e in resp['Errors']]}")


def s3_create_object(client: Any, bucket: Any, key: str, data: Any, replace: bool, encoding: Any = None) -> None:
    """S3CreateObjectOperator without compression/encrypt/acl: S3Hook.load_string for text, load_bytes for bytes."""
    b, k = s3_bucket_key(bucket, key)
    if not replace and _s3_head(client, b, k):
        raise ValueError(f"The key {k} already exists.")
    body = data.encode(encoding or "utf-8") if isinstance(data, str) else data
    client.put_object(Bucket=b, Key=k, Body=body)


def s3_upload_file(client: Any, filename: str, bucket: str, key: str, replace: bool) -> None:
    """LocalFilesystemToS3Operator without gzip/encrypt/acl (S3Hook.load_file)."""
    b, k = s3_bucket_key(bucket, key)
    if not replace and _s3_head(client, b, k):
        raise ValueError(f"The key {k} already exists.")
    client.upload_file(str(filename), b, k, ExtraArgs={}, Config=_transfer_config())

def item_key(item):
    """Partition key for one mapped item: a short string is used as it is, a small value is its JSON, anything longer a digest."""
    if isinstance(item, str) and len(item) <= 120 and not item.startswith(("json:", "sha1:")):
        return item
    text = json.dumps(item, sort_keys=True, default=str)
    if len(text) <= 120:
        return "json:" + text
    import hashlib
    return "sha1:" + hashlib.sha1(text.encode()).hexdigest()


def decode_item(key, items):
    """The item a partition key stands for, found again in the list the producer returned."""
    for item in items:
        if item_key(item) == key:
            return item
    raise dg.Failure(description=f"partition {key!r} is not among the items the producer returned (it may have run again with a different list)")


def item_keys(items):
    if not isinstance(items, (list, tuple)):
        raise dg.Failure(description=f"expected a list of items to map over, got {type(items).__name__}")
    return list(dict.fromkeys(item_key(i) for i in items))          # identical items collapse into one partition


def fan_out(context, asset_event, partitions_name):
    """After the producer ran: register each item as a dynamic partition and request one run per item."""
    meta = asset_event.dagster_event.event_specific_data.materialization.metadata.get("airflow/map_keys")
    keys = list(meta.value) if meta is not None else []
    if not keys:
        return dg.SkipReason("the producer returned no items")
    existing = set(context.instance.get_dynamic_partitions(partitions_name))
    new = [k for k in keys if k not in existing]
    if new:
        context.instance.add_dynamic_partitions(partitions_name, new)
    return [dg.RunRequest(run_key=f"{asset_event.run_id}:{k}", partition_key=k) for k in keys]

class Skipped:
    """A task Airflow would have skipped. `hard` skips everything downstream whatever its trigger rule (a short-circuit's default)."""

    def __init__(self, hard=False):
        self.hard = hard

    def __repr__(self):
        return "Skipped"


class Failed:
    """A task that failed (after its retries). The run is failed at the end; steps whose trigger rule allows it still run."""

    def __init__(self, error=""):
        self.error = error

    def __repr__(self):
        return f"Failed({self.error})"


class UpstreamFailed:
    """A task that did not run because an upstream task failed (Airflow's `upstream_failed`)."""

    def __repr__(self):
        return "UpstreamFailed"


class BranchResult:
    """What a branch task chose: the task ids (or task group ids) to follow."""

    def __init__(self, selected):
        self.selected = [] if selected is None else ([selected] if isinstance(selected, str) else list(selected))

    def selects(self, task_id):
        return any(task_id == s or task_id.startswith(s + ".") for s in self.selected)


class ShortCircuit:
    def __init__(self, passed, hard=True):
        self.passed, self.hard = passed, hard


class AirflowException(Exception):
    """Stands in for Airflow's exception so converted callables that raise it need no Airflow import."""


class AirflowSkipException(AirflowException):
    """Raised by a task to skip itself: in the graph form the step becomes a skipped step."""


class AirflowFailException(AirflowException):
    """Raised by a task to fail without retrying."""


class PokeReturnValue:
    """What a sensor callable may return: done or not, and a value to pass on."""

    def __init__(self, is_done, xcom_value=None):
        self.is_done, self.xcom_value = is_done, xcom_value

    def __bool__(self):
        return bool(self.is_done)


def _skip(context, hard):
    context.add_output_metadata({"airflow/state": "skipped"})
    return Skipped(hard)


af_skip = _skip


def _state(x):
    """An upstream value as an Airflow task state."""
    if isinstance(x, Skipped):
        return "skipped"
    if isinstance(x, Failed) or (isinstance(x, list) and any(isinstance(i, Failed) for i in x)):
        return "failed"
    if isinstance(x, UpstreamFailed) or (isinstance(x, list) and any(isinstance(i, UpstreamFailed) for i in x)):
        return "upstream_failed"
    return "success"


def _upstream_failed(context):
    context.add_output_metadata({"airflow/state": "upstream_failed"})
    return UpstreamFailed()


def af_gate(context, rule, inputs, task_id, setup=None):
    """Airflow's decision for one task from its upstream states (TriggerRuleDep): None to run it, or a marker (Skipped, UpstreamFailed)."""
    if any(isinstance(x, Skipped) and x.hard for x in inputs):
        return _skip(context, True)
    for x in inputs:
        if isinstance(x, BranchResult) and not x.selects(task_id):
            return _skip(context, False)
        if isinstance(x, ShortCircuit) and not x.passed:
            return _skip(context, x.hard)
    states = [_state(x) for x in inputs]
    n = len(states)
    if not n:
        return None
    ok, sk = states.count("success"), states.count("skipped")
    bad = states.count("failed") + states.count("upstream_failed")
    SKIP, UPF = "skip", "upf"
    if rule == "all_success":
        out = UPF if bad else (SKIP if sk else None)
    elif rule == "all_failed":
        out = SKIP if (ok or sk) else None
    elif rule == "one_success":
        out = SKIP if sk == n else (UPF if ok <= 0 else None)
    elif rule == "one_failed":
        out = None if bad else SKIP
    elif rule == "one_done":
        out = None if (states.count("failed") or ok) else SKIP
    elif rule == "none_failed":
        out = UPF if bad else None
    elif rule == "none_failed_min_one_success":
        out = UPF if bad else (SKIP if sk == n else (UPF if ok == 0 else None))
    elif rule == "none_skipped":
        out = SKIP if sk else None
    elif rule == "all_skipped":
        out = SKIP if (ok or bad) else None
    elif rule == "all_done_min_one_success":
        out = SKIP if sk else (UPF if ok == 0 else None)
    elif rule == "all_done_setup_success":                         # a teardown: skip if every setup was skipped, fail if no setup succeeded
        flags = [i for i, f in enumerate(setup or []) if f]
        if flags and sum(states[i] == "skipped" for i in flags) >= len(flags):
            out = SKIP
        elif flags and not any(states[i] == "success" for i in flags):
            out = UPF
        else:
            out = None
    else:                                                          # all_done, always
        out = None
    if out == SKIP:
        return _skip(context, False)
    if out == UPF:
        return _upstream_failed(context)
    return None


def af_guard(context, run, retries, delay):
    """Run a step so that a failure (after its retries) becomes a Failed value that later steps can react to."""
    try:
        return run()
    except dg.RetryRequested:
        raise
    except AirflowSkipException:
        return af_skip(context, False)
    except Exception as e:                                         # noqa: BLE001
        if context.retry_number < retries and not isinstance(e, AirflowFailException):
            raise dg.RetryRequested(max_retries=retries, seconds_to_wait=delay) from e
        context.add_output_metadata({"airflow/state": "failed", "error": str(e)[:1000]})
        return Failed(f"{type(e).__name__}: {str(e)[:300]}")


def af_isolated(fn, requirements=None, python=None, python_version=None, system_site_packages=False, pip_options=None, index_urls=None,
                env_vars=None, inherit_env=True, support=()):
    """PythonVirtualenvOperator / ExternalPythonOperator: run the callable in its own interpreter and return its pickled result.
    With `python` that interpreter is used; otherwise a virtualenv with `requirements` is built once and reused."""
    import hashlib
    import inspect
    import os
    import pickle
    import shutil
    import subprocess
    import sys
    import tempfile
    import textwrap
    NL = chr(10)
    src = textwrap.dedent(inspect.getsource(fn))
    name = fn.__name__

    def interpreter():
        if python:
            return python
        reqs = [requirements] if isinstance(requirements, str) else list(requirements or [])
        key = hashlib.sha256(repr((reqs, python_version, system_site_packages, pip_options, index_urls)).encode()).hexdigest()[:16]
        root = os.path.join(os.environ.get("AF_VENV_CACHE", os.path.join(tempfile.gettempdir(), "af_venvs")), key)
        py = os.path.join(root, "bin", "python")
        if os.path.exists(py):
            return py
        uv = shutil.which("uv")
        if uv:
            cmd = [uv, "venv", root] + (["--python", str(python_version)] if python_version else []) + (["--system-site-packages"] if system_site_packages else [])
        else:
            cmd = [sys.executable, "-m", "venv", root] + (["--system-site-packages"] if system_site_packages else [])
        subprocess.run(cmd, check=True, capture_output=True)
        if reqs:
            opts = (pip_options if isinstance(pip_options, list) else str(pip_options or "").split()) + sum([["--index-url" if i == 0 else "--extra-index-url", u] for i, u in enumerate(index_urls or [])], [])
            cmd = [uv, "pip", "install", "--python", py] + opts + reqs if uv else [py, "-m", "pip", "install"] + opts + reqs
            done = subprocess.run(cmd, capture_output=True, text=True)
            if done.returncode:
                shutil.rmtree(root, ignore_errors=True)
                raise dg.Failure(description="could not build the virtualenv: " + done.stderr[-2000:])
        return py

    def run(*args, **kwargs):
        py = interpreter()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "in.pkl"), "wb") as f:
                pickle.dump((list(args), dict(kwargs)), f)
            script = NL.join([*support, src, "import pickle, sys", "args, kwargs = pickle.load(open(sys.argv[1], 'rb'))",
                              f"result = {name}(*args, **kwargs)", "pickle.dump(result, open(sys.argv[2], 'wb'))", ""])
            with open(os.path.join(d, "run.py"), "w") as f:
                f.write(script)
            env = {**(os.environ if inherit_env else {}), **(env_vars or {})}
            done = subprocess.run([py, os.path.join(d, "run.py"), os.path.join(d, "in.pkl"), os.path.join(d, "out.pkl")], capture_output=True, text=True, env=env)
            if done.stdout:
                print(done.stdout)
            if done.returncode:
                raise dg.Failure(description=f"{name} failed in its own interpreter ({py}): " + done.stderr[-3000:])
            with open(os.path.join(d, "out.pkl"), "rb") as f:
                return pickle.load(f)
    return run


def af_sla_check(context, seconds, key):
    """Airflow's task `sla`: the task should finish within `seconds` of the scheduled time (the schedule tick). A miss is recorded as an observation on the asset;
    the task is not stopped. A run without a scheduler tick (a manual launch) is measured from the run's start."""
    import datetime as _d
    try:
        tag = context.run.tags.get("dagster/scheduled_execution_time")
        if tag:
            start = _d.datetime.fromisoformat(tag)
        else:
            stats = context.instance.get_run_stats(context.run_id)
            start = _d.datetime.fromtimestamp(stats.start_time, _d.timezone.utc) if stats.start_time else None
        if start is None:
            return
        late = (_d.datetime.now(_d.timezone.utc) - (start if start.tzinfo else start.replace(tzinfo=_d.timezone.utc))).total_seconds() - seconds
        if late > 0:
            context.log.warning(f"SLA missed: {key} finished {late:.0f}s after its {seconds:.0f}s deadline")
            context.log_event(dg.AssetObservation(asset_key=dg.AssetKey(key.split("/")), metadata={
                "sla_breach": True, "sla_expected_seconds": seconds, "sla_overrun_seconds": late}))
    except Exception as e:                                         # noqa: BLE001  a monitoring failure must not fail the task
        context.log.warning(f"could not evaluate the SLA of {key}: {e}")


def af_s3_mapped(client, op_type, f):
    """A mapped S3 operator: one instance's arguments (the fixed ones merged with its expanded ones)."""
    if op_type == "S3ListOperator":
        return s3_list_keys(client, f.get("bucket"), f.get("prefix"), f.get("delimiter"))
    if op_type == "S3CopyObjectOperator":
        return s3_copy_object(client, f.get("source_bucket_key"), f.get("dest_bucket_key"), f.get("source_bucket_name"), f.get("dest_bucket_name"))
    if op_type == "S3DeleteObjectsOperator":
        if f.get("keys") is None:
            raise dg.Failure(description="S3DeleteObjectsOperator by prefix or date range is not supported in a mapped task")
        return s3_delete_keys(client, f.get("bucket"), f.get("keys"))
    if op_type == "S3CreateObjectOperator":
        return s3_create_object(client, f.get("s3_bucket"), f.get("s3_key"), f.get("data"), bool(f.get("replace")), f.get("encoding"))
    return s3_upload_file(client, f.get("filename"), f.get("dest_bucket"), f.get("dest_key"), bool(f.get("replace")))


def af_snowflake_mapped(context, op_type, conn, overrides, f):
    """A mapped Snowflake operator: COPY INTO with this instance's arguments, or its SQL."""
    ov = {**overrides, **{k: f[k] for k in ("warehouse", "database", "role", "schema") if f.get(k)}}
    if op_type == "CopyFromExternalStageToSnowflakeOperator":
        kw = {k: f.get(k) for k in ("table", "stage", "file_format", "schema", "prefix", "files", "pattern", "copy_options", "validation_mode")}
        return snowflake_run(context, conn, ov, [copy_into_sql(**kw)])
    sql = f.get("sql")
    return snowflake_run(context, conn, ov, [sql] if isinstance(sql, str) else list(sql or []), split=True)


def af_sync(fn):
    """An async callable run to completion (Airflow's PythonOperator awaits nothing: a coroutine function is run with asyncio)."""
    import asyncio
    import functools

    @functools.wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return run


def af_xcom_key(value, key):
    """A named XCom of a task that returns a dict with multiple_outputs: Airflow pushes each dict key as its own XCom."""
    return value.get(key) if isinstance(value, dict) else None


class LoopDecision:
    """What an `until` callable of a loop returns in Airflow 3.4: whether to stop, and why."""

    def __init__(self, stop, reason=None):
        self.stop, self.reason = bool(stop), reason


class _LoopState:
    def __init__(self, index, max_iterations, previous, result=None):
        self.index, self.max_iterations, self.previous, self.result = index, max_iterations, previous, result


def af_loop(body, until, max_iterations):
    """An Airflow 3.4 task-group loop: run the body (the group's tasks, in order) up to max_iterations times. `loop.index` starts at 1 and `loop.previous` is the
    preceding iteration's result (None on the first). After each iteration `until(loop=...)` decides: a LoopDecision, a bool, or a (stop, reason) pair. Without an
    `until` the body runs exactly max_iterations times; with one, running out of iterations before it says stop fails the step."""
    previous = None
    result = None
    for index in range(1, max_iterations + 1):
        result = body(_LoopState(index, max_iterations, previous))
        if until is not None:
            decision = until(loop=_LoopState(index, max_iterations, previous, result))
            if isinstance(decision, LoopDecision):
                stop = decision.stop
            elif isinstance(decision, bool):
                stop = decision
            elif isinstance(decision, tuple) and len(decision) == 2 and isinstance(decision[0], bool):
                stop = decision[0]
            else:
                raise dg.Failure(description=f"the loop's until callable must return a LoopDecision, a bool or a (stop, reason) pair, not {decision!r}")
            if stop:
                return result
        previous = result
    if until is not None:
        raise dg.Failure(description=f"the loop ran {max_iterations} iterations and its until callable never said stop")
    return result


def af_map_items(fns, items):
    """`.map(f)` on an XCom: each item through the functions in turn; an item whose function raises AirflowSkipException is left out, as Airflow skips that mapped task instance."""
    out = []
    for x in items:
        try:
            for f in fns:
                x = f(x)
        except AirflowSkipException:
            continue
        out.append(x)
    return out


def structural_size(value):
    """The number of items of a sized result (list, dict, string, DataFrame), or None when it has no size: used by the generated structural asset checks."""
    try:
        if hasattr(value, "shape") and hasattr(value, "columns"):
            return len(value)
        return len(value)
    except TypeError:
        return None


def af_caveat(reason, skip=False):
    """A task the converter could not translate. It fails with the reason, or (skip mode) returns nothing so the rest of the DAG runs."""
    if skip:
        return None
    raise dg.Failure(description=f"AIRFLOW-CAVEAT: {reason}")


def af_skippable(context, run):
    """Run a step; a raised AirflowSkipException skips the step (and what depends on it under its trigger rule) instead of failing it."""
    try:
        return run()
    except AirflowSkipException:
        return af_skip(context, False)


def af_finish(context, values, ignore=(), leaves=None):
    """The last step of a failure-aware DAG. Airflow decides a DAG run by its LEAF tasks: the run fails if a leaf failed or was upstream-failed,
    so a failed task whose downstream (a cleanup, an alert) succeeded does not fail the run. A failed teardown does not either, unless it asks to."""
    failed = []
    for name, v in values.items():
        if name in ignore or (leaves is not None and name not in leaves):
            continue
        items = v if isinstance(v, list) else [v]
        failed += [f"{name}: {i.error if isinstance(i, Failed) else 'an upstream task failed'}" for i in items if isinstance(i, (Failed, UpstreamFailed))]
    if failed:
        raise dg.Failure(description="tasks failed: " + "; ".join(failed))


af_datetime, af_time = _dt.datetime, _dt.time          # public names: generated modules import the runtime with *


def _target_times_as_dates(base, lower, upper):
    """Airflow's combination of time-of-day bounds with the date being checked."""
    if isinstance(lower, _dt.datetime) and isinstance(upper, _dt.datetime):
        return lower, upper
    if lower is not None and isinstance(lower, _dt.time):
        lower = _dt.datetime.combine(base, lower)
    if upper is not None and isinstance(upper, _dt.time):
        upper = _dt.datetime.combine(base, upper)
    if lower is None or upper is None:
        return lower, upper
    if upper < lower:
        upper += _dt.timedelta(days=1)
    return lower, upper


def _in_tz(dt, tz):
    return None if dt is None else (dt.replace(tzinfo=tz) if dt.tzinfo is None else dt)


def branch_datetime(ctx, lower, upper, if_true, if_false, tzname):
    """BranchDateTimeOperator: follow if_true when the time (the system's, or the logical date) is inside [lower, upper]."""
    import zoneinfo
    tz = zoneinfo.ZoneInfo(tzname)
    now = ctx["logical_date"] if ctx is not None else _dt.datetime.now(_dt.timezone.utc)
    lower, upper = _target_times_as_dates(now, lower, upper)
    lower, upper = _in_tz(lower, tz), _in_tz(upper, tz)
    if upper is not None and upper < now:
        return if_false
    if lower is not None and lower > now:
        return if_false
    return if_true


_WEEKDAYS = {"MONDAY": 1, "TUESDAY": 2, "WEDNESDAY": 3, "THURSDAY": 4, "FRIDAY": 5, "SATURDAY": 6, "SUNDAY": 7}


def branch_weekday(ctx, week_day, if_true, if_false, tzname):
    """BranchDayOfWeekOperator: follow if_true when today (the system's day in the DAG timezone, or the logical date) is one of week_day."""
    import zoneinfo
    days = {d if isinstance(d, int) else _WEEKDAYS[d.upper()] for d in ([week_day] if isinstance(week_day, (str, int)) else week_day)}   # names, or Airflow's WeekDay numbers (Monday=1)
    now = ctx["logical_date"] if ctx is not None else _dt.datetime.now(_dt.timezone.utc).astimezone(zoneinfo.ZoneInfo(tzname))
    return if_true if now.isoweekday() in days else if_false


af_utcnow = lambda: _dt.datetime.now(_dt.timezone.utc)
af_timedelta = _dt.timedelta


def af_poke(context, check, interval, timeout, soft_fail=False):
    """BaseSensorOperator.execute in `poke` mode: check, give up (or skip, with soft_fail) once the timeout has passed, sleep, repeat."""
    import time
    start = time.monotonic()
    while True:
        result = check()
        if getattr(result, "is_done", result):
            return getattr(result, "xcom_value", None)
        if time.monotonic() - start > timeout:
            if soft_fail:
                return af_skip(context, False)
            raise dg.Failure(description=f"Sensor has timed out after {timeout} s")
        time.sleep(interval)


def bash_sensor(command, env, retry_exit_code):
    """BashSensor.poke: true on exit 0; with retry_exit_code, that code means try again and any other non-zero code fails."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(prefix="airflowtmp") as tmp:
        proc = subprocess.run(["bash", "-c", command], cwd=tmp, env=env, capture_output=True, text=True)
    if proc.returncode == 0:
        return True
    if retry_exit_code is not None:
        if proc.returncode == retry_exit_code:
            return False
        raise dg.Failure(description=f"Command exited with return code {proc.returncode}")
    return False


def file_sensor(filepath, recursive):
    """FileSensor.poke with the default `fs_default` connection (base path /): a file matching the glob, or a directory that holds files."""
    import glob
    import os
    for path in glob.glob(os.path.join("/", filepath), recursive=recursive):
        if os.path.isfile(path):
            return True
        for _, _, files in os.walk(path):
            if files:
                return True
    return False


def time_sensor_target(time_str, tzname):
    """TimeSensor: today's date in the DAG timezone at the given time, as a UTC moment."""
    import zoneinfo
    tz = zoneinfo.ZoneInfo(tzname)
    return _dt.datetime.combine(_dt.datetime.now(tz), _dt.time.fromisoformat(time_str), tz).astimezone(_dt.timezone.utc)


def reconstruct_dag_job(module, job_name):
    """Rebuild a converted DAG's job in a fresh process, for execute_job (the same mechanism as the registry's trigger_job_asset)."""
    import importlib
    mod = importlib.import_module(module)
    assets = [v for v in vars(mod).values() if isinstance(v, dg.AssetsDefinition)]
    return dg.Definitions(assets=assets, jobs=[v for v in vars(mod).values() if getattr(v, "name", None) == job_name and hasattr(v, "selection")]).get_job_def(job_name)


def trigger_dag_run(context, calling_module, dag_id, conf=None, wait=True):
    """TriggerDagRunOperator: launch the other converted DAG's job here. With wait, fail if its run fails; without, start the run in a
    background thread and move on (the registry's trigger_job_asset `wait: false` does the same)."""
    from dagster._core.definitions.reconstruct import build_reconstructable_job
    name = "".join(c if c.isalnum() or c == "_" else "_" for c in dag_id)
    name = f"_{name}" if name[:1].isdigit() else name           # the module and the job of a converted DAG are named like this
    module = calling_module.rsplit(".", 1)[0] + "." + name
    job = build_reconstructable_job(__name__, "reconstruct_dag_job", reconstructable_kwargs={"module": module, "job_name": name})
    tags = {"airflow/triggered_by": context.run.run_id, **({"airflow/conf": json.dumps(conf)} if conf else {})}
    cfg = {"execution": {"config": {"in_process": {}}}} if context.instance.is_ephemeral else None       # an ephemeral instance cannot coordinate subprocesses
    if not wait:
        import threading
        threading.Thread(target=lambda: dg.execute_job(job, instance=context.instance, tags=tags, run_config=cfg, raise_on_error=False), daemon=False).start()
        context.log.info(f"launched {dag_id!r} without waiting")
        return
    result = dg.execute_job(job, instance=context.instance, tags=tags, run_config=cfg)
    if not result.success:
        raise dg.Failure(description=f"the triggered DAG {dag_id!r} failed (run {result.run_id})", metadata={"triggered_run_id": result.run_id})


DATASET_KEYS_MODE = "path"


def asset_key_of(ref):
    """The Dagster asset key of an Airflow asset or dataset name or URI (the converter names assets the same way)."""
    import re as _re
    from urllib.parse import urlparse

    def safe_(p):
        s_ = _re.sub("[^0-9A-Za-z_]", "_", p)
        return f"_{s_}" if s_[:1].isdigit() else s_
    if DATASET_KEYS_MODE == "script-orchestrator":
        cleaned = _re.sub(r"^[a-z]+://", "", ref)
        return [f"airflow_dataset_{_re.sub('_+', '_', _re.sub('[^A-Za-z0-9_]', '_', cleaned)).strip('_')}"]
    u = urlparse(ref)
    parts = [u.scheme, u.netloc, *u.path.strip("/").split("/")] if u.scheme and u.netloc else [ref]
    return [safe_(p) for p in parts if p]


class Asset:
    """An Airflow Asset or Dataset: a name or URI that maps to a Dagster asset key."""

    def __init__(self, name=None, uri=None, *, group=None, extra=None, **kwargs):
        self.name, self.uri, self.extra = name, uri, extra or {}
        self.ref = uri or name

    @property
    def key(self):
        return asset_key_of(self.ref)


Dataset = Asset


class AssetAlias:
    def __init__(self, name):
        self.name, self.ref = name, name


DatasetAlias = AssetAlias


class Metadata:
    """`yield Metadata(asset, {"k": v})` from a task: metadata on that asset's materialization."""

    def __init__(self, asset, extra, alias=None):
        self.asset, self.extra, self.alias = asset, dict(extra or {}), alias


class _OutletEvent:
    def __init__(self):
        self.extra, self.added, self.partitions = {}, [], []

    def add_partitions(self, keys):
        """PartitionedAtRuntime: the partition key(s) of this asset that this run produced."""
        self.partitions += [keys] if isinstance(keys, str) else list(keys)

    def add(self, asset, extra=None):
        self.added.append((asset, dict(extra or {})))


def _meta_value(v):
    try:
        json.dumps(v)
    except TypeError:
        return str(v)
    return dg.MetadataValue.json(v) if isinstance(v, (dict, list)) else v


class _OutletEvents:
    """outlet_events[asset].extra = {...}: becomes metadata on the materialization of that asset."""

    def __init__(self, context):
        self._ctx, self._events = context, {}

    def __getitem__(self, asset):
        return self._events.setdefault(tuple(asset_key_of(getattr(asset, "ref", asset))), _OutletEvent())

    def flush(self):
        try:
            own = self._ctx.asset_key                                  # raises in a multi-asset context
        except Exception:                                              # noqa: BLE001
            own = None
        for key, ev in self._events.items():
            if ev.extra:
                meta = {k: _meta_value(v) for k, v in ev.extra.items()}
                if own is not None and tuple(own.path) == key:
                    self._ctx.add_output_metadata(meta)
                else:
                    self._ctx.log_event(dg.AssetMaterialization(asset_key=dg.AssetKey(list(key)), metadata=meta))
            for pk in dict.fromkeys(ev.partitions):            # duplicate keys collapse to one event, as in Airflow
                af_report_partition(self._ctx, key, pk, {k: _meta_value(v) for k, v in ev.extra.items()})
            for added, extra in ev.added:                      # an alias resolved at run time: the asset it names is materialized
                self._ctx.log_event(dg.AssetMaterialization(asset_key=dg.AssetKey(asset_key_of(added.ref)), metadata={k: _meta_value(v) for k, v in extra.items()}))


def af_report_partition(context, key, partition, metadata=None):
    """Record a partition of a dynamically partitioned asset that a run chose while it ran: register the key, then report the materialization for it."""
    name = "_".join(key)
    if not context.instance.has_dynamic_partition(name, partition):
        context.instance.add_dynamic_partitions(name, [partition])
    context.instance.report_runless_asset_event(dg.AssetMaterialization(asset_key=dg.AssetKey(list(key)), partition=partition, metadata=metadata or None))


def af_runtime_partition_tag(context, key):
    """A run launched for one partition key (the run tag `airflow/partition_key`): that key is recorded for the asset when the step succeeds."""
    tag = context.run.tags.get("airflow/partition_key")
    if tag:
        af_report_partition(context, key, tag)


class _InletEvent:
    def __init__(self, record):
        self.timestamp = record.timestamp
        mat = record.asset_materialization
        self.extra = {k: getattr(v, "value", v) for k, v in (mat.metadata if mat else {}).items()}


class _InletEvents:
    """inlet_events[asset]: the asset's materializations, oldest first, each with the metadata it carried as `.extra`."""

    def __init__(self, context):
        self._ctx = context

    def __getitem__(self, asset):
        records = self._ctx.instance.get_event_records(
            dg.EventRecordsFilter(event_type=dg.DagsterEventType.ASSET_MATERIALIZATION, asset_key=dg.AssetKey(asset_key_of(getattr(asset, "ref", asset)))), limit=200, ascending=False)
        return [_InletEvent(r) for r in reversed(list(records))]

    def for_asset(self, *, name=None, uri=None, alias_name=None):
        return self[uri or name or alias_name]


class BaseOperator:
    """Stands in for Airflow's BaseOperator for a copied operator class: it keeps the arguments, offers `self.log`, and the converted task runs `execute`."""
    template_fields: tuple = ()

    def __init__(self, *args, task_id=None, **kwargs):
        import logging
        self.task_id = task_id
        self.log = logging.getLogger(f"airflow.task.{task_id}")
        for k, v in kwargs.items():                      # retries, owner and the like are stored and not used
            setattr(self, k, v)

    def execute(self, context):
        raise NotImplementedError


def apply_defaults(fn):
    return fn


Context = dict


def _render_deep(ctx, v):
    if isinstance(v, str):
        return render(ctx, v) if "{{" in v or "{%" in v else v
    if isinstance(v, (list, tuple)):
        return type(v)(_render_deep(ctx, x) for x in v)
    if isinstance(v, dict):
        return {k: _render_deep(ctx, x) for k, x in v.items()}
    return v


def run_operator(op, ctx):
    """Render the operator's template fields (as Airflow does before execute) and run it with the rebuilt task context."""
    for f in getattr(op, "template_fields", ()) or ():
        if hasattr(op, f):
            setattr(op, f, _render_deep(ctx, getattr(op, f)))
    return op.execute(ctx)


def af_data(x):
    """The value a downstream task sees: None for a skipped task, the chosen ids for a branch, the flag for a short-circuit."""
    if isinstance(x, (Skipped, Failed, UpstreamFailed)):
        return None
    if isinstance(x, BranchResult):
        return list(x.selected)
    if isinstance(x, ShortCircuit):
        return x.passed
    return x


# ---- provider operators (provider_ops.py) -----------------------------------------------------------------------------------
import re as _re


def _envname(conn_id):
    return _re.sub(r"\W", "_", conn_id).upper()


def _need_env(name, what):
    value = os.environ.get(name)
    if not value:
        raise dg.Failure(description=f"{what}: set the environment variable {name} (the Airflow connection's value is not copied by the conversion)")
    return value


def _http_session(conn_id):
    import requests
    e = _envname(conn_id)
    session = requests.Session()
    if os.environ.get(e + "_LOGIN"):
        session.auth = (os.environ[e + "_LOGIN"], os.environ.get(e + "_PASSWORD", ""))
    if os.environ.get(e + "_AUTH_HEADER"):
        session.headers["Authorization"] = os.environ[e + "_AUTH_HEADER"]
    if os.environ.get(e + "_HEADERS"):
        session.headers.update(json.loads(os.environ[e + "_HEADERS"]))
    return session


def _http_url(conn_id, endpoint):
    """HttpHook.url_from_endpoint: the connection's base URL and the endpoint joined with exactly one slash."""
    base = _need_env(_envname(conn_id) + "_BASE_URL", f"HTTP connection {conn_id}")
    if base and not base.endswith("/") and endpoint and not endpoint.startswith("/"):
        return base + "/" + endpoint
    if base and base.endswith("/") and endpoint and endpoint.startswith("/"):
        return base + endpoint[1:]
    return (base or "") + (endpoint or "")


def _http_send(conn_id, method, endpoint, data, headers, options):
    """HttpHook.run: GET sends the data as query parameters, other methods as the body; HTTP errors fail unless check_response is false."""
    import requests
    session = _http_session(conn_id)
    url = _http_url(conn_id, endpoint)
    method = method.upper()
    if method == "GET":
        req = requests.Request(method, url, params=data, headers=headers)
    elif method == "HEAD":
        req = requests.Request(method, url, headers=headers)
    else:
        req = requests.Request(method, url, data=data, headers=headers)
    prepped = session.prepare_request(req)
    settings = session.merge_environment_settings(prepped.url, proxies={}, stream=None, verify=options.get("verify"), cert=None)
    response = session.send(prepped, timeout=options.get("timeout"), allow_redirects=options.get("allow_redirects", True), **settings)
    if options.get("check_response", True):
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            raise dg.Failure(description=f"{response.status_code}:{response.reason}", metadata={"url": url, "body": response.text[:2000]})
    return response


def http_run(context, conn_id, method, endpoint, data=None, headers=None, options=None, log_response=False):
    """HttpOperator.execute: the response body is the task's value."""
    response = _http_send(conn_id, method, endpoint, data, headers, options or {})
    if log_response:
        context.log.info(response.text)
    return response.text


def http_poke(conn_id, method, endpoint, params, headers, options, allowed_codes):
    """HttpSensor.poke without a response_check: true once the request succeeds; the allowed error codes (404 by default) mean not yet."""
    try:
        _http_send(conn_id, method, endpoint, params, headers, options or {})
    except dg.Failure as e:
        if str(e.description).startswith(tuple(allowed_codes)):
            return False
        raise
    return True


def _split_sql(text):
    try:
        import sqlparse
    except ImportError:
        raise dg.Failure(description="split_statements=True needs the sqlparse package (pip install sqlparse)")
    return [s for s in sqlparse.split(text) if s.strip()]


def sql_run(context, conn_id, statements, parameters=None, autocommit=False, split=False):
    """DbApiHook.run through a SQLAlchemy URL: every statement gets the parameters; one transaction committed at the end, or each statement committed when
    autocommit is set. Returns the rows (as lists) of the last statement that returned any, which is what the operator pushes."""
    from sqlalchemy import create_engine
    url = _need_env(_envname(conn_id) + "_URL", f"SQL connection {conn_id}")
    stmts = []
    for s in statements:
        stmts += _split_sql(s) if split else [s]
    engine = create_engine(url)
    rows = None
    try:
        with engine.connect() as conn:
            for s in stmts:
                context.log.info(f"Running statement: {s[:500]}")
                cur = conn.exec_driver_sql(s, parameters) if parameters else conn.exec_driver_sql(s)
                if cur.returns_rows:
                    rows = [list(r) for r in cur.fetchall()]
                if autocommit:
                    conn.commit()
            conn.commit()
    finally:
        engine.dispose()
    return rows


def _addresses(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in _re.split(r"[,;]", v) if x.strip()]
    return list(v)


def send_email(context, conn_id, to, subject, html, files, cc, bcc, from_email, mime_subtype, mime_charset, custom_headers):
    """EmailOperator through smtplib (airflow.utils.email.send_email_smtp): one HTML part, files attached by name, Bcc kept out of the headers."""
    import smtplib
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.utils import formatdate
    e = _envname(conn_id)
    host = _need_env(e + "_HOST", f"SMTP connection {conn_id}")
    sender = from_email or _need_env(e + "_FROM", f"SMTP connection {conn_id} (no from_email on the operator)")
    to, cc, bcc = _addresses(to), _addresses(cc), _addresses(bcc)
    msg = MIMEMultipart(mime_subtype)
    msg["Subject"], msg["From"], msg["To"], msg["Date"] = subject, sender, ", ".join(to), formatdate(localtime=True)
    if cc:
        msg["Cc"] = ", ".join(cc)
    for k, v in (custom_headers or {}).items():
        msg[k] = v
    msg.attach(MIMEText(html, "html", mime_charset))
    for path in files or []:
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), Name=os.path.basename(path))
        part["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
        msg.attach(part)
    port = int(os.environ.get(e + "_PORT") or 25)
    on = lambda name: os.environ.get(name, "").lower() in ("1", "true", "yes")
    smtp = (smtplib.SMTP_SSL if on(e + "_SSL") else smtplib.SMTP)(host, port)
    try:
        if on(e + "_STARTTLS"):
            smtp.starttls()
        if os.environ.get(e + "_USER"):
            smtp.login(os.environ[e + "_USER"], os.environ.get(e + "_PASSWORD", ""))
        smtp.sendmail(sender, to + cc + bcc, msg.as_string())
    finally:
        smtp.quit()
    context.log.info(f"Sent an email to {len(to + cc + bcc)} recipient(s)")


def slack_webhook_post(context, conn_id, payload, timeout=None):
    """SlackWebhookOperator: POST the message as JSON to the webhook URL; any other status than 200 fails."""
    import requests
    url = _need_env(_envname(conn_id) + "_WEBHOOK_URL", f"Slack webhook connection {conn_id}")
    resp = requests.post(url, json=payload, timeout=timeout or 30)
    if resp.status_code != 200:
        raise dg.Failure(description=f"Slack webhook returned {resp.status_code}", metadata={"body": resp.text[:500]})


def k8s_pod_run(context, *, kube_context=None, **kw):
    """KubernetesPodOperator through dagster-k8s: run the image as a Kubernetes Job, wait for it, stream its logs into the run log.
    (PipesK8sClient needs a container that speaks the Pipes protocol; a pod operator's image usually does not.)"""
    from dagster_k8s import execute_k8s_job
    kw = {k: v for k, v in kw.items() if v is not None}
    if kube_context is not None:
        kw["_kubeconfig_file_context"] = kube_context
    execute_k8s_job(context=context, **kw)


def dbt_cloud_run(context, resource, job_id, cause, steps_override, schema_override, extra, wait=True, poll_interval=60.0, poll_timeout=None):
    """DbtCloudRunJobOperator through dagster-dbt's client. The resource is built with disable_schedule_on_trigger=False: the operator never touches the job's schedule."""
    client = resource.get_dbt_client()
    body = {k: v for k, v in {"cause": cause or "Triggered via Dagster", "steps_override": steps_override, "schema_override": schema_override}.items() if v is not None}
    body.update(extra or {})
    job_id = int(job_id)
    if not wait:
        return client.run_job(job_id, **body)["id"]
    run = client.run_job(job_id, **body)
    client.poll_run(run["id"], poll_interval=poll_interval, poll_timeout=poll_timeout)
    return run["id"]


def airbyte_sync(context, resource, connection_id, wait=True, poll_interval=3.0, poll_timeout=3600.0):
    """AirbyteTriggerSyncOperator through dagster-airbyte: start the sync, poll until it ends (unless asynchronous); the job id is the value."""
    if not wait:
        return resource.start_sync(connection_id)["job"]["id"]
    return resource.sync_and_poll(connection_id, poll_interval=poll_interval, poll_timeout=poll_timeout).job_details["job"]["id"]


def fivetran_sync(context, resource, connector_id, poll_interval=15.0, poll_timeout=None):
    """FivetranOperator through dagster-fivetran: sync the connector and poll until it ends (the resource sets the connector to manual scheduling, as the operator does)."""
    resource.sync_and_poll(connector_id, poll_interval=poll_interval, poll_timeout=poll_timeout)


def databricks_run(context, resource, path, body, wait=True, poll_interval=30.0):
    """DatabricksRunNowOperator / DatabricksSubmitRunOperator: POST the same JSON body to the Jobs API, then wait for the run to finish; the run id is the value."""
    client = resource.get_client()
    run_id = client.workspace_client.api_client.do("POST", path, body=body)["run_id"]
    context.log.info(f"Databricks run {run_id} started")
    if wait:
        client.wait_for_run_to_complete(logger=context.log, databricks_run_id=run_id, poll_interval_sec=poll_interval, max_wait_time_sec=604800, verbose_logs=True)
    return run_id


# ---- more provider operators (provider_ops_more.py) -----------------------------------------------------------------------------
def slack_api_post(resource, channel, text, username, icon_url, attachments, blocks):
    """chat.postMessage through the SlackResource's client (SlackAPIPostOperator calls the same Web API method)."""
    kw = {k: v for k, v in {"channel": channel, "text": text, "username": username, "icon_url": icon_url, "attachments": attachments, "blocks": blocks}.items() if v is not None}
    resp = resource.get_client().chat_postMessage(**kw)
    if not resp["ok"]:
        raise dg.Failure(description=f"Slack API error: {resp.get('error')}")


def s3_key_exists(client, bucket_key, bucket_name, wildcard):
    """S3KeySensor: True when the key (or any key matching the wildcard) exists; a list of keys must all exist."""
    import fnmatch
    keys = [bucket_key] if isinstance(bucket_key, str) else list(bucket_key)
    for k in keys:
        b, key = s3_bucket_key(bucket_name, k)
        if wildcard:
            prefix = key.split("*")[0].split("?")[0].split("[")[0]
            found = False
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=b, Prefix=prefix):
                if any(fnmatch.fnmatch(o["Key"], key) for o in page.get("Contents", [])):
                    found = True
                    break
            if not found:
                return False
        elif not _s3_head(client, b, key):
            return False
    return True


def s3_list_prefixes(client, bucket, prefix=None, delimiter=None):
    """S3ListPrefixesOperator: the common prefixes under `prefix` (S3Hook.list_prefixes)."""
    out = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix or "", Delimiter=delimiter or ""):
        out += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
    return out


def gcs_upload(resource, src, dst, bucket, mime_type=None):
    """LocalFilesystemToGCSOperator: a file (or every file of a directory) to gs://bucket/dst."""
    import os
    client = resource.get_client()
    b = client.bucket(bucket)
    if os.path.isdir(src):
        for root, _, files in os.walk(src):
            for f in files:
                p = os.path.join(root, f)
                b.blob((dst or "") + os.path.relpath(p, src)).upload_from_filename(p, content_type=mime_type)
    else:
        b.blob(dst or os.path.basename(src)).upload_from_filename(src, content_type=mime_type)


def gcs_download(resource, bucket, object_name, filename):
    resource.get_client().bucket(bucket).blob(object_name).download_to_filename(filename)
    return filename


def gcs_list(resource, bucket, prefix=None, delimiter=None, match_glob=None):
    kw = {k: v for k, v in {"prefix": prefix, "delimiter": delimiter, "match_glob": match_glob}.items() if v}
    return [b.name for b in resource.get_client().list_blobs(bucket, **kw)]


def gcs_delete(resource, bucket, objects=None, prefix=None):
    client = resource.get_client()
    names = [objects] if isinstance(objects, str) else list(objects) if objects else [b.name for b in client.list_blobs(bucket, prefix=prefix)]
    for n in names:
        client.bucket(bucket).blob(n).delete()


def gcs_create_bucket(resource, bucket, location="US", project=None):
    """GCSCreateBucketOperator (name and location only): create the bucket; an existing bucket of that name is left as it is (the hook's `create_bucket` logs and continues on a conflict)."""
    client = resource.get_client()
    try:
        client.create_bucket(bucket, location=location, project=project)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ != "Conflict":
            raise


def gcs_delete_bucket(resource, bucket, force=True):
    """GCSDeleteBucketOperator: delete the bucket (with its objects when `force`)."""
    resource.get_client().bucket(bucket).delete(force=force)


def s3_create_bucket(client, bucket, region=None):
    """S3CreateBucketOperator: create the bucket unless it exists (S3Hook.create_bucket checks first); outside us-east-1 the location constraint is the region."""
    try:
        client.head_bucket(Bucket=bucket)
        return
    except Exception:  # noqa: BLE001
        pass
    kw = {"CreateBucketConfiguration": {"LocationConstraint": region}} if region and region != "us-east-1" else {}
    client.create_bucket(Bucket=bucket, **kw)


def s3_delete_bucket(client, bucket, force=False):
    """S3DeleteBucketOperator: delete the bucket; with `force` empty it first (every key, then every version)."""
    if force:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                client.delete_objects(Bucket=bucket, Delete={"Objects": objs})
    client.delete_bucket(Bucket=bucket)


def gcs_exists(resource, bucket, obj):
    return resource.get_client().bucket(bucket).blob(obj).exists()


def ssh_run(resource, command, timeout=None):
    """SSHOperator: run the command, fail on a non-zero exit status, the value is stdout."""
    with resource.get_connection() as client:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out, err = stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")
        status = stdout.channel.recv_exit_status()
    if status != 0:
        raise dg.Failure(description=f"SSH command exited with status {status}", metadata={"stderr": err[-4000:]})
    return out


def docker_run(image, command, environment, docker_url, network_mode, user, working_dir, tty, entrypoint, remove, force_pull):
    """DockerOperator: run the image to completion, stream its logs, fail on a non-zero exit code; the value is the last log line (Airflow pushes it)."""
    import docker
    client = docker.DockerClient(base_url=docker_url)
    if force_pull:
        client.images.pull(image)
    c = client.containers.run(image, command, environment=environment or None, network_mode=network_mode, user=user, working_dir=working_dir, tty=tty, entrypoint=entrypoint, detach=True)
    try:
        res = c.wait()
        logs = c.logs().decode(errors="replace") if hasattr(c.logs(), "decode") else str(c.logs())
    finally:
        if remove:
            c.remove(force=True)
    code = res.get("StatusCode", 0) if isinstance(res, dict) else int(res)
    if code != 0:
        raise dg.Failure(description=f"docker container exited with code {code}", metadata={"logs": logs[-4000:]})
    lines = logs.strip().splitlines()
    return lines[-1] if lines else None


def lambda_invoke(function_name, payload, invocation_type="RequestResponse", qualifier=None, log_type=None, region=None):
    """LambdaInvokeFunctionOperator: boto3 `lambda.invoke`; a function error raises; the value is the response payload (JSON when it parses)."""
    import json as _json
    import boto3
    kw = {"FunctionName": function_name, "InvocationType": invocation_type}
    if payload is not None:
        kw["Payload"] = payload if isinstance(payload, (str, bytes)) else _json.dumps(payload)
    if qualifier:
        kw["Qualifier"] = qualifier
    if log_type:
        kw["LogType"] = log_type
    resp = boto3.client("lambda", region_name=region).invoke(**kw)
    body = resp["Payload"].read() if hasattr(resp.get("Payload"), "read") else resp.get("Payload")
    text = body.decode() if isinstance(body, bytes) else body
    if resp.get("FunctionError"):
        raise dg.Failure(description=f"Lambda function error: {resp['FunctionError']}", metadata={"payload": (text or "")[:4000]})
    try:
        return _json.loads(text) if text else None
    except ValueError:
        return text


def bigquery_query(resource, sql, use_legacy_sql=True, priority=None, location=None, project=None):
    """BigQueryInsertJobOperator with a query configuration: run the query and wait; the value is the job id."""
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(use_legacy_sql=use_legacy_sql, **({"priority": priority} if priority else {}))
    with resource.get_client() as client:
        job = client.query(sql, job_config=cfg, location=location, project=project)
        job.result()
        return job.job_id


def spark_submit(conn_env, application, args, opts, env_vars, binary="spark-submit"):
    """SparkSubmitOperator: build the spark-submit command line as the Spark hook does and run it; the exit code decides."""
    import os
    import subprocess
    cmd = [binary, "--master", os.environ.get(conn_env + "_MASTER", "yarn")]
    dm = opts.get("deploy_mode") or os.environ.get(conn_env + "_DEPLOY_MODE")
    for k, v in (opts.get("conf") or {}).items():
        cmd += ["--conf", f"{k}={v}"]
    flags = {"jars": "--jars", "packages": "--packages", "exclude_packages": "--exclude-packages", "repositories": "--repositories", "py_files": "--py-files", "files": "--files", "archives": "--archives",
             "driver_class_path": "--driver-class-path", "total_executor_cores": "--total-executor-cores", "executor_cores": "--executor-cores", "executor_memory": "--executor-memory",
             "driver_memory": "--driver-memory", "num_executors": "--num-executors", "queue": "--queue", "name": "--name", "java_class": "--class"}
    for k, flag in flags.items():
        if opts.get(k) not in (None, "", [], {}):
            cmd += [flag, str(opts[k])]
    if dm:
        cmd += ["--deploy-mode", dm]
    if opts.get("verbose"):
        cmd.append("--verbose")
    cmd.append(application)
    cmd += [str(x) for x in (args or [])]
    proc = subprocess.run(cmd, env={**os.environ, **{str(k): str(v) for k, v in (env_vars or {}).items()}}, capture_output=True, text=True)
    if proc.returncode != 0:
        raise dg.Failure(description=f"spark-submit exited {proc.returncode}", metadata={"stderr": proc.stderr[-4000:], "command": " ".join(cmd)})
    return proc.stdout[-2000:]

DBT_DIR = 'dbt'                  # where the dbt projects live inside this package (convert_ir.py --dbt-dir)


def run_dbt(context, project, args, profile=None, target=None, profiles_dir=None, global_flags=()):
    """One dbt command, as a Cosmos operator ran it: `dagster_dbt.DbtCliResource` in the project directory, the failure raised."""
    from pathlib import Path
    from dagster_dbt import DbtCliResource
    root = Path(__file__).resolve().parent
    res = DbtCliResource(project_dir=str(root / DBT_DIR / project), profiles_dir=str(root / profiles_dir) if profiles_dir else None, profile=profile, target=target,
                         global_config_flags=list(global_flags))
    inv = res.cli(list(args), raise_on_error=True)
    inv.wait()
    context.log.info("dbt " + " ".join(args) + " finished")
