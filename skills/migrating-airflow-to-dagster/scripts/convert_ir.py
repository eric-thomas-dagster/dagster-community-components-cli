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
ELIDED = {"EmptyOperator", "DummyOperator"}
PY_TASKS = {"PythonOperator", "_PythonDecoratedOperator", "DecoratedOperator"}
BASH = {"BashOperator"}
BASH_DEC = {"_BashDecoratedOperator"}
S3_OPS = {"S3ListOperator", "S3CopyObjectOperator", "S3DeleteObjectsOperator", "LocalFilesystemToS3Operator"}
SNOWFLAKE_OPS = {"CopyFromExternalStageToSnowflakeOperator", "SnowflakeOperator", "SQLExecuteQueryOperator"}


class Defer(Exception):
    pass


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


def resolve_callable(info: dict[str, Any] | None, shims: set[str] | None = None) -> tuple[str, list[str]]:
    """(function source without decorators, support blocks in source order). Raises Defer with the reason.

    `shims` collects compatibility shims the code relies on (names provided by the runtime module)."""
    shims = shims if shims is not None else set()
    if not info or "source" not in info:
        raise Defer(f"callable source unavailable ({(info or {}).get('source_error', 'no callable')})")
    if info.get("name") == "<lambda>" or info["source"].lstrip().startswith("lambda"):
        raise Defer("callable is a lambda")
    params = set(info.get("params") or [])
    if params & CONTEXT_PARAMS:
        raise Defer(f"callable uses Airflow task context parameters {sorted(params & CONTEXT_PARAMS)}")
    try:
        tree = ast.parse(info["source"])
    except SyntaxError as e:
        raise Defer(f"callable source does not parse: {e.msg}") from e
    fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if fn is None:
        raise Defer("callable is not a plain function")
    if isinstance(fn, ast.AsyncFunctionDef):
        raise Defer("async callable")
    body_text = ast.unparse(ast.Module(body=[*fn.body], type_ignores=[]))
    if re.search(r"\bairflow\b|\bti\.|get_current_context|\bVariable\b|\bxcom_(pull|push)\b", body_text):
        raise Defer("callable uses the Airflow API (context, Variable, xcom)")
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
                if isinstance(node, ast.ImportFrom) and mod == "airflow.providers.amazon.aws.hooks.s3" and all(a.name == "S3Hook" for a in node.names):
                    shims.add("S3Hook")                     # provided by the generated runtime module (star-imported)
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


def expr(v: Any, params: dict[str, str]) -> str:
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
        return "{" + ", ".join(f"{k!r}: {expr(x, params)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(expr(x, params) for x in v) + "]"
    return repr(v)


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


def snowflake_call(t: dict[str, Any]) -> tuple[str, str]:
    """(statement, snowflake connection id) for a supported Snowflake operator."""
    ty, tid = t["type"], t["task_id"]
    a = {**t["template_fields"], **(t.get("args") or {})}
    conn = a.get("snowflake_conn_id") or a.get("conn_id") or "snowflake_default"
    ov = {k: a.get(k) for k in ("warehouse", "database", "role", "schema") if a.get(k)}
    if ty == "CopyFromExternalStageToSnowflakeOperator":
        if any(a.get(k) for k in ("columns_array", "storage_integration", "authenticator", "session_parameters")):
            raise Defer(f"{tid}: COPY operator option not mapped")
        kw = {k: a.get(k) for k in ("table", "stage", "file_format", "schema", "prefix", "files", "pattern", "copy_options", "validation_mode")}
        if has_jinja(kw):
            raise Defer(f"{tid}: Jinja template")
        return f"snowflake_run(context, {conn!r}, {ov!r}, [copy_into_sql(**{kw!r})])", conn
    if ty == "SQLExecuteQueryOperator" and "snowflake" not in conn.lower():
        raise Defer(f"{tid}: SQLExecuteQueryOperator on connection {conn!r} (only Snowflake connections are mapped)")
    sql = (t.get("template_files") or {}).get("sql") or a.get("sql")
    if not isinstance(sql, (str, list)) or has_jinja(sql):
        raise Defer(f"{tid}: SQL is not a plain string (templated, parameterised or a callable)")
    if a.get("parameters"):
        raise Defer(f"{tid}: SQL parameters")
    return f"snowflake_run(context, {conn!r}, {ov!r}, {[sql] if isinstance(sql, str) else sql!r}, split=True)", conn


def and_condition(keys: list[list[str]]) -> str:
    """Fires only once EVERY upstream asset has updated since this asset was last handled (Airflow's schedule=[a, b])."""
    terms = [f"dg.AutomationCondition.asset_matches(dg.AssetKey({k!r}), dg.AutomationCondition.newly_updated()).since_last_handled()" for k in keys]
    return ("(" + " & ".join(terms) + " & ~dg.AutomationCondition.any_deps_missing() & ~dg.AutomationCondition.any_deps_in_progress() "
            "& ~dg.AutomationCondition.in_progress())")


def s3_call(t: dict[str, Any], params: dict[str, str]) -> tuple[str, str]:
    """(statement, aws connection id) for a supported S3 operator."""
    ty, f = t["type"], t["template_fields"]
    conn = f.get("aws_conn_id") or "aws_default"
    g = lambda k: expr(f.get(k), params)
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
    for k, default in (("max_active_tasks", 16), ("max_active_runs", 16), ("concurrency", 16)):
        v = ds.get(k)
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
        for flag in ("max_active_tis_per_dag", "max_active_tis_per_dagrun"):
            if t.get(flag):
                reasons.append(f"{tid}: {flag}")
        if t.get("pool_slots") not in (None, 1):
            reasons.append(f"{tid}: pool_slots={t['pool_slots']}")
        cbs = list(t.get("callbacks") or [])                    # pre/post_execute appear only when overridden (run_if, skip_if, hooks)
        if cbs:
            reasons.append(f"{tid}: task callbacks or hooks {cbs}")
        if t.get("execution_timeout_s") and ty not in BASH | BASH_DEC:
            reasons.append(f"{tid}: execution_timeout on a non-shell task (a Python callable cannot be interrupted safely)")
        if len(t["outlets"]) > 1:
            reasons.append(f"{tid}: {len(t['outlets'])} outlets")
        if ty not in ELIDED | PY_TASKS | BASH | BASH_DEC | S3_OPS | SNOWFLAKE_OPS:
            reasons.append(f"{tid}: operator {ty}")
        relevant = {k: tf.get(k) for k in (S3_FIELDS.get(ty) or ("bash_command", "env", "cwd"))} if ty not in PY_TASKS | SNOWFLAKE_OPS else {}
        if has_jinja(relevant) or has_jinja(t.get("op_args")) or has_jinja(t.get("op_kwargs")):
            reasons.append(f"{tid}: Jinja template")
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
    for t in real:
        tid = t["task_id"]
        ups = [u for u in eff_up(tid) if u in keyparts]
        params = {u: f"in_{i}" for i, u in enumerate(ups)}
        data = {u for u in ups if u in xcom_refs(t.get("op_args")) | xcom_refs(t.get("op_kwargs")) | xcom_refs(t["template_fields"])}
        args = ["    context: dg.AssetExecutionContext"] + [f"    {params[u]}" for u in ups if u in data]
        deco = [f"key={keyparts[tid]!r}", f"group_name={safe(dag_id)!r}"]
        if data:
            deco.append("ins={" + ", ".join(f"{params[u]!r}: dg.AssetIn(key={keyparts[u]!r})" for u in ups if u in data) + "}")
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
        deco.append(f"metadata={{'airflow_dag': {dag_id!r}, 'airflow_task': {tid!r}, 'airflow_operator': {t['type']!r}}}")
        fname = safe("t_" + tid)
        ty = t["type"]
        ret_none = tid not in consumed
        if ty in BASH:
            cmd = t["template_fields"].get("bash_command")
            if not isinstance(cmd, str):
                raise Defer(f"{tid}: bash_command is not a string")
            body = f"    run_shell(context, {cmd!r}, env={t['template_fields'].get('env')!r}, cwd={t['template_fields'].get('cwd')!r}, timeout={t.get('execution_timeout_s')!r})"
            deco.append("kinds={'shell'}")
            pre = ""
        elif ty in PY_TASKS | BASH_DEC:
            code, sup = resolve_callable(t.get("callable"), shims_used)
            for s in sup:
                support.setdefault(s, len(support))
            pyname = "_af_" + safe(tid)
            code = re.sub(r"^def\s+\w+", f"def {pyname}", code, count=1)
            call = ", ".join([expr(a, params) for a in (t.get("op_args") or [])] +
                             [f"{k}={expr(v, params)}" for k, v in (t.get("op_kwargs") or {}).items()])
            pre = code + "\n\n\n"
            if ty in BASH_DEC:
                body = (f"    cmd = {pyname}({call})\n    if not isinstance(cmd, str) or not cmd.strip():\n"
                        f"        raise dg.Failure(description='@task.bash must return a non-empty command string')\n"
                        f"    run_shell(context, cmd, env={t['template_fields'].get('env')!r}, cwd={t['template_fields'].get('cwd')!r}, timeout={t.get('execution_timeout_s')!r})")
                deco.append("kinds={'shell'}")
            else:
                body = f"    return {pyname}({call})" if not ret_none else f"    {pyname}({call})"
                deco.append("kinds={'python'}")
        elif ty in SNOWFLAKE_OPS:
            stmt, conn = snowflake_call(t)
            sf_resources.add(conn)
            deco.append(f"required_resource_keys={{{conn!r}}}")
            deco.append("kinds={'snowflake'}")
            body = f"    {stmt}"
            pre = ""
            ret_none = True
        else:
            stmt, conn = s3_call(t, params)
            resources.add(conn)
            deco.append(f"required_resource_keys={{{conn!r}}}")
            deco.append("kinds={'s3'}")
            body = f"    client = getattr(context.resources, {conn!r})     # dagster-aws attaches the boto client\n    {stmt}"
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
    out += f"\n{safe(dag_id)}_job = dg.define_asset_job({safe(dag_id)!r}, selection=[dg.AssetKey(k) for k in {keys!r}])\n"
    if tt["kind"] in ("cron_trigger", "cron_data_interval"):
        out += (f"\n{safe(dag_id)}_schedule = dg.ScheduleDefinition(name={safe(dag_id) + '_schedule'!r}, job={safe(dag_id)}_job, "
                f"cron_schedule={tt['cron']!r}, execution_timezone={tz_name(tt['timezone'])!r})\n")
    for k in own_keys:
        produced[k] = dag_id
    return out, {"assets": len(real), "elided": len(tasks) - len(real), "schedule": tt.get("cron"),
                 "timezone": tz_name(tt["timezone"]) if tt.get("cron") else None, "triggers": triggers,
                 "resources": sorted(resources), "snowflake_resources": sorted(sf_resources), "shims": sorted(shims_used), "notes": notes, "requirements": sorted(third_party(support)), "external": sorted("/".join(e) for e in external), "keys": keys,
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
    a = ap.parse_args(argv)
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
