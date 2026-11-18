"""Helpers used by the converted DAGs. Each mirrors what the Airflow operator or hook does."""
from __future__ import annotations

import os
import subprocess
from typing import Any, Optional

import dagster as dg


def run_shell(context: dg.AssetExecutionContext, command: str, env: Optional[dict[str, Any]] = None,
              cwd: Optional[str] = None, timeout: Optional[int] = None) -> None:
    """Run a BashOperator command with `bash -c`, as Airflow does."""
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


def s3_upload_file(client: Any, filename: str, bucket: str, key: str, replace: bool) -> None:
    """LocalFilesystemToS3Operator without gzip/encrypt/acl (S3Hook.load_file)."""
    b, k = s3_bucket_key(bucket, key)
    if not replace and _s3_head(client, b, k):
        raise ValueError(f"The key {k} already exists.")
    client.upload_file(str(filename), b, k, ExtraArgs={}, Config=_transfer_config())
