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
             "ti": {"dag_id", "task_id", "run_id", "try_number", "xcom_pull", "xcom_push"},
             "task_instance": {"dag_id", "task_id", "run_id", "try_number", "xcom_pull", "xcom_push"}}
SUPPORTED_MACROS = {"ds_add", "ds_format", "datetime", "timedelta"}
SHIM_IMPORTS = {("airflow.models", "Variable"), ("airflow.sdk", "Variable"), ("airflow.models.variable", "Variable"),
                ("airflow.operators.python", "get_current_context"), ("airflow.sdk", "get_current_context"),
                ("airflow.providers.amazon.aws.hooks.s3", "S3Hook")} | \
    {(m, n) for m in ("airflow.exceptions", "airflow.sdk.exceptions", "airflow.sdk") for n in ("AirflowException", "AirflowSkipException", "AirflowFailException")} | \
    {(m, "PokeReturnValue") for m in ("airflow.sensors.base", "airflow.sdk", "airflow.sdk.bases.sensor")}
ELIDED = {"EmptyOperator", "DummyOperator"}
PY_TASKS = {"PythonOperator", "_PythonDecoratedOperator", "DecoratedOperator"}
BASH = {"BashOperator"}
BASH_DEC = {"_BashDecoratedOperator"}
BRANCH = {"BranchPythonOperator", "_BranchPythonDecoratedOperator"}
SHORT = {"ShortCircuitOperator", "_ShortCircuitDecoratedOperator"}
TRIGGER = {"TriggerDagRunOperator"}          # launches another converted DAG's job and waits for it (wait_for_completion=True)
SENSOR_CALL = {"PythonSensor", "DecoratedSensorOperator"}                                # sensors that poll a callable
SENSORS = SENSOR_CALL | {"BashSensor", "FileSensor", "TimeSensor", "TimeDeltaSensor"}   # converted as a polling step (Airflow's default `poke` behavior)
CLOCK_BRANCH = {"BranchDateTimeOperator", "BranchDayOfWeekOperator"}      # branch on the clock or the logical date: no callable
GATE_RULES = {"all_success", "all_failed", "all_done", "one_success", "one_failed", "one_done", "none_failed", "none_failed_min_one_success",
              "none_skipped", "all_skipped", "all_done_min_one_success", "always", "all_done_setup_success"}
PLAIN_RULES = {"all_success", "none_failed", "none_failed_min_one_success"}               # behave like all_success when a failure stops the run anyway
S3_OPS = {"S3ListOperator", "S3CopyObjectOperator", "S3DeleteObjectsOperator", "LocalFilesystemToS3Operator"}
SNOWFLAKE_OPS = {"CopyFromExternalStageToSnowflakeOperator", "SnowflakeOperator", "SQLExecuteQueryOperator"}


class Defer(Exception):
    pass


AF_MAJOR = 2             # the Airflow major version of the IR being converted (set from the IR): Airflow 3 parses a shell task's output as JSON
MAP_MODE = "graph"       # --mapping graph (dynamic outputs in one graph asset) | partitions (leaf mapped tasks as dynamic/static partitions)
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


def resolve_callable(info: dict[str, Any] | None, shims: set[str] | None = None, meta: dict[str, Any] | None = None,
                     context_arg: bool = False) -> tuple[str, list[str]]:
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
    if context_arg and named:
        named = named[1:]                                        # a callback's first parameter is the whole context dict, whatever it is called
    unsupported = sorted(set(named) & (KNOWN_CTX - SUPPORTED_CTX))
    if unsupported:
        raise Defer(f"callable uses Airflow task context names the runtime does not rebuild {unsupported}")
    ctx_named = sorted(set(named) & SUPPORTED_CTX)
    inner_shims = [n for n in ast.walk(fn) if isinstance(n, ast.ImportFrom) and n.module and all((n.module, a.name) in SHIM_IMPORTS for a in n.names)]
    for n in inner_shims:                                        # `from airflow.exceptions import AirflowSkipException` inside the function: the runtime provides the name
        shims.update(a.name for a in n.names)
    body_text = ast.unparse(ast.Module(body=[*fn.body], type_ignores=[]))
    for n in inner_shims:
        body_text = body_text.replace(ast.unparse(n), "")
    if re.search(r"\bairflow\b|\bset_xcom\b", body_text):
        raise Defer("callable uses the Airflow API (a direct airflow reference)")
    for n in (m for stmt in [*fn.body, *fn.args.defaults, *[d for d in fn.args.kw_defaults if d is not None]] for m in ast.walk(stmt)):    # the body, not the decorators (`@task.sensor` is not the `task` context object)
        obj = n.value if isinstance(n, ast.Attribute) else None
        if isinstance(obj, ast.Subscript) and isinstance(obj.slice, ast.Constant):
            obj_name = obj.slice.value
        else:
            obj_name = obj.id if isinstance(obj, ast.Name) else None
        if obj_name in CTX_ATTRS and n.attr not in CTX_ATTRS[obj_name]:
            raise Defer(f"callable uses {obj_name}.{n.attr}, which the runtime's {obj_name} does not provide")
    meta["skips"] = "AirflowSkipException" in info["source"]
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
    for n in inner_shims:                                        # the import line goes; `pass` keeps the block valid
        indent = re.match(r"\s*", lines[n.lineno - 1]).group(0)
        lines[n.lineno - 1:n.end_lineno] = [indent + "pass"] + [""] * (n.end_lineno - n.lineno)
    start = next(i for i, l in enumerate(lines) if re.match(r"def\s", l))
    return "\n".join(lines[start:]), [needed[k] for k in sorted(needed)]


# --------------------------------------------------------------------------------------------------------------------
# helpers shared by the per-task handlers
# --------------------------------------------------------------------------------------------------------------------
def local_modules(mods: set[str], files: Any) -> set[str]:
    """The imported modules that live next to a source file or in a folder above it (a project's own `include/`, `utils/`): they are not pip packages."""
    roots: list[Path] = []
    for f in ([files] if isinstance(files, str) else files):
        if f and Path(f).is_absolute():
            here = Path(f).resolve().parent
            roots += [here, *here.parents[:4]]
    return {m for m in mods if any((r / f"{m}.py").exists() or (r / m).is_dir() for r in roots)}


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
    if ty in TRIGGER:
        return [tf.get("conf"), tf.get("trigger_dag_id")]
    if ty == "BashSensor":
        return [tf.get("bash_command"), tf.get("env")]
    if ty == "FileSensor":
        return [tf.get("filepath")]
    if ty in PY_TASKS | BASH_DEC | BRANCH | SHORT | SENSOR_CALL:
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
def plan_graph(dag: dict[str, Any], tasks: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Dynamic task mapping as Dagster dynamic outputs: the whole DAG becomes one graph asset in which a mapped task is a mapped op
    and a task after it receives the collected list. Python and shell tasks only; `expand` with literal lists or other tasks' outputs."""
    mapped = {k: t for k, t in tasks.items() if t.get("is_mapped")}
    gated = any(t["type"] in BRANCH | SHORT | CLOCK_BRANCH or (t["type"] in SENSORS and (t.get("sensor") or {}).get("soft_fail"))
                or t["trigger_rule"] in GATE_RULES - PLAIN_RULES or t.get("is_setup") or t.get("is_teardown")
                or "AirflowSkipException" in (t.get("callable") or {}).get("source", "") for t in tasks.values())
    if not mapped and not gated:
        return None, []
    why: list[str] = []
    for tid, t in tasks.items():
        if t["type"] not in ELIDED | PY_TASKS | BASH | BASH_DEC | BRANCH | SHORT | CLOCK_BRANCH | SENSORS:
            why.append(f"{tid}: a {t['type']} inside a DAG with mapped tasks (only Python and shell tasks are converted there)")
    mp: dict[str, Any] = {}
    for tid, t in mapped.items():
        e = t.get("expand") or {}
        if e.get("form") not in ("expand", "expand_kwargs") or not e.get("args"):
            why.append(f"{tid}: dynamic task mapping with {e.get('form')} is not converted")
            continue
        if e.get("fixed_unserializable") or e.get("op_args"):
            why.append(f"{tid}: mapped task with fixed arguments that are not plain values")
            continue
        srcs: dict[str, tuple[str, Any]] = {}
        for name, a in e["args"].items():
            if a["kind"] == "literal" and isinstance(a.get("value"), list):
                srcs[name] = ("lit", a["value"])
            elif a["kind"] == "xcom" and a.get("wrapper") == "PlainXComArg" and a.get("key") == "return_value" and a.get("task_id") in tasks:
                srcs[name] = ("task", a["task_id"])
            else:
                why.append(f"{tid}: mapping over {a['kind']}{':' + a['wrapper'] if a.get('wrapper') else ''} for `{name}` "
                           "(only a literal list or a task's plain return value is converted)")
                break
        else:
            mode = "kwargs" if e["form"] == "expand_kwargs" else ("args" if list(srcs) == ["op_args"] else "kw")
            if mode != "kw":
                bad = [] if t["type"] in PY_TASKS | BASH_DEC or mode == "kwargs" else ["op_args"]
                if mode == "kwargs" and t["type"] in BASH and "bash_command" not in (e.get("partial") or {}):
                    pass
            elif t["type"] in PY_TASKS | BASH_DEC:
                bad = [n for n in srcs if n not in (t.get("callable") or {}).get("params", "")]
            else:
                bad = [n for n in srcs if n not in ("bash_command", "env", "cwd")]
                if "bash_command" not in srcs and "bash_command" not in (e.get("partial") or {}):
                    bad.append("bash_command (not found)")
            if bad:
                why.append(f"{tid}: mapped arguments {bad} are not supported for this task")
            elif len(srcs) > 1 and any(v[0] == "task" and tasks[v[1]].get("is_mapped") for v in srcs.values()):
                why.append(f"{tid}: a cross product over the output of a mapped task")
            else:
                mp[tid] = {"srcs": srcs, "fixed": e.get("fixed") or {}, "partial": e.get("partial") or {}, "mode": mode}
    if why:
        return None, why
    return {"mode": "graph", "mapped": mp}, []


def plan_mapping(dag: dict[str, Any], tasks: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    if tasks and all(t["type"] in ELIDED for t in tasks.values()):
        return {"mode": "graph", "mapped": {}}, []              # nothing to run, but the steps and their trigger rules are kept
    if MAP_MODE == "graph" or any(t["type"] in BRANCH | SHORT | CLOCK_BRANCH or (t["type"] in SENSORS and (t.get("sensor") or {}).get("soft_fail"))
                                  or t["trigger_rule"] in GATE_RULES - PLAIN_RULES or t.get("is_setup") or t.get("is_teardown")
                                  or "AirflowSkipException" in (t.get("callable") or {}).get("source", "") for t in tasks.values()):
        return plan_graph(dag, tasks)
    return plan_partitions(dag, tasks)


def plan_partitions(dag: dict[str, Any], tasks: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Dynamic task mapping in the one shape converted today: leaf mapped Python tasks that all iterate over the same literal
    list, or over the return value of one root Python task (the producer). Anything else is reported, never guessed."""
    mapped = {k: t for k, t in tasks.items() if t.get("is_mapped")}
    if not mapped:
        return None, []
    why: list[str] = []
    down: dict[str, list[str]] = {}
    for t in tasks.values():
        for u in t["upstream"]:
            down.setdefault(u, []).append(t["task_id"])
    sources, argnames = set(), {}
    for tid, t in mapped.items():
        e = t.get("expand") or {}
        if t["type"] not in PY_TASKS:
            why.append(f"{tid}: dynamic task mapping of a {t['type']}")
            continue
        if e.get("form") != "expand" or len(e.get("args") or {}) != 1:
            why.append(f"{tid}: dynamic task mapping other than a single `expand` argument ({e.get('form')}, {len(e.get('args') or {})} arguments)")
            continue
        (name, a), = e["args"].items()
        if e.get("fixed_unserializable") or e.get("op_args"):
            why.append(f"{tid}: mapped task with fixed arguments that are not plain values")
            continue
        if a["kind"] == "literal" and a["value"] and t["upstream"] == []:
            sources.add(("literal", json.dumps(a["value"], sort_keys=True)))
        elif a["kind"] == "xcom" and a.get("wrapper") == "PlainXComArg" and a.get("key") == "return_value" and t["upstream"] == [a["task_id"]]:
            sources.add(("xcom", a["task_id"]))
        else:
            why.append(f"{tid}: dynamic task mapping over {a['kind']}{':' + a['wrapper'] if a.get('wrapper') else ''} "
                       "(only a literal list or the plain return value of its single upstream task is converted)")
            continue
        argnames[tid] = name
        if down.get(tid):
            why.append(f"{tid}: a task runs after the mapped task (a reduce step is not converted)")
        if name not in (t.get("callable") or {}).get("params", ""):
            why.append(f"{tid}: mapped argument {name!r} is not a parameter of the callable")
    if len(sources) > 1:
        why.append("mapped tasks iterate over different sources")
    if why or not sources:
        return None, why
    kind, src = next(iter(sources))
    plan: dict[str, Any] = {"mode": "partitions", "kind": kind, "args": argnames, "fixed": {k: (t["expand"]["fixed"]) for k, t in mapped.items()}}
    if kind == "literal":
        plan["items"] = json.loads(src)
    else:
        p = tasks[src]
        if p.get("is_mapped") or p["type"] not in PY_TASKS or p["upstream"] or set(down.get(src, [])) != set(mapped):
            return None, [f"{src}: the producer of the mapped list must be a root Python task used only by the mapped tasks"]
        plan["producer"] = src
    return plan, []


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
    mplan, mwhy = plan_mapping(dag, tasks)
    reasons += mwhy
    if mplan and triggers:
        reasons.append("dynamic task mapping in an asset-triggered DAG")
    for t in tasks.values():
        ty, tid, tf = t["type"], t["task_id"], t["template_fields"]
        if t["is_mapped"] and not mplan and not mwhy:
            reasons.append(f"{tid}: dynamic task mapping")
        if (t["is_setup"] or t["is_teardown"]) and not (mplan and mplan["mode"] == "graph"):
            reasons.append(f"{tid}: setup/teardown (converted only in the graph form)")
        # without a branch or short-circuit nothing is ever skipped here, so these rules behave like all_success (a failure stops the
        # downstream task in Dagster, which is also what Airflow does under them)
        if t["trigger_rule"] != "all_success" and not (mplan and mplan["mode"] == "graph" and t["trigger_rule"] in GATE_RULES) \
                and t["trigger_rule"] not in PLAIN_RULES:
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
        cb_ok = ("on_failure_callback", "on_success_callback", "on_retry_callback")       # converted with @on_hooks (asset form only)
        unsupported = [c for c in cbs if c not in cb_ok or not (t.get("callback_fns") or {}).get(c)]
        if unsupported or (cbs and (mplan and mplan["mode"] == "graph")):
            reasons.append(f"{tid}: task callbacks or hooks {cbs}")
        if t.get("execution_timeout_s") and ty not in BASH | BASH_DEC:
            reasons.append(f"{tid}: execution_timeout on a non-shell task (a Python callable cannot be interrupted safely)")
        if ty in PY_TASKS and (t.get("template_fields") or {}).get("templates_dict"):
            reasons.append(f"{tid}: templates_dict (rendered templates passed to the callable) is not rebuilt")
        if len(t["outlets"]) > 1:
            reasons.append(f"{tid}: {len(t['outlets'])} outlets")
        if ty in TRIGGER:
            a = t.get("args") or {}
            if not a.get("wait_for_completion"):
                reasons.append(f"{tid}: TriggerDagRunOperator without wait_for_completion (a run launched without waiting is not available from inside a run)")
            for k in ("reset_dag_run", "trigger_run_id", "logical_date", "skip_when_already_exists", "fail_when_dag_is_paused"):
                if a.get(k):
                    reasons.append(f"{tid}: TriggerDagRunOperator with {k}")
            if not a.get("trigger_dag_id"):
                reasons.append(f"{tid}: TriggerDagRunOperator target not found")
        if ty in SENSORS:
            sn = t.get("sensor") or {}
            if sn.get("exponential_backoff") or sn.get("silent_fail") or sn.get("never_fail"):
                reasons.append(f"{tid}: a sensor with exponential_backoff, silent_fail or never_fail")
            if ty == "FileSensor" and (t.get("args") or {}).get("fs_conn_id", "fs_default") != "fs_default":
                reasons.append(f"{tid}: FileSensor with the connection {(t.get('args') or {}).get('fs_conn_id')!r} (its base path is not available)")
            if ty == "TimeSensor" and not sn.get("target_datetime"):
                reasons.append(f"{tid}: TimeSensor target time not found")
            if ty == "TimeDeltaSensor" and "delta" not in (t.get("args") or {}):
                reasons.append(f"{tid}: TimeDeltaSensor delta not found")
            if sn.get("soft_fail") and not (mplan and mplan["mode"] == "graph"):
                reasons.append(f"{tid}: a sensor with soft_fail (skip handling needs the graph form)")
        if ty not in ELIDED | PY_TASKS | BASH | BASH_DEC | S3_OPS | SNOWFLAKE_OPS | SENSORS | TRIGGER and not (mplan and mplan["mode"] == "graph" and ty in BRANCH | SHORT | CLOCK_BRANCH):
            reasons.append(f"{tid}: operator {ty}")
        _, jprob = jinja_vars(task_templates(t))
        if jprob:
            reasons.append(f"{tid}: {jprob}")
        if ty in BASH and isinstance(tf.get("bash_command"), str) and tf["bash_command"].rstrip().endswith((".sh", ".bash")) \
                and "bash_command" not in (t.get("template_files") or {}):
            reasons.append(f"{tid}: bash_command names a script file ({tf['bash_command'].strip()[:40]}) that was not found")
    if reasons:
        raise Defer("; ".join(dict.fromkeys(reasons)))
    pplan = mplan if mplan and mplan["mode"] == "partitions" else None
    gplan = mplan if mplan and mplan["mode"] == "graph" else None

    elided = set() if gplan else ELIDED       # in the graph form an EmptyOperator stays a step: its trigger rule is what joins skipped branches

    def eff_up(tid: str) -> list[str]:
        out: list[str] = []
        for u in tasks[tid]["upstream"]:
            out += eff_up(u) if tasks[u]["type"] in elided else [u]
        return list(dict.fromkeys(out))

    real = [t for t in dag["tasks"] if t["type"] not in elided]
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
    if pplan and pplan.get("producer"):
        consumed.add(pplan["producer"])                       # the producer returns the list the fan-out reads
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
        if ty in PY_TASKS | BASH_DEC | BRANCH | SHORT | SENSOR_CALL:
            code, sup = resolve_callable(t.get("callable"), shims_used, meta)
            resolved[tid] = (code, sup, meta)
        names, _ = jinja_vars(task_templates(t))
        if meta.get("needs_ctx") or names or ty == "TimeDeltaSensor" or (ty in CLOCK_BRANCH and (t.get("args") or {}).get("use_task_logical_date")):
            ctx_tasks.add(tid)
        if meta.get("needs_ti") or {"ti", "task_instance"} & names:
            ti_inputs[tid] = [u for u in ancestors(tid) if u in keyparts]      # xcom_pull may name any task that ran before
            consumed |= set(ti_inputs[tid])
    cb_code: dict[str, list[tuple[str, str]]] = {}                 # task -> [(kind, function name)] and the generated adapters
    cb_blocks: list[str] = []
    for t in real:
        for kind, fns in (t.get("callback_fns") or {}).items():
            for k, info in enumerate(fns):
                code, sup = resolve_callable(info, shims_used, {}, context_arg=True)
                for sline in sup:
                    support.setdefault(sline, len(support))
                fn_name = f"_af_cb_{safe(t['task_id'])}_{kind[3:-9]}_{k}"
                cb_blocks.append(re.sub(r"^def\s+\w+", f"def {fn_name}", code, count=1))
                cb_code.setdefault(t["task_id"], []).append((kind, fn_name))
    uses_ctx = bool(ctx_tasks)
    if gplan and ti_inputs:
        raise Defer("dynamic task mapping where a callable pulls XComs by task id (not converted yet)")
    if pplan and (uses_ctx or any(resolved[x][2].get("needs_ti") for x in pplan["args"] if x in resolved)):
        raise Defer("dynamic task mapping where a callable uses the task context or pulls XComs (not converted yet)")
    dag_params = dag.get("params") or {}
    graph_outs: list[str] = []

    def sensor_body(t: dict[str, Any], params: dict[str, str], ctxn: str | None, call_inner: str | None = None) -> str:
        """A sensor as a polling step: Airflow's default `poke` loop (check, give up after the timeout, sleep poke_interval)."""
        ty, tid, tf, a, sn = t["type"], t["task_id"], t["template_fields"], t.get("args") or {}, t.get("sensor") or {}
        tzn = tz_name(tt["timezone"]) if tt.get("timezone") else "UTC"
        setup = ""
        if ty in SENSOR_CALL:
            check = call_inner
        elif ty == "BashSensor":
            check = f"bash_sensor({expr(tf.get('bash_command'), params, ctxn)}, {tf.get('env')!r}, {a.get('retry_exit_code')!r})"
        elif ty == "FileSensor":
            check = f"file_sensor({expr(tf.get('filepath'), params, ctxn)}, {bool(a.get('recursive'))!r})"
        elif ty == "TimeSensor":
            import datetime as _d
            import zoneinfo
            target = _d.datetime.fromisoformat(sn["target_datetime"]).astimezone(zoneinfo.ZoneInfo(tzn)).time().isoformat()
            setup = f"    target = time_sensor_target({target!r}, {tzn!r})\n"
            check = "af_utcnow() > target"
        else:                                                          # TimeDeltaSensor
            d = a.get("delta")
            seconds = d["seconds"] if isinstance(d, dict) else int(d) * 60
            check = f"af_utcnow() > ctx['data_interval_end'] + af_timedelta(seconds={seconds!r})"
        return setup + f"    return af_poke(context, lambda: {check}, {sn.get('poke_interval', 60.0)!r}, {sn.get('timeout', 604800.0)!r}, {bool(sn.get('soft_fail'))!r})"

    def emit_graph() -> None:
        """One graph asset for the whole DAG: every task is an op, a mapped task a mapped op, a task after it gets the collected list."""
        order: list[str] = []
        seen_t: set[str] = set()

        def visit(x: str) -> None:
            if x in seen_t:
                return
            seen_t.add(x)
            for u in eff_up(x):
                visit(u)
            order.append(x)
        for t in real:
            visit(t["task_id"])
        mp = gplan["mapped"]
        fail_aware = any(tasks[x]["trigger_rule"] in GATE_RULES - PLAIN_RULES for x in order)     # a failure must not stop the steps that react to it
        idx = {tid: i for i, tid in enumerate(order)}
        ops: list[str] = []
        lines: list[str] = []
        outs: dict[str, str] = {}
        val: dict[str, str] = {}          # task -> variable holding its value (a collected list for a mapped task)
        dyn: dict[str, str] = {}          # mapped task -> variable holding its dynamic output
        for tid in order:
            t = tasks[tid]
            ty = t["type"]
            ups = [u for u in eff_up(tid) if u in keyparts]
            i = idx[tid]
            fname = safe("op_" + tid)
            deco = [f"name={safe(dag_id) + '__' + safe(tid)!r}"]
            if t["retries"] and not fail_aware:
                deco.append(f"retry_policy=dg.RetryPolicy(max_retries={t['retries']}, delay={int(t['retry_delay_s'] or 300)})")
            if t["pool"]:
                deco.append(f"pool={t['pool']!r}")
            elif f"{safe(dag_id)}__{safe(tid)}" in pool_limits:
                deco.append(f"pool={safe(dag_id) + '__' + safe(tid)!r}")
            m = mp.get(tid)
            src_tasks = {v[1] for v in m["srcs"].values() if v[0] == "task"} if m else set()
            extras = [u for u in ups if u not in src_tasks]
            params = {u: f"in_{k}" for k, u in enumerate(extras if m else ups)}
            ctxn = "ctx" if tid in ctx_tasks else None
            ctx_line = (f"    ctx = af_context(context, {dag_id!r}, {tid!r}, {dag_params!r}, "
                        "{" + ", ".join(f"{u!r}: {params[u]}" for u in params) + "})\n") if ctxn else ""
            sig = (["context: dg.OpExecutionContext"] + (["item"] if m else []) + list(params.values()))
            if m:
                single = len(m["srcs"]) == 1
                kwexpr = ({"kw": f"{{{list(m['srcs'])[0]!r}: item}}", "args": "{}", "kwargs": "dict(item)"}[m["mode"]] if single else "dict(item)")
                combo = f"    combo = {kwexpr}\n"
            if ty in SENSORS - SENSOR_CALL:
                body = ctx_line + sensor_body(t, params, ctxn)
            elif ty in PY_TASKS | BASH_DEC | BRANCH | SHORT | SENSOR_CALL:
                code, sup, meta = resolved[tid]
                for sline in sup:
                    support.setdefault(sline, len(support))
                pyname = "_af_" + safe(tid)
                ops.append(re.sub(r"^def\s+\w+", f"def {pyname}", code, count=1) + "\n")
                if m:
                    kw = f"{{**{m['fixed']!r}, **combo}}"
                    posl = "list(item)" if m["mode"] == "args" else "[]"
                    inner = (f"call_with_context({pyname}, {posl}, {kw}, ctx)" if meta.get("needs_ctx")
                             else f"{pyname}(*{posl}, **{kw})")
                else:
                    pos = [expr(a, params, ctxn) for a in (t.get("op_args") or [])]
                    kws = {k: expr(v, params, ctxn) for k, v in (t.get("op_kwargs") or {}).items()}
                    inner = (f"call_with_context({pyname}, [{', '.join(pos)}], {{{', '.join(f'{k!r}: {v}' for k, v in kws.items())}}}, ctx)"
                             if meta.get("needs_ctx") else f"{pyname}(" + ", ".join(pos + [f"{k}={v}" for k, v in kws.items()]) + ")")
                if ty in BASH_DEC:
                    body = ((combo if m else "") + ctx_line + f"    cmd = {inner}\n    if not isinstance(cmd, str) or not cmd.strip():\n"
                            "        raise dg.Failure(description='@task.bash must return a non-empty command string')\n"
                            f"    return run_shell(context, cmd, env={t['template_fields'].get('env')!r}, cwd={t['template_fields'].get('cwd')!r}, timeout={t.get('execution_timeout_s')!r}{', xcom_json=True' if AF_MAJOR >= 3 else ''})")
                elif ty in SENSOR_CALL:
                    body = ctx_line + sensor_body(t, params, ctxn, inner)
                elif ty in BRANCH:
                    body = ctx_line + f"    return BranchResult({inner})"
                elif ty in SHORT:
                    body = ctx_line + f"    return ShortCircuit(bool({inner}), hard={t.get('short_circuit_hard', True)!r})"
                else:
                    body = (combo if m else "") + ctx_line + f"    return {inner}"
            elif ty in ELIDED:
                body = "    return None"
            elif ty in CLOCK_BRANCH:
                a = t.get("args") or {}
                yes, no = a.get("follow_task_ids_if_true"), a.get("follow_task_ids_if_false")
                logical = bool(a.get("use_task_logical_date"))
                tzn = tz_name(tt["timezone"]) if tt.get("timezone") else "UTC"

                def lit(v):
                    if v is None:
                        return "None"
                    if isinstance(v, dict) and "datetime" in v:
                        return f"af_datetime.fromisoformat({v['datetime']!r})"
                    if isinstance(v, dict) and "time" in v:
                        return f"af_time.fromisoformat({v['time']!r})"
                    raise Defer(f"{tid}: a branch bound this converter cannot read ({v!r})")
                if ty == "BranchDateTimeOperator":
                    call = f"branch_datetime({'ctx' if logical else 'None'}, {lit(a.get('target_lower'))}, {lit(a.get('target_upper'))}, {yes!r}, {no!r}, {tzn!r})"
                else:
                    call = f"branch_weekday({'ctx' if logical else 'None'}, {a.get('week_day')!r}, {yes!r}, {no!r}, {tzn!r})"
                body = ctx_line + f"    return BranchResult({call})"
            else:                                                       # BashOperator
                if m:
                    part = m["partial"]
                    body = (combo + f"    cmd = combo.get('bash_command', {part.get('bash_command')!r})\n"
                            f"    if not isinstance(cmd, str) or '{{{{' in cmd:\n"
                            f"        raise dg.Failure(description='a mapped command is run as written; templates inside mapped values are not rendered')\n"
                            f"    return run_shell(context, cmd, env=combo.get('env', {part.get('env')!r}), cwd=combo.get('cwd', {part.get('cwd')!r}), "
                            f"timeout={t.get('execution_timeout_s')!r}{', xcom_json=True' if AF_MAJOR >= 3 else ''})")
                else:
                    cmd = (t.get("template_files") or {}).get("bash_command") or t["template_fields"].get("bash_command")
                    if not isinstance(cmd, str):
                        raise Defer(f"{tid}: bash_command is not a string")
                    env = t["template_fields"].get("env")
                    body = (ctx_line + f"    return run_shell(context, {expr(cmd, params, ctxn)}, env={expr(env, params, ctxn) if env else 'None'}, "
                            f"cwd={expr(t['template_fields'].get('cwd'), params, ctxn)}, timeout={t.get('execution_timeout_s')!r}{', xcom_json=True' if AF_MAJOR >= 3 else ''})")
            if ty in BASH | BASH_DEC and t.get("do_xcom_push") is False:      # nothing is pushed: the value downstream is None
                body = body.replace("return run_shell(", "run_shell(") + "\n    return None"
            prefix = ""
            if params:                                                  # skip semantics: branch choices, short-circuits and trigger rules
                names = list(params.values())
                setup_arg = (f", setup={[bool(tasks[u].get('is_setup')) for u in params]!r}" if t["trigger_rule"] == "all_done_setup_success" else "")
                prefix = (f"    skip = af_gate(context, {t['trigger_rule']!r}, [{', '.join(names)}], {tid!r}{setup_arg})\n    if skip is not None:\n        return skip\n"
                          f"    {', '.join(names)} = {', '.join(f'af_data({n})' for n in names)}\n")
            skips = ty in PY_TASKS | BASH_DEC | BRANCH | SHORT | SENSOR_CALL and resolved.get(tid, ("", [], {}))[2].get("skips")
            if skips and not fail_aware:
                inner_lines = "\n".join(("    " + ln if ln.strip() else ln) for ln in body.split("\n"))
                body = "    def _run():\n" + inner_lines + "\n    return af_skippable(context, _run)"
            if fail_aware:                                              # the body runs inside a guard that turns a failure into a value
                inner_lines = "\n".join(("    " + ln if ln.strip() else ln) for ln in body.split("\n"))
                body = ("    def _run():\n" + inner_lines +
                        f"\n    return af_guard(context, _run, {int(t['retries'] or 0)}, {int(t['retry_delay_s'] or 300)})")
            ops.append("@dg.op(" + ", ".join(deco) + ")\ndef " + fname + "(" + ", ".join(sig) + "):\n" + prefix + body + "\n")
            if m:                                                       # the fan-out op, then the mapped op
                lit_names = [n for n, v in m["srcs"].items() if v[0] == "lit"]
                srcs = list(m["srcs"].items())
                chain = len(srcs) == 1 and srcs[0][1][0] == "task" and tasks[srcs[0][1][1]].get("is_mapped")
                extra_call = "".join(f", {val[u]}" for u in extras)
                if chain:
                    dyn[tid] = f"d{i}"
                    lines.append(f"    d{i} = {dyn[srcs[0][1][1]]}.map(lambda item: {fname}(item{extra_call}))")
                else:
                    task_srcs = [(n, v[1]) for n, v in srcs if v[0] == "task"]
                    fan_params = [f"src_{k}" for k in range(len(task_srcs))]
                    lists, k = [], 0
                    for n, v in srcs:
                        if v[0] == "lit":
                            lists.append(repr(v[1]))
                        else:
                            lists.append(f"src_{k}")
                            k += 1
                    names = [n for n, _ in srcs]
                    item = "vals[0]" if len(srcs) == 1 else f"dict(zip({names!r}, vals))"
                    ops.append(f"@dg.op(name={safe(dag_id) + '__fan_' + safe(tid)!r}, out=dg.DynamicOut())\n"
                               f"def fan_{safe(tid)}({', '.join(fan_params)}):\n"
                               f"    lists = [{', '.join(lists)}]\n"
                               "    if any(isinstance(x, Skipped) for x in lists):\n"
                               "        return\n"
                               "    if not all(isinstance(x, (list, tuple)) for x in lists):\n"
                               "        raise dg.Failure(description='expected a list of items to map over')\n"
                               "    for n, vals in enumerate(itertools.product(*lists)):\n"
                               f"        yield dg.DynamicOutput({item}, mapping_key=f'i{{n}}')\n")
                    fan_args = ", ".join(val[u] for _, u in task_srcs)
                    dyn[tid] = f"d{i}"
                    lines.append(f"    d{i} = fan_{safe(tid)}({fan_args}).map(lambda item: {fname}(item{extra_call}))")
                ops.append(f"@dg.op(name={safe(dag_id) + '__collect_' + safe(tid)!r})\ndef collect_{safe(tid)}(context, items):\n"
                           "    items = list(items)\n    return items if items else af_skip(context, False)      # Airflow skips a mapped task that expands to nothing\n")
                lines.append(f"    v{i} = collect_{safe(tid)}(d{i}.collect())")
                val[tid] = f"v{i}"
            else:
                args = ", ".join(val[u] for u in ups)
                lines.append(f"    v{i} = {fname}({args})")
                val[tid] = f"v{i}"
            outs[tid] = f"o{i}"
        if fail_aware:                                                  # the run fails at the end if any task failed
            fin_params = [f"in_{k}" for k in range(len(order))]
            ops.append(f"@dg.op(name={safe(dag_id) + '__finish'!r})\ndef op_finish(context, {', '.join(fin_params)}):\n"
                       f"    af_finish(context, {{{', '.join(f'{tid!r}: in_{k}' for k, tid in enumerate(order))}}}"
                       f"{', ignore=' + repr(sorted(x for x in order if tasks[x].get('is_teardown') and not tasks[x].get('on_failure_fail_dagrun'))) if any(tasks[x].get('is_teardown') for x in order) else ''})\n")
            lines.append(f"    fin = op_finish({', '.join(val[x] for x in order)})")
            graph_outs.append("run_check")
        outs_decl = ",\n        ".join(
            f"{outs[tid]!r}: dg.AssetOut(key={keyparts[tid]!r}, kinds={{{('shell' if tasks[tid]['type'] in BASH | BASH_DEC else 'python')!r}}}, "
            f"metadata={{'airflow_dag': {dag_id!r}, 'airflow_task': {tid!r}, 'airflow_operator': {tasks[tid]['type']!r}}})" for tid in order if tid in keyparts)
        ret = "{" + ", ".join(f"{outs[tid]!r}: {val[tid]}" for tid in order if tid in keyparts) + (", 'o_run_check': fin" if fail_aware else "") + "}"
        if fail_aware:
            outs_decl += f",\n        'o_run_check': dg.AssetOut(key={[safe(dag_id), 'run_check']!r}, kinds={{'python'}}, description='Fails the run when any task failed (the tasks that react to a failure still ran).')"
        blocks.append("\n\n".join(ops) + f"\n\n@dg.graph_multi_asset(name={safe(dag_id) + '_graph'!r}, group_name={safe(dag_id)!r}, outs={{\n        {outs_decl},\n    }})\n"
                      f"def {safe(dag_id)}_graph():\n" + "\n".join(lines) + f"\n    return {ret}\n")
        notes.append("dynamic task mapping became dynamic outputs inside one graph asset: the whole DAG runs as one Dagster run, mapped tasks are mapped ops "
                     "and a task after them receives the collected list. Differences from Airflow: retries and pools apply per op; individual tasks cannot be "
                     "re-run on their own (the asset is the unit), and an empty list skips the mapped step and gives the next step an empty list.")

    if gplan:
        emit_graph()
    for t in ([] if gplan else real):
        tid = t["task_id"]
        ups = [u for u in eff_up(tid) if u in keyparts]
        refs = xcom_refs(t.get("op_args")) | xcom_refs(t.get("op_kwargs")) | xcom_refs(t["template_fields"])
        ordered = list(dict.fromkeys([*ups, *ti_inputs.get(tid, [])]))
        params = {u: f"in_{i}" for i, u in enumerate(ordered)}
        data = {u for u in ordered if u in refs or u in ti_inputs.get(tid, [])}
        if pplan and pplan["kind"] == "xcom" and tid in pplan["args"]:
            data.add(pplan["producer"])                       # the mapped task finds its item in the producer's list
        ctxn = "ctx" if tid in ctx_tasks else None
        ctx_line = (f"    ctx = af_context(context, {dag_id!r}, {tid!r}, {dag_params!r}, "
                    "{" + ", ".join(f"{u!r}: {params[u]}" for u in ordered if u in data) + "}"
                    + (", keys=" + repr({u: keyparts[u] for u in ti_inputs[tid]}) if ti_inputs.get(tid) else "") + ")\n") if ctxn else ""
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
        if pplan and tid in pplan["args"]:
            deco.append(f"partitions_def={safe(dag_id)}_items")
        if t["retries"]:
            deco.append(f"retry_policy=dg.RetryPolicy(max_retries={t['retries']}, delay={int(t['retry_delay_s'] or 300)})")
        if t["pool"]:
            deco.append(f"pool={t['pool']!r}")
        elif t.get("max_active_tis_per_dag") and f"{safe(dag_id)}__{safe(tid)}" in pool_limits:
            deco.append(f"pool={safe(dag_id) + '__' + safe(tid)!r}")
        deco.append(f"metadata={{'airflow_dag': {dag_id!r}, 'airflow_task': {tid!r}, 'airflow_operator': {t['type']!r}}}")
        fname = safe("t_" + tid)
        ty = t["type"]
        ret_none = tid not in consumed and not (RETURN_ALL and ty in PY_TASKS | BASH | BASH_DEC)
        if ty in BASH | BASH_DEC and t.get("do_xcom_push") is False:
            ret_none = True                                   # Airflow pushes nothing, so a downstream pull gets None
        if ty in BASH:
            cmd = (t.get("template_files") or {}).get("bash_command") or t["template_fields"].get("bash_command")
            if not isinstance(cmd, str):
                raise Defer(f"{tid}: bash_command is not a string")
            env = t["template_fields"].get("env")
            env_expr = expr(env, params, ctxn) if env else "None"
            body = (ctx_line + f"    {'' if ret_none else 'return '}run_shell(context, {expr(cmd, params, ctxn)}, env={env_expr}, "
                    f"cwd={expr(t['template_fields'].get('cwd'), params, ctxn)}, timeout={t.get('execution_timeout_s')!r}{', xcom_json=True' if AF_MAJOR >= 3 else ''})")
            deco.append("kinds={'shell'}")
            pre = ""
        elif ty in TRIGGER:
            conf = t["template_fields"].get("conf")
            body = ctx_line + f"    trigger_dag_run(context, __name__, {t['args']['trigger_dag_id']!r}, {expr(conf, params, ctxn) if conf else 'None'})"
            deco.append("kinds={'dagster'}")
            pre = ""
            ret_none = True
            notes.append(f"{tid} launches the job of DAG {t['args']['trigger_dag_id']!r} and waits for it (the DAG must be converted into the same package); its run fails if the launched run fails")
        elif ty in SENSORS - SENSOR_CALL:
            body = ctx_line + sensor_body(t, params, ctxn)
            deco.append("kinds={'sensor'}")
            pre = ""
            ret_none = False
        elif ty in PY_TASKS | BASH_DEC | SENSOR_CALL:
            code, sup, meta = resolved[tid]
            for sline in sup:
                support.setdefault(sline, len(support))
            pyname = "_af_" + safe(tid)
            code = re.sub(r"^def\s+\w+", f"def {pyname}", code, count=1)
            pos = [expr(a, params, ctxn) for a in (t.get("op_args") or [])]
            kws = {k: expr(v, params, ctxn) for k, v in (t.get("op_kwargs") or {}).items()}
            is_mapped_task = bool(pplan and tid in pplan["args"])
            if is_mapped_task:
                kws = {**{k: repr(v) for k, v in pplan["fixed"][tid].items()}, pplan["args"][tid]: "item"}
            is_producer = bool(pplan and pplan.get("producer") == tid)
            if meta.get("needs_ctx"):
                inner = f"call_with_context({pyname}, [{', '.join(pos)}], {{{', '.join(f'{k!r}: {v}' for k, v in kws.items())}}}, ctx)"
            else:
                inner = f"{pyname}(" + ", ".join(pos + [f"{k}={v}" for k, v in kws.items()]) + ")"
            pre = code + "\n\n\n"
            if ty in SENSOR_CALL:
                body = ctx_line + sensor_body(t, params, ctxn, inner)
                deco.append("kinds={'sensor'}")
                ret_none = False
            elif ty in BASH_DEC:
                body = (ctx_line + f"    cmd = {inner}\n    if not isinstance(cmd, str) or not cmd.strip():\n"
                        f"        raise dg.Failure(description='@task.bash must return a non-empty command string')\n"
                        f"    {'' if ret_none else 'return '}run_shell(context, cmd, env={t['template_fields'].get('env')!r}, cwd={t['template_fields'].get('cwd')!r}, timeout={t.get('execution_timeout_s')!r}{', xcom_json=True' if AF_MAJOR >= 3 else ''})")
                deco.append("kinds={'shell'}")
            elif is_mapped_task:
                src_items = f"{safe(dag_id)}_literal_items" if pplan["kind"] == "literal" else params[pplan["producer"]]
                body = (f"    item = decode_item(context.partition_key, {src_items})\n" + ctx_line +
                        (f"    return {inner}" if not ret_none else f"    {inner}"))
                deco.append("kinds={'python'}")
            elif is_producer:
                body = (ctx_line + f"    result = {inner}\n"
                        "    context.add_output_metadata({'airflow/map_keys': dg.MetadataValue.json(item_keys(result))})\n"
                        "    return result")
                deco.append("kinds={'python'}")
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
        sig = "(\n" + ",\n".join(args) + ",\n)" + (" -> None" if ret_none and tid not in consumed else "")
        hook_deco = ""
        if tid in cb_code:                                    # task callbacks through the registry's @on_hooks decorator
            fails = [fn for kind, fn in cb_code[tid] if kind == "on_failure_callback"]
            retries_cb = [fn for kind, fn in cb_code[tid] if kind == "on_retry_callback"]
            succ = [fn for kind, fn in cb_code[tid] if kind == "on_success_callback"]
            ad = f"_cb_{safe(tid)}"
            ctx_new = f"    ctx = af_context(context, {dag_id!r}, {tid!r}, {dag_params!r}, {{}})\n"
            adapters = ""
            kw = []
            if fails or retries_cb:
                adapters += f"def {ad}_failure(context, exc):\n{ctx_new}    ctx['exception'] = exc\n"
                if fails:
                    adapters += f"    if context.retry_number >= {int(t['retries'] or 0)}:          # on_failure_callback: after the last attempt only\n" + "".join(f"        {fn}(ctx)\n" for fn in fails)
                if retries_cb:
                    adapters += f"    if context.retry_number < {int(t['retries'] or 0)}:           # on_retry_callback: before another attempt\n" + "".join(f"        {fn}(ctx)\n" for fn in retries_cb)
                adapters += "\n\n"
                kw.append(f"on_failure=[f'{{__name__}}:{ad}_failure']")
            if succ:
                adapters += f"def {ad}_success(context, result):\n{ctx_new}" + "".join(f"    {fn}(ctx)\n" for fn in succ) + "\n\n"
                kw.append(f"on_success=[f'{{__name__}}:{ad}_success']")
            pre = pre + adapters
            hook_deco = "@on_hooks(" + ", ".join(kw) + ")\n"
        blocks.append(pre + "@dg.asset(\n    " + ",\n    ".join(deco) + ",\n)\n" + hook_deco + "def " + fname + sig + ":\n" + body + "\n")

    keys = [keyparts[t["task_id"]] for t in real] + ([[safe(dag_id), "run_check"]] if graph_outs else [])
    imports = (["import itertools"] if gplan else []) + ["import dagster as dg"] + (["from dagster_community_components import on_hooks"] if cb_code else []) + ["", "from ..runtime import *  # noqa: F401,F403"]
    mod = [f'"""Converted from Airflow DAG `{dag_id}` ({dag["file"]}) by convert_ir.py.', "", note, '"""', *imports, ""]
    if support:
        mod += [s for s, _ in sorted(support.items(), key=lambda kv: kv[1])] + [""]
    if cb_blocks:
        mod += ["\n\n".join(cb_blocks), ""]
    if pplan:
        pdef = (f"dg.StaticPartitionsDefinition({list(dict.fromkeys(item_key(i) for i in pplan['items']))!r})" if pplan["kind"] == "literal"
                else f"dg.DynamicPartitionsDefinition(name={safe(dag_id) + '_items'!r})")
        mod += [f"{safe(dag_id)}_items = {pdef}", ""]
        if pplan["kind"] == "literal":
            mod += [f"{safe(dag_id)}_literal_items = {pplan['items']!r}", ""]
        notes.append("dynamic task mapping became partitions: " + (
            "one static partition per list item. A run is requested for each at the schedule tick; the items are a fixed list, so a change to it is a code change."
            if pplan["kind"] == "literal" else
            f"one dynamic partition per item {pplan['producer']} returns. After each run of {pplan['producer']}, a sensor registers the new items as partitions "
            "and requests one run per item. Differences from Airflow: the items run as separate runs, so one failed item does not stop the others "
            "(Airflow also lets sibling mapped instances finish), but they no longer share a single DAG run, and dynamic partitions accumulate over time."))
    lm = local_modules(third_party(support), {t["callable"].get("file") for t in real if t.get("callable")})
    if lm:
        notes.append(f"the copied code imports the project's own module(s) {sorted(lm)}: copy them into the Dagster project (they are not pip packages)")
    if "S3Hook" in shims_used:
        notes.append("callables use S3Hook through a compatibility shim (default AWS credentials; the connection id is not applied)")
    if notes:
        mod[1:1] = ["", *[f"NOTE: {n}" for n in notes]]
    out = "\n".join(mod) + "\n\n" + "\n\n".join(blocks) + "\n"
    mapped_keys = [keyparts[x] for x in (pplan["args"] if pplan else [])]
    job_keys = [k for k in keys if k not in mapped_keys] if pplan and pplan["kind"] == "xcom" else keys
    pd_arg = f", partitions_def={safe(dag_id)}_items" if pplan and pplan["kind"] == "literal" else ""
    out += (f"\n{safe(dag_id)}_job = dg.define_asset_job({safe(dag_id)!r}, selection=[dg.AssetKey(k) for k in {job_keys!r}]{pd_arg}"
            + (f", tags={{'airflow/dag_id': {dag_id!r}}}" if max_runs else "")
            + (f", executor_def=dg.multiprocess_executor.configured({{'max_concurrent': {max_tasks}}})" if max_tasks else "") + ")\n")
    if pplan and pplan["kind"] == "xcom":
        out += (f"\n{safe(dag_id)}_items_job = dg.define_asset_job({safe(dag_id) + '_items'!r}, selection=[dg.AssetKey(k) for k in {mapped_keys!r}], "
                f"partitions_def={safe(dag_id)}_items)\n"
                f"\n\n@dg.asset_sensor(asset_key=dg.AssetKey({keyparts[pplan['producer']]!r}), job={safe(dag_id)}_items_job, "
                f"name={safe(dag_id) + '_fanout'!r}, default_status=dg.DefaultSensorStatus.RUNNING)\n"
                f"def {safe(dag_id)}_fanout(context, asset_event):\n"
                f"    return fan_out(context, asset_event, {safe(dag_id) + '_items'!r})\n")
    if pplan and pplan["kind"] == "literal" and tt["kind"] in ("cron_trigger", "cron_data_interval"):
        out += (f"\n\n@dg.schedule(name={safe(dag_id) + '_schedule'!r}, job={safe(dag_id)}_job, cron_schedule={tt['cron']!r}, "
                f"execution_timezone={tz_name(tt['timezone'])!r})\n"
                f"def {safe(dag_id)}_schedule(context):\n"
                f"    tick = context.scheduled_execution_time.isoformat()\n"
                f"    return [dg.RunRequest(run_key=f'{{tick}}:{{k}}', partition_key=k) for k in {safe(dag_id)}_items.get_partition_keys()]\n")
    elif tt["kind"] in ("cron_trigger", "cron_data_interval") and uses_ctx:
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
                 "resources": sorted(resources), "snowflake_resources": sorted(sf_resources), "shims": sorted(shims_used), "notes": notes, "local_modules": sorted(local_modules(third_party(support), {t["callable"].get("file") for t in real if t.get("callable")})),
                 "requirements": sorted((third_party(support) - local_modules(third_party(support), {t["callable"].get("file") for t in real if t.get("callable")})) | ({"croniter"} if uses_ctx and tt["kind"] == "cron_data_interval" else set()) | ({"dagster-community-components"} if cb_code else set())), "external": sorted("/".join(e) for e in external), "keys": keys,
                 "uses_context": uses_ctx, "mapping": ({"kind": pplan["kind"], "mapped": sorted(pplan["args"]), "producer": pplan.get("producer")} if pplan else
                             ({"kind": "graph", "mapped": sorted(gplan["mapped"])} if gplan else None)), "max_active_runs": max_runs, "max_active_tasks": max_tasks, "pools": pool_limits,
                 "task_keys": {t["task_id"]: "/".join(keyparts[t["task_id"]]) for t in real}}


RUNTIME = '''"""Helpers used by the converted DAGs. Each mirrors what the Airflow operator or hook does."""
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
    conf = json.loads(tags.get("airflow/conf", "{}"))
    run_id = run.run_id if run is not None else context.run_id        # context.run_id is deprecated in current Dagster
    ti = _TaskInstance(dag_id, task_id, run_id, getattr(context, "retry_number", 0) + 1, upstream, context, keys)
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


ITEM_SRC = '''
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
'''
GATE_SRC = '''
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


def af_skippable(context, run):
    """Run a step; a raised AirflowSkipException skips the step (and what depends on it under its trigger rule) instead of failing it."""
    try:
        return run()
    except AirflowSkipException:
        return af_skip(context, False)


def af_finish(context, values, ignore=()):
    """The last step of a failure-aware DAG: fail the run if any task failed (a failed teardown does not, unless it asks to)."""
    failed = []
    for name, v in values.items():
        if name in ignore:
            continue
        items = v if isinstance(v, list) else [v]
        failed += [f"{name}: {i.error}" for i in items if isinstance(i, Failed)]
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


def trigger_dag_run(context, calling_module, dag_id, conf=None):
    """TriggerDagRunOperator with wait_for_completion: launch the other converted DAG's job here and fail if its run fails."""
    from dagster._core.definitions.reconstruct import build_reconstructable_job
    name = "".join(c if c.isalnum() or c == "_" else "_" for c in dag_id)
    name = f"_{name}" if name[:1].isdigit() else name           # the module and the job of a converted DAG are named like this
    module = calling_module.rsplit(".", 1)[0] + "." + name
    job = build_reconstructable_job(__name__, "reconstruct_dag_job", reconstructable_kwargs={"module": module, "job_name": name})
    tags = {"airflow/triggered_by": context.run.run_id, **({"airflow/conf": json.dumps(conf)} if conf else {})}
    result = dg.execute_job(job, instance=context.instance, tags=tags)
    if not result.success:
        raise dg.Failure(description=f"the triggered DAG {dag_id!r} failed (run {result.run_id})", metadata={"triggered_run_id": result.run_id})


def af_data(x):
    """The value a downstream task sees: None for a skipped task, the chosen ids for a branch, the flag for a short-circuit."""
    if isinstance(x, (Skipped, Failed, UpstreamFailed)):
        return None
    if isinstance(x, BranchResult):
        return list(x.selected)
    if isinstance(x, ShortCircuit):
        return x.passed
    return x
'''
exec(ITEM_SRC)           # item_key is also used at conversion time, so the converter and the generated runtime share one definition
RUNTIME += ITEM_SRC + GATE_SRC


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ir", type=Path)
    ap.add_argument("--out-dir", type=Path, required=True, help="the project's defs directory (src/<pkg>/defs)")
    ap.add_argument("--report", type=Path, default=Path("conversion_report.json"))
    ap.add_argument("--mapping", choices=["graph", "partitions"], default="graph",
                    help="dynamic task mapping: dynamic outputs in one graph asset (default; keeps one run, reduce steps and all-or-nothing) "
                         "or partitions (leaf mapped tasks become independently runnable partitions)")
    ap.add_argument("--return-all", action="store_true", help="return every Python task's value (stored by the IO manager), for parity runs")
    a = ap.parse_args(argv)
    global RETURN_ALL, MAP_MODE, AF_MAJOR
    RETURN_ALL = a.return_all
    MAP_MODE = a.mapping
    ir = json.loads(a.ir.read_text())
    _m = re.match(r"(\d+)", str(ir.get("airflow_version") or ""))
    AF_MAJOR = int(_m.group(1)) if _m else 2
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
