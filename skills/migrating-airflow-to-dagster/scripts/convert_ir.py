#!/usr/bin/env python3
"""Convert the IR from airflow_ir.py into Dagster code, deterministically, for the constructs with a mechanical mapping.

    python3 convert_ir.py ir.json --out-dir <dagster project>/src/<pkg>/defs --report report.json

For each DAG it either writes one module (assets, a job, a schedule or automation conditions) or DEFERS the whole DAG
with every reason, because a partial conversion would silently change the graph. Nothing is stubbed.

Mapped today
  tasks      PythonOperator / @task (callable source copied with the imports, constants and helpers it needs),
             BashOperator, @task.bash, EmptyOperator (elided, edges rewired); callables using S3Hook (a mirrored subset),
             S3ListOperator, S3CopyObjectOperator, S3DeleteObjectsOperator (explicit keys), LocalFilesystemToS3Operator,
             CopyFromExternalStageToSnowflakeOperator, SnowflakeOperator / SQLExecuteQueryOperator on a Snowflake connection
  data       a value returned by one task and passed to another becomes an asset parameter (the IO manager carries it)
  lineage    a task with one outlet is the asset with that key; inlets become dependencies
  schedule   none, @once, cron (trigger or data-interval timetable) with timezone; asset triggers: a single or any-of
             asset (`eager()`) or all-of several (a per-asset `since_last_handled` condition); retries, retry delay, pool
  structure  task groups become key prefixes; one asset per task, one job per DAG
Deferred with the reason: Jinja templates, task context (ds, ti, **context), other operators, dynamic mapping, trigger
rules other than all_success, setup/teardown, callbacks, other timetables (delta, partitioned assets),
and anything whose callable cannot be copied faithfully.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import json
import re
import textwrap
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

CONTEXT_PARAMS = {"ds", "ds_nodash", "ts", "ti", "task_instance", "dag_run", "logical_date", "data_interval_start",
                  "data_interval_end", "context", "execution_date", "params", "prev_ds", "next_ds", "run_id", "conf", "dag", "task"}
# Airflow's task context. SUPPORTED are rebuilt from run tags by the generated runtime; the others defer.
KNOWN_CTX = CONTEXT_PARAMS | {"ts_nodash", "ts_nodash_with_tz", "prev_ds", "prev_ds_nodash", "next_ds", "next_ds_nodash", "prev_execution_date",
                              "next_execution_date", "prev_data_interval_start_success", "prev_data_interval_end_success", "macros", "var", "conn",
                              "inlets", "outlets", "templates_dict", "test_mode", "map_index_template", "triggering_asset_events",
                              "triggering_dataset_events", "inlet_events", "outlet_events", "tomorrow_ds", "yesterday_ds", "expanded_ti_count",
                              "prev_start_date_success", "prev_end_date_success"}
SUPPORTED_CTX = {"ds", "ds_nodash", "ts", "ts_nodash", "logical_date", "execution_date", "data_interval_start", "data_interval_end",
                 "run_id", "params", "dag", "task", "ti", "task_instance", "dag_run", "conf"}
JINJA_VARS = SUPPORTED_CTX | {"macros"}
CTX_ATTRS = {"dag_run": {"run_id", "conf", "logical_date", "execution_date", "dag_id"}, "dag": {"dag_id"}, "task": {"task_id"},
             "ti": {"dag_id", "task_id", "run_id", "try_number", "xcom_pull"}, "task_instance": {"dag_id", "task_id", "run_id", "try_number", "xcom_pull"}}
SUPPORTED_MACROS = {"ds_add", "ds_format", "datetime", "timedelta"}
SHIM_IMPORTS = {("airflow.models", "Variable"), ("airflow.sdk", "Variable"), ("airflow.models.variable", "Variable"),
                ("airflow.operators.python", "get_current_context"), ("airflow.sdk", "get_current_context"),
                ("airflow.providers.amazon.aws.hooks.s3", "S3Hook")}
ELIDED = {"EmptyOperator", "DummyOperator"}
PY_TASKS = {"PythonOperator", "_PythonDecoratedOperator", "DecoratedOperator"}
BASH = {"BashOperator"}
BASH_DEC = {"_BashDecoratedOperator"}
S3_OPS = {"S3ListOperator", "S3CopyObjectOperator", "S3DeleteObjectsOperator", "LocalFilesystemToS3Operator"}
SNOWFLAKE_OPS = {"CopyFromExternalStageToSnowflakeOperator", "SnowflakeOperator", "SQLExecuteQueryOperator"}


class Defer(Exception):
    pass


RETURN_ALL = False        # set by --return-all: keep every Python return value so a parity run can compare them


def safe(name: str) -> str:
    s = re.sub(r"\W", "_", name)
    return f"_{s}" if s[:1].isdigit() else s


def key_from_asset(ref: str) -> list[str]:
    """Airflow asset/dataset name or URI -> Dagster key path (s3://bucket/a/b -> [s3, bucket, a, b])."""
    u = urlparse(ref)
    parts = [u.scheme, u.netloc, *u.path.strip("/").split("/")] if u.scheme and u.netloc else [ref]
    return [safe(p) for p in parts if p]


# --------------------------------------------------------------------------------------------------------------------
# Callable extraction: the function source plus exactly the module-level code it needs
# --------------------------------------------------------------------------------------------------------------------
def _free_names(fn: ast.FunctionDef) -> set[str]:
    local = {a.arg for a in [*fn.args.args, *fn.args.kwonlyargs, *fn.args.posonlyargs]}
    for extra in (fn.args.vararg, fn.args.kwarg):
        if extra:
            local.add(extra.arg)
    used: set[str] = set()
    probe = ast.Module(body=[*fn.body], type_ignores=[])          # decorators are dropped, so they are not dependencies
    for n in ast.walk(probe):
        if isinstance(n, ast.Name):
            (used if isinstance(n.ctx, ast.Load) else local).add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            local.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                local.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.arg):
            local.add(n.arg)
    for d in [*fn.args.defaults, *[x for x in fn.args.kw_defaults if x is not None]]:   # default values run in the enclosing scope
        for n in ast.walk(d):
            if isinstance(n, ast.Name):
                used.add(n.id)
    return used - local - set(dir(builtins))


class ModuleIndex:
    def __init__(self, path: str) -> None:
        self.src = Path(path).read_text()
        self.tree = ast.parse(self.src)
        self.defs: dict[str, tuple[str, ast.AST]] = {}
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.defs[(a.asname or a.name).split(".")[0]] = ("import", node)
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    self.defs[a.asname or a.name] = ("import", node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs[node.name] = ("def", node)
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self.defs[node.targets[0].id] = ("assign", node)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                self.defs[node.target.id] = ("assign", node)

    def seg(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.src, node) or ""


def _loads(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)} - set(dir(builtins))


def resolve_callable(info: dict[str, Any] | None, shims: set[str] | None = None, meta: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    """(function source without decorators, support blocks in source order). Raises Defer with the reason.

    `shims` collects compatibility shims the code relies on (names provided by the runtime module); `meta` receives how the
    callable uses the Airflow task context: needs_ctx (context names, **kwargs or get_current_context) and needs_ti (ti/xcom_pull)."""
    shims = shims if shims is not None else set()
    meta = meta if meta is not None else {}
    if not info or "source" not in info:
        raise Defer(f"callable source unavailable ({(info or {}).get('source_error', 'no callable')})")
    if info.get("name") == "<lambda>" or info["source"].lstrip().startswith("lambda"):
        raise Defer("callable is a lambda")
    try:
        tree = ast.parse(info["source"])
    except SyntaxError as e:
        raise Defer(f"callable source does not parse: {e.msg}") from e
    fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if fn is None:
        raise Defer("callable is not a plain function")
    if isinstance(fn, ast.AsyncFunctionDef):
        raise Defer("async callable")
    named = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]]
    unsupported = sorted(set(named) & (KNOWN_CTX - SUPPORTED_CTX))
    if unsupported:
        raise Defer(f"callable uses Airflow task context names the runtime does not rebuild {unsupported}")
    ctx_named = sorted(set(named) & SUPPORTED_CTX)
    body_text = ast.unparse(ast.Module(body=[*fn.body], type_ignores=[]))
    if re.search(r"\bairflow\b|\bxcom_push\b|\bset_xcom\b", body_text):
        raise Defer("callable uses the Airflow API (xcom_push or a direct airflow reference)")
    for n in ast.walk(fn):                                       # attributes of the context objects the runtime rebuilds
        obj = n.value if isinstance(n, ast.Attribute) else None
        if isinstance(obj, ast.Subscript) and isinstance(obj.slice, ast.Constant):
            obj_name = obj.slice.value
        else:
            obj_name = obj.id if isinstance(obj, ast.Name) else None
        if obj_name in CTX_ATTRS and n.attr not in CTX_ATTRS[obj_name]:
            raise Defer(f"callable uses {obj_name}.{n.attr}, which the runtime's {obj_name} does not provide")
    meta["var_kw"] = fn.args.kwarg.arg if fn.args.kwarg else None
    meta["ctx_named"] = ctx_named
    meta["needs_ti"] = bool({"ti", "task_instance"} & set(ctx_named)) or bool(re.search(r"\bxcom_pull\b|\bti\b|\btask_instance\b", body_text))
    meta["needs_ctx"] = bool(ctx_named) or bool(fn.args.kwarg) or "get_current_context" in body_text
    idx = ModuleIndex(info["file"])
    needed: dict[int, str] = {}
    seen: set[str] = set()

    def need(names: set[str], where: str) -> None:
        for name in sorted(names):
            if name in seen:
                continue
            seen.add(name)
            hit = idx.defs.get(name)
            if hit is None:
                raise Defer(f"unresolved name {name!r} in {where}")
            kind, node = hit
            if kind == "import":
                mod = getattr(node, "module", None) or (node.names[0].name if isinstance(node, ast.Import) else "")
                if isinstance(node, ast.ImportFrom) and all((mod, a.name) in SHIM_IMPORTS for a in node.names):
                    shims.update(a.name for a in node.names)    # provided by the generated runtime module (star-imported)
                    continue
                if mod.split(".")[0] == "airflow":
                    raise Defer(f"callable depends on an Airflow import ({mod})")
                needed[node.lineno] = idx.seg(node)
                continue
            if re.search(r"\bairflow\b", idx.seg(node)):
                raise Defer(f"{name!r} (used by the callable) depends on Airflow")
            needed[node.lineno] = idx.seg(node)
            need(_free_names(node) if isinstance(node, ast.FunctionDef) else _loads(node), name)

    need(_free_names(fn), info["name"])
    lines = textwrap.dedent(info["source"]).splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(r"def\s", l))
    return "\n".join(lines[start:]), [needed[k] for k in sorted(needed)]


# --------------------------------------------------------------------------------------------------------------------
# helpers shared by the per-task handlers
# --------------------------------------------------------------------------------------------------------------------
def third_party(support: dict[str, int]) -> set[str]:
    """Top-level third-party modules imported by the copied support code (the project must install them)."""
    import sys
    mods: set[str] = set()
    for block in support:
        try:
            tree = ast.parse(block)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods.add(n.module.split(".")[0])
    return {m for m in mods if m not in sys.stdlib_module_names}


def tz_name(tz: str | None) -> str:
    if not tz or tz in ("None", "UTC", "Timezone('UTC')"):
        return "UTC"
    m = re.search(r"FixedTimezone\((-?\d+)", tz)
    if m:
        secs = int(m.group(1))
        return "UTC" if secs == 0 else f"Etc/GMT{'-' if secs > 0 else '+'}{abs(secs) // 3600}"
    m = re.search(r"Timezone\('([^']+)'\)", tz)
    return m.group(1) if m else tz


def xcom_refs(x: Any) -> set[str]:
    if isinstance(x, dict):
        return ({x["xcom_from"]} if "xcom_from" in x and x["xcom_from"] else set()) | {r for v in x.values() for r in xcom_refs(v)}
    if isinstance(x, list):
        return {r for v in x for r in xcom_refs(v)}
    return set()


def expr(v: Any, params: dict[str, str], ctx: str | None = None) -> str:
    if isinstance(v, dict):
        if "xcom_from" in v:
            if v.get("key") not in (None, "return_value"):
                raise Defer(f"XCom key {v['key']!r} (only return values are mapped)")
            if v["xcom_from"] not in params:
                raise Defer(f"XCom reference to {v['xcom_from']!r}, which is not an upstream task the converter can wire")
            return params[v["xcom_from"]]
        if "repr" in v:
            raise Defer(f"argument {v['repr'][:60]!r} cannot be serialised")
        if "seconds" in v:
            return repr(v["seconds"])
        return "{" + ", ".join(f"{k!r}: {expr(x, params, ctx)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(expr(x, params, ctx) for x in v) + "]"
    if isinstance(v, str) and ctx and ("{{" in v or "{%" in v):
        return f"render({ctx}, {v!r})"            # only literal templates are rendered, never values passed in from upstream tasks
    return repr(v)


def strings_of(x: Any):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from strings_of(v)
    elif isinstance(x, list):
        for v in x:
            yield from strings_of(v)


def jinja_vars(x: Any) -> tuple[set[str], str | None]:
    """(variables the templates in x use, a problem or None). Parsed with real Jinja, with the Airflow filters registered."""
    import jinja2
    from jinja2 import meta
    env = jinja2.Environment(extensions=["jinja2.ext.do"])
    env.filters.update({k: (lambda v: v) for k in ("ds", "ds_nodash", "ts", "ts_nodash")})
    names: set[str] = set()
    for text in strings_of(x):
        if "{{" not in text and "{%" not in text:
            continue
        try:
            tree = env.parse(text)
            names |= meta.find_undeclared_variables(tree)
            used = {n.attr for n in tree.find_all(jinja2.nodes.Getattr) if isinstance(n.node, jinja2.nodes.Name) and n.node.name == "macros"}
            if used - SUPPORTED_MACROS:
                return names, f"template uses macros {sorted(used - SUPPORTED_MACROS)}, which the runtime does not provide"
        except jinja2.TemplateSyntaxError as e:
            return names, f"template does not parse ({e.message})"
        except jinja2.TemplateAssertionError as e:
            return names, f"template uses an unsupported filter or test ({e.message})"
    bad = names - JINJA_VARS - set(env.globals)
    return names, (f"template uses {sorted(bad)}, which the runtime does not provide" if bad else None)


def has_jinja(x: Any) -> bool:
    if isinstance(x, str):
        return "{{" in x or "{%" in x
    if isinstance(x, dict):
        return any(has_jinja(v) for v in x.values())
    if isinstance(x, list):
        return any(has_jinja(v) for v in x)
    return False


S3_FIELDS = {
    "S3ListOperator": ("bucket", "prefix", "delimiter", "aws_conn_id", "apply_wildcard"),
    "S3CopyObjectOperator": ("source_bucket_key", "dest_bucket_key", "source_bucket_name", "dest_bucket_name", "source_version_id",
                             "acl_policy", "meta_data_directive", "kms_key_id", "kms_encryption_type", "aws_conn_id"),
    "S3DeleteObjectsOperator": ("bucket", "keys", "prefix", "from_datetime", "to_datetime", "aws_conn_id"),
    "LocalFilesystemToS3Operator": ("filename", "dest_key", "dest_bucket", "replace", "encrypt", "gzip", "acl_policy", "aws_conn_id"),
}


def snowflake_call(t: dict[str, Any], ctx: str | None = None) -> tuple[str, str]:
    """(statement, snowflake connection id) for a supported Snowflake operator."""
    ty, tid = t["type"], t["task_id"]
    a = {**t["template_fields"], **(t.get("args") or {})}
    conn = a.get("snowflake_conn_id") or a.get("conn_id") or "snowflake_default"
    ov = {k: a.get(k) for k in ("warehouse", "database", "role", "schema") if a.get(k)}
    if ty == "CopyFromExternalStageToSnowflakeOperator":
        if any(a.get(k) for k in ("columns_array", "storage_integration", "authenticator", "session_parameters")):
            raise Defer(f"{tid}: COPY operator option not mapped")
        kw = {k: a.get(k) for k in ("table", "stage", "file_format", "schema", "prefix", "files", "pattern", "copy_options", "validation_mode")}
        return f"snowflake_run(context, {conn!r}, {ov!r}, [copy_into_sql(**{expr(kw, {}, ctx)})])", conn
    if ty == "SQLExecuteQueryOperator" and "snowflake" not in conn.lower():
        raise Defer(f"{tid}: SQLExecuteQueryOperator on connection {conn!r} (only Snowflake connections are mapped)")
    sql = (t.get("template_files") or {}).get("sql") or a.get("sql")
    if not isinstance(sql, (str, list)):
        raise Defer(f"{tid}: SQL is not a string or a list of strings")
    if a.get("parameters"):
        raise Defer(f"{tid}: SQL parameters")
    return f"snowflake_run(context, {conn!r}, {ov!r}, {expr([sql] if isinstance(sql, str) else sql, {}, ctx)}, split=True)", conn


def task_templates(t: dict[str, Any]) -> Any:
    """The fields of a task that Airflow renders as templates (the ones this converter maps)."""
    ty, tf = t["type"], t["template_fields"]
    if ty in PY_TASKS | BASH_DEC:
        return [t.get("op_args"), t.get("op_kwargs"), tf.get("env") if ty in BASH_DEC else None]
    if ty in BASH:
        return [(t.get("template_files") or {}).get("bash_command") or tf.get("bash_command"), tf.get("env"), tf.get("cwd")]
    if ty in SNOWFLAKE_OPS:
        a = {**tf, **(t.get("args") or {})}
        return [(t.get("template_files") or {}).get("sql") or a.get("sql"), {k: a.get(k) for k in ("table", "stage", "prefix", "files", "pattern", "copy_options")}]
    if ty in S3_FIELDS:
        return {k: tf.get(k) for k in S3_FIELDS[ty]}
    return None


def and_condition(keys: list[list[str]]) -> str:
    """Fires only once EVERY upstream asset has updated since this asset was last handled (Airflow's schedule=[a, b])."""
    terms = [f"dg.AutomationCondition.asset_matches(dg.AssetKey({k!r}), dg.AutomationCondition.newly_updated()).since_last_handled()" for k in keys]
    return ("(" + " & ".join(terms) + " & ~dg.AutomationCondition.any_deps_missing() & ~dg.AutomationCondition.any_deps_in_progress() "
            "& ~dg.AutomationCondition.in_progress())")


def s3_call(t: dict[str, Any], params: dict[str, str], ctx: str | None = None) -> tuple[str, str]:
    """(statement, aws connection id) for a supported S3 operator."""
    ty, f = t["type"], t["template_fields"]
    conn = f.get("aws_conn_id") or "aws_default"
    g = lambda k: expr(f.get(k), params, ctx)
    if ty == "S3ListOperator":
        if f.get("apply_wildcard"):
            raise Defer(f"{t['task_id']}: S3ListOperator apply_wildcard")
        return f"return s3_list_keys(client, {g('bucket')}, {g('prefix')}, {g('delimiter')})", conn
    if ty == "S3CopyObjectOperator":
        for k in ("acl_policy", "meta_data_directive", "kms_key_id", "kms_encryption_type", "source_version_id"):
            if f.get(k):
                raise Defer(f"{t['task_id']}: S3CopyObjectOperator {k}")
        return (f"s3_copy_object(client, {g('source_bucket_key')}, {g('dest_bucket_key')}, {g('source_bucket_name')}, "
                f"{g('dest_bucket_name')})"), conn
    if ty == "S3DeleteObjectsOperator":
        if f.get("prefix") or f.get("from_datetime") or f.get("to_datetime") or f.get("keys") is None:
            raise Defer(f"{t['task_id']}: S3DeleteObjectsOperator by prefix or date range")
        return f"s3_delete_keys(client, {g('bucket')}, {g('keys')})", conn
    if f.get("gzip") or f.get("encrypt") or f.get("acl_policy"):
        raise Defer(f"{t['task_id']}: LocalFilesystemToS3Operator gzip/encrypt/acl")
    return f"s3_upload_file(client, {g('filename')}, {g('dest_bucket')}, {g('dest_key')}, {bool(f.get('replace'))!r})", conn


# --------------------------------------------------------------------------------------------------------------------
# Where an existing integration or community component fits (ids verified against the registry with `dagster-component search`)
# --------------------------------------------------------------------------------------------------------------------
HINTS: dict[str, dict[str, Any]] = {
    "BashOperator": {"registry": ["shell_command_asset"], "gap": "no retries, pool, multi-part asset key, automation condition or metadata attributes"},
    "_BashDecoratedOperator": {"registry": ["shell_command_asset"], "gap": "same as BashOperator; the command comes from a function"},
    "S3ListOperator": {"official": "dagster-aws S3Resource", "registry": [], "gap": "no component that lists objects as an asset"},
    "S3CopyObjectOperator": {"official": "dagster-aws S3Resource", "registry": [], "gap": "no copy-object asset component"},
    "S3DeleteObjectsOperator": {"official": "dagster-aws S3Resource", "registry": ["s3_cleanup_job"], "gap": "no delete-by-key-list asset"},
    "LocalFilesystemToS3Operator": {"official": "dagster-aws S3Resource", "registry": [], "gap": "no upload-file asset component"},
    "CopyFromExternalStageToSnowflakeOperator": {"official": "dagster-snowflake SnowflakeResource", "registry": ["snowflake_resource"], "gap": "no COPY INTO asset"},
    "SnowflakeOperator": {"official": "dagster-snowflake SnowflakeResource", "registry": ["snowflake_resource", "snowflake_stored_procedure_call_asset"], "gap": "no run-SQL-statements asset (multi-statement split, USE overrides)"},
    "SQLExecuteQueryOperator": {"official": "dagster-snowflake SnowflakeResource (Snowflake connections)", "registry": ["snowflake_resource"], "gap": "no run-SQL-statements asset"},
    "KubernetesPodOperator": {"official": "dagster-k8s PipesK8sClient", "registry": ["k8s_job_asset"], "gap": ""},
    "SimpleHttpOperator": {"registry": ["http_resource", "http_poll_sensor"], "gap": "no request-as-an-asset component"},
    "HttpOperator": {"registry": ["http_resource", "http_poll_sensor"], "gap": "no request-as-an-asset component"},
    "TriggerDagRunOperator": {"registry": ["asset_to_job_trigger_sensor"], "gap": "usually an asset dependency or automation condition instead"},
    "ExternalTaskSensor": {"registry": [], "gap": "usually an asset dependency; no sensor component for a task state in another job"},
    "TimeSensor": {"registry": [], "gap": "no time-of-day wait component; usually a schedule"},
    "PythonVirtualenvOperator": {"official": "Dagster Pipes (PipesSubprocessClient)", "registry": [], "gap": "no run-a-callable-in-a-virtualenv component"},
    "ExternalPythonOperator": {"official": "Dagster Pipes (PipesSubprocessClient)", "registry": [], "gap": "no run-a-callable-in-a-virtualenv component"},
}


# --------------------------------------------------------------------------------------------------------------------
# Per-DAG conversion
# --------------------------------------------------------------------------------------------------------------------
def convert_dag(dag: dict[str, Any], note: str, produced: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """(module source, summary). Raises Defer if any part of the DAG cannot be converted."""
    reasons: list[str] = []
    dag_id = dag["dag_id"]
    tasks = {t["task_id"]: t for t in dag["tasks"]}
    tt = dag["timetable"]
    triggers: list[str] = []
    trig_mode = "eager"
    if tt["kind"] in ("cron_trigger", "cron_data_interval", "manual", "once"):
        pass
    elif tt["kind"] == "asset_triggered":
        leaves, logic = tt.get("assets") or [], tt.get("asset_logic")
        if not leaves:
            reasons.append("asset-triggered schedule with no assets in the IR")
        elif len(leaves) == 1 or logic == "any":
            triggers = list(leaves)                               # one asset, or any of several: eager() fires on any update
        elif logic == "all":
            triggers, trig_mode = list(leaves), "and"             # Airflow waits for all; verified against Dagster's evaluator
        else:
            reasons.append(f"schedule mixes AND and OR over {len(leaves)} assets")
    else:
        reasons.append(f"schedule type {tt['class']} ({tt['kind']})")
    if dag.get("has_callbacks"):
        reasons.append("DAG-level callbacks")
    ds = dag.get("dag_settings") or {}
    if ds.get("fail_stop"):
        reasons.append("fail_stop=True (Airflow cancels running tasks on the first failure; Dagster lets independent steps finish)")
    max_runs = ds.get("max_active_runs")
    max_runs = max_runs if isinstance(max_runs, int) and not isinstance(max_runs, bool) and max_runs > 0 and max_runs != 16 else None
    def _pos(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None
    max_tasks = _pos(ds.get("max_active_tasks")) if ds.get("max_active_tasks") != 16 else None
    pool_limits: dict[str, int | None] = {}                    # pool name -> limit (None: take it from the Airflow pool's slot count)
    for k, default in (("max_active_tasks", 16), ("max_active_runs", 16), ("concurrency", 16)):
        v = ds.get(k)
        if k == "max_active_tasks" and max_tasks is not None:
            continue                                          # mapped below: a per-run executor limit
        if k == "max_active_runs" and max_runs is not None:
            continue                                          # mapped below: a run tag plus a deployment concurrency limit
        if v not in (None, default, 0) and not (isinstance(v, dict)):
            reasons.append(f"{k}={v} (a concurrency limit that is not mapped)")
    if ds.get("dagrun_timeout"):
        reasons.append("dagrun_timeout (a run time limit that is not mapped)")
    if ds.get("max_consecutive_failed_dag_runs"):
        reasons.append("max_consecutive_failed_dag_runs (auto-pause after failures is not mapped)")
    if ds.get("render_template_as_native_obj"):
        reasons.append("render_template_as_native_obj")
    if dag.get("catchup") and tt["kind"] in ("cron_trigger", "cron_data_interval"):
        reasons.append("catchup=True needs partitions and a backfill")
    for t in tasks.values():
        ty, tid, tf = t["type"], t["task_id"], t["template_fields"]
        if t["is_mapped"]:
            reasons.append(f"{tid}: dynamic task mapping")
        if t["is_setup"] or t["is_teardown"]:
            reasons.append(f"{tid}: setup/teardown")
        if t["trigger_rule"] != "all_success":
            reasons.append(f"{tid}: trigger_rule {t['trigger_rule']}")
        # every behavior-affecting task setting is mapped or defers the DAG: none is silently ignored
        for flag, why in (("depends_on_past", "depends_on_past"), ("wait_for_downstream", "wait_for_downstream"),
                          ("retry_exponential_backoff", "retry_exponential_backoff"), ("sla_s", "an SLA")):
            if t.get(flag):
                reasons.append(f"{tid}: {why}")
        if t.get("max_active_tis_per_dagrun"):
            reasons.append(f"{tid}: max_active_tis_per_dagrun")
        mtis = t.get("max_active_tis_per_dag")
        if mtis:
            if _pos(mtis) is None or t.get("pool"):
                reasons.append(f"{tid}: max_active_tis_per_dag={mtis}" + (" together with a named pool" if t.get("pool") else ""))
            else:
                pool_limits[f"{safe(dag_id)}__{safe(tid)}"] = mtis   # a Dagster pool of that size, shared by every run
        if t.get("pool"):
            pool_limits.setdefault(t["pool"], None)
        if t.get("pool_slots") not in (None, 1):
            reasons.append(f"{tid}: pool_slots={t['pool_slots']}")
        cbs = [c for c in (t.get("callbacks") or []) if not (t["is_mapped"] and c in ("pre_execute", "post_execute"))]   # a mapped wrapper overrides them; mapping already defers
        if cbs:
            reasons.append(f"{tid}: task callbacks or hooks {cbs}")
        if t.get("execution_timeout_s") and ty not in BASH | BASH_DEC:
            reasons.append(f"{tid}: execution_timeout on a non-shell task (a Python callable cannot be interrupted safely)")
        if ty in PY_TASKS and (t.get("template_fields") or {}).get("templates_dict"):
            reasons.append(f"{tid}: templates_dict (rendered templates passed to the callable) is not rebuilt")
        if len(t["outlets"]) > 1:
            reasons.append(f"{tid}: {len(t['outlets'])} outlets")
        if ty not in ELIDED | PY_TASKS | BASH | BASH_DEC | S3_OPS | SNOWFLAKE_OPS:
            reasons.append(f"{tid}: operator {ty}")
        _, jprob = jinja_vars(task_templates(t))
        if jprob:
            reasons.append(f"{tid}: {jprob}")
        if ty in BASH and isinstance(tf.get("bash_command"), str) and tf["bash_command"].rstrip().endswith((".sh", ".bash")) \
                and "bash_command" not in (t.get("template_files") or {}):
            reasons.append(f"{tid}: bash_command names a script file ({tf['bash_command'].strip()[:40]}) that was not found")
    if reasons:
        raise Defer("; ".join(dict.fromkeys(reasons)))

    def eff_up(tid: str) -> list[str]:
        out: list[str] = []
        for u in tasks[tid]["upstream"]:
            out += eff_up(u) if tasks[u]["type"] in ELIDED else [u]
        return list(dict.fromkeys(out))

    real = [t for t in dag["tasks"] if t["type"] not in ELIDED]
    if not real:
        raise Defer("only EmptyOperators: nothing to run")
    notes: list[str] = []

    def reaches(a: str, b: str) -> bool:
        """True if task a is upstream of task b (possibly through elided tasks)."""
        stack, seen = list(tasks[b]["upstream"]), set()
        while stack:
            x = stack.pop()
            if x == a:
                return True
            if x not in seen:
                seen.add(x)
                stack += tasks[x]["upstream"]
        return False

    keyparts: dict[str, list[str]] = {}
    for t in real:
        keyparts[t["task_id"]] = key_from_asset(t["outlets"][0]) if t["outlets"] else [safe(dag_id)] + [safe(p) for p in t["task_id"].split(".")]
        k = "/".join(keyparts[t["task_id"]])
        if k in produced and produced[k] != dag_id:
            raise Defer(f"{t['task_id']}: asset key {k} is already produced by DAG {produced[k]} (one producer per asset)")
    # Several tasks naming the same outlet: if they form a strict chain, the LAST one produces the asset and the earlier ones
    # become ordinary step assets. Unordered writers need an owner decision (Dagster allows one producer per asset).
    by_key: dict[str, list[str]] = {}
    for tid, kp in keyparts.items():
        by_key.setdefault("/".join(kp), []).append(tid)
    for k, tids in by_key.items():
        if len(tids) < 2:
            continue
        order = sorted(tids, key=lambda x: sum(reaches(y, x) for y in tids))        # most upstream first
        if not all(reaches(a, b) for a, b in zip(order, order[1:])):
            raise Defer(f"tasks {sorted(tids)} update the same asset {k} but are not ordered: one producer per asset, so the owner must choose")
        for tid in order[:-1]:
            keyparts[tid] = [safe(dag_id)] + [safe(p) for p in tid.split(".")]
        notes.append(f"{' -> '.join(order)} all update {k}; only {order[-1]} produces the asset (the earlier steps are plain assets). "
                     "Airflow emits a dataset event after each of them; Dagster emits one, after the last.")
    consumed: set[str] = set()
    for t in real:
        consumed |= xcom_refs(t.get("op_args")) | xcom_refs(t.get("op_kwargs")) | xcom_refs(t["template_fields"])
    own_keys = {"/".join(v) for v in keyparts.values()}
    external: set[tuple[str, ...]] = set()
    resources: set[str] = set()
    sf_resources: set[str] = set()
    shims_used: set[str] = set()
    support: dict[str, int] = {}
    blocks: list[str] = []
    roots = [t["task_id"] for t in real if not eff_up(t["task_id"])]

    def ancestors(tid: str) -> list[str]:
        out: list[str] = []
        for u in eff_up(tid):
            out += [*ancestors(u), u]
        return list(dict.fromkeys(out))

    # pre-pass: which callables need the Airflow context, which tasks pull upstream values (ti.xcom_pull), which templates need dates
    resolved: dict[str, tuple[str, list[str], dict[str, Any]]] = {}
    ctx_tasks: set[str] = set()
    ti_inputs: dict[str, list[str]] = {}
    for t in real:
        tid, ty = t["task_id"], t["type"]
        meta: dict[str, Any] = {}
        if ty in PY_TASKS | BASH_DEC:
            code, sup = resolve_callable(t.get("callable"), shims_used, meta)
            resolved[tid] = (code, sup, meta)
        names, _ = jinja_vars(task_templates(t))
        if meta.get("needs_ctx") or names:
            ctx_tasks.add(tid)
        if meta.get("needs_ti") or {"ti", "task_instance"} & names:
            ti_inputs[tid] = [u for u in ancestors(tid) if u in keyparts]      # xcom_pull may name any task that ran before
            consumed |= set(ti_inputs[tid])
    uses_ctx = bool(ctx_tasks)
    dag_params = dag.get("params") or {}
    for t in real:
        tid = t["task_id"]
        ups = [u for u in eff_up(tid) if u in keyparts]
        refs = xcom_refs(t.get("op_args")) | xcom_refs(t.get("op_kwargs")) | xcom_refs(t["template_fields"])
        ordered = list(dict.fromkeys([*ups, *ti_inputs.get(tid, [])]))
        params = {u: f"in_{i}" for i, u in enumerate(ordered)}
        data = {u for u in ordered if u in refs or u in ti_inputs.get(tid, [])}
        ctxn = "ctx" if tid in ctx_tasks else None
        ctx_line = (f"    ctx = af_context(context, {dag_id!r}, {tid!r}, {dag_params!r}, "
                    "{" + ", ".join(f"{u!r}: {params[u]}" for u in ordered if u in data) + "})\n") if ctxn else ""
        args = ["    context: dg.AssetExecutionContext"] + [f"    {params[u]}" for u in ordered if u in data]
        deco = [f"key={keyparts[tid]!r}", f"group_name={safe(dag_id)!r}"]
        if data:
            deco.append("ins={" + ", ".join(f"{params[u]!r}: dg.AssetIn(key={keyparts[u]!r})" for u in ordered if u in data) + "}")
        deps = [keyparts[u] for u in ups if u not in data]
        for inl in t["inlets"]:
            k = key_from_asset(inl)
            if k == keyparts[tid]:
                notes.append(f"{tid}: reads and writes {'/'.join(k)}; the self-dependency was dropped (an asset cannot depend on itself unless partitioned)")
                continue
            if "/".join(k) not in own_keys:
                external.add(tuple(k))
            deps.append(k)
        if triggers and tid in roots:
            for a in triggers:
                k = key_from_asset(a)
                if "/".join(k) not in own_keys:
                    external.add(tuple(k))
                deps.append(k)
        if deps:
            deco.append("deps=[" + ", ".join(f"dg.AssetKey({d!r})" for d in deps) + "]")
        if triggers and trig_mode == "and" and tid in roots:
            deco.append("automation_condition=" + and_condition([key_from_asset(a) for a in triggers]))
        elif triggers:
            deco.append("automation_condition=dg.AutomationCondition.eager()")
        if t["retries"]:
            deco.append(f"retry_policy=dg.RetryPolicy(max_retries={t['retries']}, delay={int(t['retry_delay_s'] or 300)})")
        if t["pool"]:
            deco.append(f"pool={t['pool']!r}")
        elif t.get("max_active_tis_per_dag") and f"{safe(dag_id)}__{safe(tid)}" in pool_limits:
            deco.append(f"pool={safe(dag_id) + '__' + safe(tid)!r}")
        deco.append(f"metadata={{'airflow_dag': {dag_id!r}, 'airflow_task': {tid!r}, 'airflow_operator': {t['type']!r}}}")
        fname = safe("t_" + tid)
        ty = t["type"]
        ret_none = tid not in consumed and not (RETURN_ALL and ty in PY_TASKS)
        if ty in BASH:
            cmd = (t.get("template_files") or {}).get("bash_command") or t["template_fields"].get("bash_command")
            if not isinstance(cmd, str):
                raise Defer(f"{tid}: bash_command is not a string")
            env = t["template_fields"].get("env")
            env_expr = expr(env, params, ctxn) if env else "None"
            body = (ctx_line + f"    run_shell(context, {expr(cmd, params, ctxn)}, env={env_expr}, "
                    f"cwd={expr(t['template_fields'].get('cwd'), params, ctxn)}, timeout={t.get('execution_timeout_s')!r})")
            deco.append("kinds={'shell'}")
            pre = ""
        elif ty in PY_TASKS | BASH_DEC:
            code, sup, meta = resolved[tid]
            for sline in sup:
                support.setdefault(sline, len(support))
            pyname = "_af_" + safe(tid)
            code = re.sub(r"^def\s+\w+", f"def {pyname}", code, count=1)
            pos = [expr(a, params, ctxn) for a in (t.get("op_args") or [])]
            kws = {k: expr(v, params, ctxn) for k, v in (t.get("op_kwargs") or {}).items()}
            if meta.get("needs_ctx"):
                inner = f"call_with_context({pyname}, [{', '.join(pos)}], {{{', '.join(f'{k!r}: {v}' for k, v in kws.items())}}}, ctx)"
            else:
                inner = f"{pyname}(" + ", ".join(pos + [f"{k}={v}" for k, v in kws.items()]) + ")"
            pre = code + "\n\n\n"
            if ty in BASH_DEC:
                body = (ctx_line + f"    cmd = {inner}\n    if not isinstance(cmd, str) or not cmd.strip():\n"
                        f"        raise dg.Failure(description='@task.bash must return a non-empty command string')\n"
                        f"    run_shell(context, cmd, env={t['template_fields'].get('env')!r}, cwd={t['template_fields'].get('cwd')!r}, timeout={t.get('execution_timeout_s')!r})")
                deco.append("kinds={'shell'}")
            else:
                body = ctx_line + (f"    return {inner}" if not ret_none else f"    {inner}")
                deco.append("kinds={'python'}")
        elif ty in SNOWFLAKE_OPS:
            stmt, conn = snowflake_call(t, ctxn)
            sf_resources.add(conn)
            deco.append(f"required_resource_keys={{{conn!r}}}")
            deco.append("kinds={'snowflake'}")
            body = ctx_line + f"    {stmt}"
            pre = ""
            ret_none = True
        else:
            stmt, conn = s3_call(t, params, ctxn)
            resources.add(conn)
            deco.append(f"required_resource_keys={{{conn!r}}}")
            deco.append("kinds={'s3'}")
            body = ctx_line + f"    client = getattr(context.resources, {conn!r})     # dagster-aws attaches the boto client\n    {stmt}"
            pre = ""
            ret_none = not stmt.startswith("return")
        sig = "(\n" + ",\n".join(args) + ",\n)" + (" -> None" if ret_none else "")
        blocks.append(pre + "@dg.asset(\n    " + ",\n    ".join(deco) + ",\n)\ndef " + fname + sig + ":\n" + body + "\n")

    keys = [keyparts[t["task_id"]] for t in real]
    imports = ["import dagster as dg", "", "from ..runtime import *  # noqa: F401,F403"]
    mod = [f'"""Converted from Airflow DAG `{dag_id}` ({dag["file"]}) by convert_ir.py.', "", note, '"""', *imports, ""]
    if support:
        mod += [s for s, _ in sorted(support.items(), key=lambda kv: kv[1])] + [""]
    if "S3Hook" in shims_used:
        notes.append("callables use S3Hook through a compatibility shim (default AWS credentials; the connection id is not applied)")
    if notes:
        mod[1:1] = ["", *[f"NOTE: {n}" for n in notes]]
    out = "\n".join(mod) + "\n\n" + "\n\n".join(blocks) + "\n"
    out += (f"\n{safe(dag_id)}_job = dg.define_asset_job({safe(dag_id)!r}, selection=[dg.AssetKey(k) for k in {keys!r}]"
            + (f", tags={{'airflow/dag_id': {dag_id!r}}}" if max_runs else "")
            + (f", executor_def=dg.multiprocess_executor.configured({{'max_concurrent': {max_tasks}}})" if max_tasks else "") + ")\n")
    if tt["kind"] in ("cron_trigger", "cron_data_interval") and uses_ctx:
        sem = "data_interval" if tt["kind"] == "cron_data_interval" else "trigger"
        out += (f"\n{safe(dag_id)}_schedule = airflow_schedule({safe(dag_id) + '_schedule'!r}, {safe(dag_id)}_job, "
                f"{tt['cron']!r}, {tz_name(tt['timezone'])!r}, {sem!r})\n")
    elif tt["kind"] in ("cron_trigger", "cron_data_interval"):
        out += (f"\n{safe(dag_id)}_schedule = dg.ScheduleDefinition(name={safe(dag_id) + '_schedule'!r}, job={safe(dag_id)}_job, "
                f"cron_schedule={tt['cron']!r}, execution_timezone={tz_name(tt['timezone'])!r})\n")
    for k in own_keys:
        produced[k] = dag_id
    return out, {"assets": len(real), "elided": len(tasks) - len(real), "schedule": tt.get("cron"),
                 "timezone": tz_name(tt["timezone"]) if tt.get("cron") else None, "triggers": triggers,
                 "resources": sorted(resources), "snowflake_resources": sorted(sf_resources), "shims": sorted(shims_used), "notes": notes, "requirements": sorted(third_party(support) | ({"croniter"} if uses_ctx and tt["kind"] == "cron_data_interval" else set())), "external": sorted("/".join(e) for e in external), "keys": keys,
                 "uses_context": uses_ctx, "max_active_runs": max_runs, "max_active_tasks": max_tasks, "pools": pool_limits,
                 "task_keys": {t["task_id"]: "/".join(keyparts[t["task_id"]]) for t in real}}


RUNTIME = '''"""Helpers used by the converted DAGs. Each mirrors what the Airflow operator or hook does."""
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


import contextvars
import datetime as _dt
import inspect
import json
import types

_MISSING = object()
_AF_CTX: contextvars.ContextVar = contextvars.ContextVar("airflow_context", default=None)


class _TaskInstance:
    """The part of Airflow's TaskInstance converted callables and templates use. Anything else raises."""

    def __init__(self, dag_id, task_id, run_id, try_number, upstream):
        self.dag_id, self.task_id, self.run_id, self.try_number, self._up = dag_id, task_id, run_id, try_number, upstream

    def xcom_pull(self, task_ids=None, dag_id=None, key="return_value", include_prior_dates=False, map_indexes=None, default=None):
        if key != "return_value" or dag_id not in (None, self.dag_id) or include_prior_dates or map_indexes is not None or task_ids is None:
            raise NotImplementedError("xcom_pull is mirrored only for return values of named tasks in this DAG")
        ids = [task_ids] if isinstance(task_ids, str) else list(task_ids)
        missing = [i for i in ids if i not in self._up]
        if missing:
            raise dg.Failure(description=f"xcom_pull of {missing}: not an upstream asset of this task, so its value was not passed in")
        vals = [self._up[i] for i in ids]
        return vals[0] if isinstance(task_ids, str) else vals

    def xcom_push(self, *a, **k):
        raise NotImplementedError("xcom_push is not mirrored: return the value instead")


def af_context(context, dag_id, task_id, params, upstream):
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
    conf = json.loads(tags.get("airflow/conf", "{}"))
    run_id = run.run_id if run is not None else context.run_id        # context.run_id is deprecated in current Dagster
    ti = _TaskInstance(dag_id, task_id, run_id, getattr(context, "retry_number", 0) + 1, upstream)
    return {"ds": ds, "ds_nodash": ds.replace("-", ""), "ts": ld.isoformat(), "ts_nodash": ld.strftime("%Y%m%dT%H%M%S"),
            "logical_date": ld, "execution_date": ld, "data_interval_start": ld, "data_interval_end": de,
            "run_id": run_id, "params": dict(params or {}), "conf": conf,
            "dag": types.SimpleNamespace(dag_id=dag_id), "task": types.SimpleNamespace(task_id=task_id), "ti": ti, "task_instance": ti,
            "dag_run": types.SimpleNamespace(run_id=run_id, conf=conf, logical_date=ld, execution_date=ld, dag_id=dag_id)}


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
        return fn(*args, **call_kwargs)
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

    def execution_fn(ctx):
        tick = ctx.scheduled_execution_time
        logical = croniter(cron, tick).get_prev(_dt.datetime) if semantics == "data_interval" else tick
        utc = _dt.timezone.utc
        return dg.RunRequest(tags={"airflow/logical_date": logical.astimezone(utc).isoformat(),
                                   "airflow/data_interval_end": tick.astimezone(utc).isoformat()})

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


def s3_upload_file(client: Any, filename: str, bucket: str, key: str, replace: bool) -> None:
    """LocalFilesystemToS3Operator without gzip/encrypt/acl (S3Hook.load_file)."""
    b, k = s3_bucket_key(bucket, key)
    if not replace and _s3_head(client, b, k):
        raise ValueError(f"The key {k} already exists.")
    client.upload_file(str(filename), b, k, ExtraArgs={}, Config=_transfer_config())
'''


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ir", type=Path)
    ap.add_argument("--out-dir", type=Path, required=True, help="the project's defs directory (src/<pkg>/defs)")
    ap.add_argument("--report", type=Path, default=Path("conversion_report.json"))
    ap.add_argument("--return-all", action="store_true", help="return every Python task's value (stored by the IO manager), for parity runs")
    a = ap.parse_args(argv)
    global RETURN_ALL
    RETURN_ALL = a.return_all
    ir = json.loads(a.ir.read_text())
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "__init__.py").touch()
    (a.out_dir.parent / "runtime.py").write_text(RUNTIME)
    note = ("Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 "
            "(trigger timetable) and Airflow 2 (data-interval timetable).")
    report: dict[str, Any] = {"airflow_version": ir.get("airflow_version"), "dags": {}}
    produced: dict[str, str] = {}
    resources: set[str] = set()
    sf_resources: set[str] = set()
    external: set[str] = set()
    for dag in ir["dags"]:
        name = safe(dag["dag_id"])
        try:
            code, summary = convert_dag(dag, note, produced)
            compile(code, name, "exec")
            (a.out_dir / f"{name}.py").write_text(code)
            report["dags"][dag["dag_id"]] = {"status": "converted", **summary}
            resources |= set(summary["resources"])
            sf_resources |= set(summary["snowflake_resources"])
            external |= set(summary["external"])
        except Defer as e:
            report["dags"][dag["dag_id"]] = {"status": "deferred", "reason": str(e)}
        except SyntaxError as e:
            report["dags"][dag["dag_id"]] = {"status": "deferred", "reason": f"generated code did not compile: {e.msg}"}
        except Exception as e:                       # a converter bug must show up as a named deferral, never a crash or a silent skip
            report["dags"][dag["dag_id"]] = {"status": "deferred", "reason": f"CONVERTER ERROR {type(e).__name__}: {e}"}
    owned = {"/".join(k) for v in report["dags"].values() if v["status"] == "converted" for k in v["keys"]}
    ext = sorted(e for e in external if e not in owned)
    if resources or sf_resources:
        lines = ['"""Airflow connections become resources under the same key (credentials from the environment)."""',
                 "import dagster as dg"]
        if resources:
            lines.append("from dagster_aws.s3 import S3Resource")
        if sf_resources:
            lines.append("from dagster_snowflake import SnowflakeResource")
        entries = [f"{r!r}: S3Resource()" for r in sorted(resources)] + [
            f"{r!r}: SnowflakeResource(account=dg.EnvVar('SNOWFLAKE_ACCOUNT'), user=dg.EnvVar('SNOWFLAKE_USER'), password=dg.EnvVar('SNOWFLAKE_PASSWORD'))"
            for r in sorted(sf_resources)]
        (a.out_dir / "resources.py").write_text("\n".join(lines) + "\n\n\n@dg.definitions\ndef resources() -> dg.Definitions:\n    return dg.Definitions(resources={" + ", ".join(entries) + "})\n")
    if ext:
        (a.out_dir / "sources.py").write_text(
            '"""Assets that converted DAGs read or are triggered by but that no converted DAG produces."""\n'
            "import dagster as dg\n\n\n@dg.definitions\ndef sources() -> dg.Definitions:\n    return dg.Definitions(assets=[\n" +
            "".join(f"        dg.AssetSpec(key={e.split('/')!r}),\n" for e in ext) + "    ])\n")
    conv = {k: v for k, v in report["dags"].items() if v["status"] == "converted"}
    limits = {k: v["max_active_runs"] for k, v in conv.items() if v.get("max_active_runs")}
    pools: dict[str, int | None] = {}
    for v in conv.values():
        pools.update(v.get("pools") or {})
    if limits or pools:
        text = ("# Concurrency settings from the Airflow DAGs. They are enforced only when applied to the deployment.\n"
                "# 1) max_active_runs -> run queue tag limits: merge into run_coordinator.config in dagster.yaml (or the Dagster+ deployment settings).\n"
                "tag_concurrency_limits:\n" + "".join(f"  - key: airflow/dag_id\n    value: {k!r}\n    limit: {n}\n" for k, n in sorted(limits.items())) +
                "# 2) pools (Airflow pools and max_active_tis_per_dag) -> Dagster pools, shared by every run. Apply each with:\n"
                "#      dagster instance concurrency set <pool> <limit>      (or in the Dagster+ deployment settings)\n"
                "pools:\n" + "".join(f"  {k}: {n if n is not None else 'null  # set to the slot count of this Airflow pool (it lives in the Airflow metadata database)'}\n"
                                       for k, n in sorted(pools.items())))
        (a.out_dir.parent / "concurrency_limits.yaml").write_text(text)
    reqs = sorted({r for v in report["dags"].values() if v["status"] == "converted" for r in v["requirements"]})
    if reqs:
        (a.out_dir.parent / "converted_requirements.txt").write_text("\n".join(reqs) + "\n")
    supported = ELIDED | PY_TASKS | BASH | BASH_DEC | S3_OPS | SNOWFLAKE_OPS
    ops = Counter(t["type"] for d in ir["dags"] for t in d["tasks"])
    report["operators"] = {ty: {"tasks": n, "mapped_by_converter": ty in supported, **(HINTS.get(ty) or {})} for ty, n in ops.most_common()}
    a.report.write_text(json.dumps(report, indent=1) + "\n")
    c = Counter(v["status"] for v in report["dags"].values())
    print(f"{c['converted']} converted, {c['deferred']} deferred of {len(report['dags'])} DAGs; resources {sorted(resources | sf_resources)}; external assets {len(ext)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
