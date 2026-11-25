#!/usr/bin/env python3
"""Convert the IR from airflow_ir.py into Dagster code, deterministically, for the constructs with a mechanical mapping.

    python3 convert_ir.py ir.json --out-dir <dagster project>/src/<pkg>/defs --report report.json

For each DAG it writes one module (assets, a job, a schedule or automation conditions), or DEFERS the whole DAG with every
reason when part of it cannot be expressed at all. By default (`--caveats raise`) a single task that cannot be translated is
written as a loud stub in its place (an asset whose body raises `AIRFLOW-CAVEAT: <reason>`), and a setting that has no equivalent
is a `degraded` caveat in the report; `--caveats off` defers the whole DAG instead of using either.

Converted
  tasks      PythonOperator / @task (callable source copied with the imports, constants and helpers it needs, Airflow's task
             context and Jinja templates rebuilt from run tags), BashOperator, @task.bash, EmptyOperator (elided, edges rewired),
             virtualenv / external-python operators (run in their own interpreter), user-defined operator classes (copied),
             callables using S3Hook (a mirrored subset), sensors (poll step), branches, short-circuits, clock branches,
             TriggerDagRunOperator (target in the same conversion), ExternalTaskSensor (some shapes),
             S3ListOperator, S3CopyObjectOperator, S3DeleteObjectsOperator (explicit keys), LocalFilesystemToS3Operator,
             S3CreateObjectOperator, CopyFromExternalStageToSnowflakeOperator, SnowflakeOperator / SQLExecuteQueryOperator
             on a Snowflake connection
  data       a value returned by one task and passed to another becomes an asset parameter (the IO manager carries it)
  lineage    a task with one outlet is the asset with that key; inlets become dependencies
  schedule   none, @once, cron (trigger or data-interval timetable) with timezone; asset triggers (single, any-of, all-of,
             nested); partition timetables; retries, retry delay, exponential backoff, pool, SLA
  structure  task groups become key prefixes; one asset per task, one job per DAG; dynamic task mapping, trigger rules,
             setup/teardown and callbacks are translated (graph form or `@on_hooks`)
Stubbed (loud, with the reason): a callable that uses the Airflow API or an unresolved import, a template the runtime does not
rebuild, an operator with no conversion yet (the report names the integration or registry component that fits), a mapped provider
operator outside the S3 and Snowflake ones, human-in-the-loop operators.
Deferred with the reason (whole DAG): producer conflicts, SubDAGs, schedules with no mapping, `catchup=True` (unless
`--catchup ignore`), a custom operator class that cannot be copied, and the mapped or partitioned shapes listed in
reference/conversion.md. Settings Dagster cannot enforce (`depends_on_past`, `pool_slots` above 1, `fail_stop`, ...) are `degraded`
caveats, or deferrals with `--caveats off`.
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
                              "prev_start_date_success", "prev_end_date_success", "loop", "asset_state_store"}
SUPPORTED_CTX = {"ds", "ds_nodash", "ts", "ts_nodash", "logical_date", "execution_date", "data_interval_start", "data_interval_end",
                 "run_id", "params", "dag", "task", "ti", "task_instance", "dag_run", "conf", "outlet_events", "inlet_events",
                 "next_ds", "next_ds_nodash", "next_execution_date", "prev_ds", "prev_ds_nodash", "prev_execution_date",     # Airflow 2 macros
                 "tomorrow_ds", "tomorrow_ds_nodash", "yesterday_ds", "yesterday_ds_nodash", "ts_nodash_with_tz",
                 "prev_data_interval_start_success", "prev_data_interval_end_success", "task_instance_key_str"}
JINJA_VARS = SUPPORTED_CTX | {"macros"}
CTX_ATTRS = {"dag_run": {"run_id", "conf", "logical_date", "execution_date", "dag_id", "partition_key", "partition_date"}, "dag": {"dag_id"}, "task": {"task_id"},
             "ti": {"dag_id", "task_id", "run_id", "try_number", "xcom_pull", "xcom_push"},
             "task_instance": {"dag_id", "task_id", "run_id", "try_number", "xcom_pull", "xcom_push"}}
SUPPORTED_MACROS = {"ds_add", "ds_format", "datetime", "timedelta"}
SHIM_IMPORTS = {("airflow.sdk", "LoopDecision"), ("airflow.models", "Variable"), ("airflow.sdk", "Variable"), ("airflow.models.variable", "Variable"),
                ("airflow.operators.python", "get_current_context"), ("airflow.sdk", "get_current_context"),
                ("airflow.providers.amazon.aws.hooks.s3", "S3Hook")} | \
    {(m, "BaseOperator") for m in ("airflow.models", "airflow.models.baseoperator", "airflow.sdk", "airflow.sdk.bases.operator")} | \
    {(m, n) for m in ("airflow.sdk", "airflow.datasets", "airflow.assets", "airflow.sdk.definitions.asset", "airflow.datasets.metadata") for n in ("Asset", "Dataset", "Metadata", "AssetAlias", "DatasetAlias")} | \
    {("airflow.utils.decorators", "apply_defaults")} | {(m, "Context") for m in ("airflow.utils.context", "airflow.sdk", "airflow.sdk.definitions.context")} | \
    {(m, n) for m in ("airflow.exceptions", "airflow.sdk.exceptions", "airflow.sdk") for n in ("AirflowException", "AirflowSkipException", "AirflowFailException")} | \
    {(m, "PokeReturnValue") for m in ("airflow.sensors.base", "airflow.sdk", "airflow.sdk.bases.sensor")}
ELIDED = {"EmptyOperator", "DummyOperator", "LatestOnlyOperator", "ExternalTaskMarker"}      # nothing to run; LatestOnly and ExternalTaskMarker have no effect once schedules do not backfill
ISOLATED = {"PythonVirtualenvOperator", "_PythonVirtualenvDecoratedOperator", "ExternalPythonOperator", "_PythonExternalDecoratedOperator"}   # a callable run in its own interpreter
PY_TASKS = {"PythonOperator", "_PythonDecoratedOperator", "DecoratedOperator", "_AssetMainOperator"} | ISOLATED      # _AssetMainOperator: an Airflow 3 @asset function
BASH = {"BashOperator"}
BASH_DEC = {"_BashDecoratedOperator"}
ISOLATED_BRANCH = {"BranchExternalPythonOperator", "_BranchExternalPythonDecoratedOperator", "BranchPythonVirtualenvOperator", "_BranchPythonVirtualenvDecoratedOperator"}
BRANCH = {"BranchPythonOperator", "_BranchPythonDecoratedOperator"} | ISOLATED_BRANCH
SHORT = {"ShortCircuitOperator", "_ShortCircuitDecoratedOperator"}
TRIGGER = {"TriggerDagRunOperator"}          # launches another converted DAG's job and waits for it (wait_for_completion=True)
SENSOR_CALL = {"PythonSensor", "DecoratedSensorOperator"}                                # sensors that poll a callable
SENSORS = SENSOR_CALL | {"BashSensor", "FileSensor", "TimeSensor", "TimeDeltaSensor"}   # converted as a polling step (Airflow's default `poke` behavior)
CLOCK_BRANCH = {"BranchDateTimeOperator", "BranchDayOfWeekOperator"}      # branch on the clock or the logical date: no callable
GATE_RULES = {"all_success", "all_failed", "all_done", "one_success", "one_failed", "one_done", "none_failed", "none_failed_min_one_success",
              "none_skipped", "all_skipped", "all_done_min_one_success", "always", "all_done_setup_success"}
PLAIN_RULES = {"all_success", "none_failed", "none_failed_min_one_success"}               # behave like all_success when a failure stops the run anyway
S3_OPS = {"S3ListOperator", "S3CopyObjectOperator", "S3DeleteObjectsOperator", "LocalFilesystemToS3Operator", "S3CreateObjectOperator"}
SNOWFLAKE_OPS = {"CopyFromExternalStageToSnowflakeOperator", "SnowflakeOperator", "SQLExecuteQueryOperator"}


class Defer(Exception):
    pass


OUTPUT = "python"     # --output python|components: generated Python, or component YAML (registry components) for the DAGs whose every task has a component
CAVEATS = "raise"    # --caveats off|raise|skip: what to do with a task whose callable or arguments cannot be translated (a marked stub instead of deferring the DAG)
DATASET_KEYS = "path"     # --dataset-keys path (s3://bucket/a/b -> s3/bucket/a/b) | script-orchestrator (airflow_dataset_bucket_a_b, the script_orchestrator component's naming)
CATCHUP = "defer"      # --catchup defer | ignore: what to do with catchup=True (Airflow runs every missed interval since start_date)
ALL_DAG_IDS: set[str] = set()
TASK_TYPES: dict[tuple[str, str], str] = {}
PARTITION_START = "2025-01-01"   # --partition-start: where partitioned assets begin (Airflow's partitions have no start; Dagster needs one)
TASK_KEYS: dict[tuple[str, str], list[str]] = {}    # (dag id, task id) -> the asset key of that task, for dependencies between DAGs (ExternalTaskSensor)
DAG_LEAVES: dict[str, list[list[str]]] = {}          # dag id -> the asset keys of its last tasks
ASSET_NAMES: dict[str, str] = {}      # an Airflow asset name -> its URI, so a reference by name (Asset.ref(name=...)) gets the same key as the producer's outlet
RUNTIME_PARTS: dict[str, str] = {}   # asset key -> dynamic partitions name, for assets whose producer chooses the partition key while it runs (PartitionedAtRuntime)
PARTITIONS: dict[str, dict] = {}   # asset key -> its time partitioning, from the partition timetables in the IR (filled by build_partition_catalog)
AF_MAJOR = 2             # the Airflow major version of the IR being converted (set from the IR): Airflow 3 parses a shell task's output as JSON
MAP_MODE = "graph"       # --mapping graph (dynamic outputs in one graph asset) | partitions (leaf mapped tasks as dynamic/static partitions)
RETURN_ALL = False        # set by --return-all: keep every Python return value so a parity run can compare them


def safe(name: str) -> str:
    s = re.sub(r"\W", "_", name)
    return f"_{s}" if s[:1].isdigit() else s


def key_from_asset(ref: str) -> list[str]:
    """Airflow asset/dataset name or URI -> Dagster key path (s3://bucket/a/b -> [s3, bucket, a, b]).
    With --dataset-keys script-orchestrator: one flat key named as the script_orchestrator component names it.
    A reference of the form `keyparts:a|b` is an asset key given directly (a task of another DAG)."""
    if ref.startswith("keyparts:"):
        return ref[len("keyparts:"):].split("|")
    ref = ASSET_NAMES.get(ref, ref)
    if DATASET_KEYS == "script-orchestrator":
        cleaned = re.sub(r"^[a-z]+://", "", ref)
        cleaned = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", cleaned)).strip("_")
        return [f"airflow_dataset_{cleaned}"]
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


def resolve_operator_class(info: dict[str, Any] | None, shims: set[str] | None = None, meta: dict[str, Any] | None = None) -> tuple[str, list[str]]:
    """The source of a user-defined operator class (an `execute` method on a BaseOperator subclass) and the module-level code it needs."""
    shims = shims if shims is not None else set()
    meta = meta if meta is not None else {}
    if not info or "source" not in info:
        raise Defer(f"operator class source unavailable ({(info or {}).get('source_error', 'no class')})")
    try:
        tree = ast.parse(info["source"])
    except SyntaxError as e:
        raise Defer(f"operator class source does not parse: {e.msg}") from e
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef)), None)
    methods = {m.name: m for m in (cls.body if cls else []) if isinstance(m, ast.FunctionDef)}
    if cls is None or "execute" not in methods:
        raise Defer("operator class without an execute method")
    inner_shims = [n for n in ast.walk(cls) if isinstance(n, ast.ImportFrom) and n.module and all((n.module, a.name) in SHIM_IMPORTS for a in n.names)]
    for n in inner_shims:
        shims.update(a.name for a in n.names)
    body_text = ast.unparse(ast.Module(body=[*cls.body], type_ignores=[]))
    for n in inner_shims:
        body_text = body_text.replace(ast.unparse(n), "")
    if re.search(r"\bairflow\b|\bset_xcom\b|\bxcom_push\b", body_text):
        raise Defer("operator class uses the Airflow API (a direct airflow reference)")
    meta["skips"] = "AirflowSkipException" in info["source"]
    idx = ModuleIndex(info["file"])
    class_level = {t.id for st in cls.body if isinstance(st, ast.Assign) for t in st.targets if isinstance(t, ast.Name)} | set(methods)
    needed: dict[int, str] = {}
    seen: set[str] = set()

    def need(names: set[str], where: str) -> None:
        for name in sorted(names):
            if name in seen or name in class_level:
                continue
            seen.add(name)
            if name == "af_loop":                                # provided by the generated runtime (a converted task-group loop)
                continue
            hit = idx.defs.get(name)
            if hit is None:
                raise Defer(f"unresolved name {name!r} in {where}")
            kind, node = hit
            if kind == "import":
                mod = getattr(node, "module", None) or (node.names[0].name if isinstance(node, ast.Import) else "")
                al = next((a for a in node.names if (a.asname or a.name) == name), None) if isinstance(node, ast.ImportFrom) else None
                if al is not None and (mod, al.name) in SHIM_IMPORTS:
                    shims.add(al.name)
                    if al.asname and al.asname != al.name:
                        needed[node.lineno] = f"{al.asname} = {al.name}"
                    continue
                if mod.split(".")[0] == "airflow":
                    raise Defer(f"operator class depends on an Airflow import ({mod})")
                needed[node.lineno] = idx.seg(node)
                continue
            if re.search(r"\bairflow\b", idx.seg(node)):
                raise Defer(f"{name!r} (used by the operator class) depends on Airflow")
            needed[node.lineno] = idx.seg(node)
            need(_free_names(node) if isinstance(node, ast.FunctionDef) else _loads(node), name)

    free = set()
    for m in methods.values():
        free |= _free_names(m)
    for st in cls.body:                                          # class-level statements and the base classes run in the module's scope
        if not isinstance(st, ast.FunctionDef):
            free |= _loads(st)
    for b in cls.bases:
        free |= _loads(b)
    need(free - {"BaseOperator", "apply_defaults", "Context", "super", "self"}, cls.name)
    for n in cls.bases:
        if not (isinstance(n, ast.Name) and n.id == "BaseOperator"):
            raise Defer(f"operator class extends {ast.unparse(n)}, not BaseOperator")
    lines = textwrap.dedent(info["source"]).splitlines()
    for n in inner_shims:
        indent = re.match(r"\s*", lines[n.lineno - 1]).group(0)
        lines[n.lineno - 1:n.end_lineno] = [indent + "pass"] + [""] * (n.end_lineno - n.lineno)
    start = next(i for i, l in enumerate(lines) if re.match(r"class\s", l))
    body_src = "\n".join(lines[start:])
    typing_used = sorted(n for n in ("Any", "Callable", "Dict", "Iterable", "List", "Mapping", "Optional", "Sequence", "Set", "Tuple", "Union")
                         if re.search(rf"\b{n}\b", body_src) and not any(re.search(rf"\b{n}\b", v) for v in needed.values()))
    if typing_used:                                              # annotations in the class body name typing classes the original module imported
        needed[-1] = "from typing import " + ", ".join(typing_used)
    return body_src, [needed[k] for k in sorted(needed)]


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
        meta["async"] = True                                     # run with asyncio.run (af_sync wraps it where the task is emitted)
    named = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]]
    if context_arg and named:
        named = named[1:]                                        # a callback's first parameter is the whole context dict, whatever it is called
    unsupported = sorted(set(named) & (KNOWN_CTX - SUPPORTED_CTX) - {"templates_dict"})
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
    meta["needs_ctx"] = bool(ctx_named) or bool(fn.args.kwarg) or "get_current_context" in body_text or bool(re.search(r"\bMetadata\b|\boutlet_events\b|\binlet_events\b", body_text))
    idx = ModuleIndex(info["file"])
    needed: dict[int, str] = {}
    seen: set[str] = set()

    def need(names: set[str], where: str) -> None:
        for name in sorted(names):
            if name in seen:
                continue
            seen.add(name)
            if name == "af_loop":                                # provided by the generated runtime (a converted task-group loop)
                continue
            hit = idx.defs.get(name)
            if hit is None:
                raise Defer(f"unresolved name {name!r} in {where}")
            kind, node = hit
            if kind == "import":
                mod = getattr(node, "module", None) or (node.names[0].name if isinstance(node, ast.Import) else "")
                al = next((a for a in node.names if (a.asname or a.name) == name), None) if isinstance(node, ast.ImportFrom) else None
                if al is not None and (mod, al.name) in SHIM_IMPORTS:       # this name is provided by the generated runtime module (star-imported)
                    shims.add(al.name)
                    if al.asname and al.asname != al.name:
                        needed[node.lineno] = f"{al.asname} = {al.name}"
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
    drop: list[Any] = []                                         # annotations naming something the module does not carry (an Airflow class): removed from the copy
    for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs, fn.args.vararg, fn.args.kwarg]:
        if a is not None and a.annotation is not None:
            drop.append((a, a.annotation))
    if fn.returns is not None:
        drop.append((None, fn.returns))
    bad_ann = []
    known_text = set(re.findall(r"\b\w+\b", "\n".join(needed.values())))
    for owner, node in drop:
        if any(isinstance(n, ast.Name) and not hasattr(builtins, n.id) and n.id not in known_text and n.id not in ("dg", "dagster")
               and n.id not in idx.defs or isinstance(n, ast.Name) and idx.defs.get(n.id, ("", None))[0] == "import" and n.id not in known_text and n.id not in shims
               for n in ast.walk(node)):
            bad_ann.append((owner, node))
    lines = textwrap.dedent(info["source"]).splitlines()
    for n in inner_shims:                                        # the import line goes; `pass` keeps the block valid
        indent = re.match(r"\s*", lines[n.lineno - 1]).group(0)
        lines[n.lineno - 1:n.end_lineno] = [indent + "pass"] + [""] * (n.end_lineno - n.lineno)
    for owner, node in sorted(bad_ann, key=lambda x: (x[1].lineno, x[1].col_offset), reverse=True):
        row = node.lineno - 1
        if owner is None:                                        # `-> T`
            m = re.search(r"\s*->\s*$", lines[row][:node.col_offset])
            lines[row] = lines[row][:m.start()] + lines[row][node.end_col_offset:] if m and node.end_lineno == node.lineno else lines[row]
        else:                                                    # `name: T`
            m = re.search(r"\s*:\s*$", lines[row][:node.col_offset])
            if m and node.end_lineno == node.lineno:
                lines[row] = lines[row][:m.start()] + lines[row][node.end_col_offset:]
    start = next(i for i, l in enumerate(lines) if re.match(r"(async\s+)?def\s", l))
    return "\n".join(lines[start:]), [needed[k] for k in sorted(needed)]


# --------------------------------------------------------------------------------------------------------------------
# helpers shared by the per-task handlers
# --------------------------------------------------------------------------------------------------------------------
def is_custom(t: dict[str, Any]) -> bool:
    """A user-defined operator (an `execute` method on a BaseOperator subclass in the project, not a provider's) that nothing else here maps."""
    known = ELIDED | PY_TASKS | BASH | BASH_DEC | BRANCH | SHORT | CLOCK_BRANCH | SENSORS | TRIGGER | S3_OPS | SNOWFLAKE_OPS | {"ExternalTaskSensor"}
    return bool(t.get("operator_class")) and t["type"] not in known and not t.get("is_mapped")


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


def delta_to_cron(seconds: float | None, start_date: str | None) -> tuple[str | None, str]:
    """A fixed interval (`timedelta`) as a cron string, when the interval divides the day and the start date is aligned to it: otherwise (None, why)."""
    if not seconds:
        return None, "interval not found"
    secs = int(seconds)
    t = (start_date or "")[11:19] or "00:00:00"
    hh, mm = int(t[0:2] or 0), int(t[3:5] or 0)
    if secs % 60 == 0 and secs < 3600 and 3600 % secs == 0:
        n = secs // 60
        return (f"*/{n} * * * *" if n > 1 else "* * * * *"), ""
    if secs % 3600 == 0 and secs < 86400 and 86400 % secs == 0:
        h = secs // 3600
        if mm == 0 and hh % h == 0:
            return (f"0 */{h} * * *" if h > 1 else "0 * * * *"), ""
        return None, "an hourly interval whose start date is not aligned to it"
    if secs == 86400:
        return f"{mm} {hh} * * *", ""
    if secs == 7 * 86400 and start_date:
        import datetime as _d
        try:
            dow = (_d.date.fromisoformat(start_date[:10]).isoweekday() % 7)
        except ValueError:
            return None, "start date not readable"
        return f"{mm} {hh} * * {dow}", ""
    return None, "an interval that is not a divisor of a day, a day or a week"


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
            if v["xcom_from"] not in params:
                raise Defer(f"XCom reference to {v['xcom_from']!r}, which is not an upstream task the converter can wire")
            if v.get("key") not in (None, "return_value"):         # a key of a task that returns a dict with multiple_outputs
                return f"af_xcom_key({params[v['xcom_from']]}, {v['key']!r})"
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
    "S3CreateObjectOperator": ("s3_bucket", "s3_key", "data", "replace", "encrypt", "acl_policy", "compression", "encoding", "aws_conn_id"),
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
    if t.get("operator_class"):
        return [t.get("args")]
    if ty == "BashSensor":
        return [tf.get("bash_command"), tf.get("env")]
    if ty == "FileSensor":
        return [tf.get("filepath")]
    if ty in PY_TASKS | BASH_DEC | BRANCH | SHORT | SENSOR_CALL:
        return [t.get("op_args"), t.get("op_kwargs"), tf.get("env") if ty in BASH_DEC else None, tf.get("templates_dict")]
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


def mapped_base(t: dict[str, Any]) -> dict[str, Any]:
    """The arguments of a mapped operator that are not expanded (Airflow's partial_kwargs), as plain values."""
    pk = (t.get("args") or {}).get("partial_kwargs") or {}
    skip = {"task_id", "owner", "retries", "retry_delay", "queue", "pool", "pool_slots", "trigger_rule", "depends_on_past", "email", "email_on_failure",
            "email_on_retry", "start_date", "end_date", "do_xcom_push", "executor_config", "weight_rule", "priority_weight", "allow_nested_operators",
            "wait_for_downstream", "retry_exponential_backoff", "max_retry_delay", "resources", "on_failure_callback", "on_success_callback", "on_retry_callback"}
    return {k: v for k, v in pk.items() if k not in skip and isinstance(v, (str, int, float, bool, list, dict, type(None)))}


def tree_condition(tree: Any) -> str:
    """An Airflow asset condition with nested AND/OR as a Dagster automation condition over the same assets."""
    def term(n: Any) -> str:
        if isinstance(n, str):
            return f"dg.AutomationCondition.asset_matches(dg.AssetKey({key_from_asset(n)!r}), dg.AutomationCondition.newly_updated()).since_last_handled()"
        return "(" + (" & " if n["op"] == "and" else " | ").join(term(x) for x in n["items"]) + ")"
    return f"({term(tree)} & ~dg.AutomationCondition.any_deps_in_progress() & ~dg.AutomationCondition.in_progress())"


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
    if ty == "S3CreateObjectOperator":
        if f.get("compression") or f.get("encrypt") or f.get("acl_policy"):
            raise Defer(f"{t['task_id']}: S3CreateObjectOperator compression/encrypt/acl")
        return f"s3_create_object(client, {g('s3_bucket')}, {g('s3_key')}, {g('data')}, {bool(f.get('replace'))!r}, {g('encoding')})", conn
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


# Operators the converter does not translate, and the registry components that cover the same work: named in the caveat so the stub can be replaced by hand.
OPERATOR_COMPONENTS: dict[str, str] = {
    "HITLOperator": "no equivalent: Dagster cannot pause a run for a person's choice",
    "HITLEntryOperator": "no equivalent: Dagster cannot pause a run for a person's choice",
    "HITLBranchOperator": "no equivalent: Dagster cannot pause a run for a person's choice",
    "ApprovalOperator": "no equivalent: Dagster cannot pause a run for a person's choice",
    "SageMakerTransformOperator": "aws_sagemaker",
    "SageMakerTrainingOperator": "aws_sagemaker",
    "SageMakerProcessingOperator": "aws_sagemaker",
    "S3ToRedshiftOperator": "aws_redshift",
    "RedshiftSQLOperator": "aws_redshift",
    "GlueJobOperator": "aws_glue",
    "BigQueryInsertJobOperator": "google_bigquery",
    "DataflowTemplatedJobStartOperator": "google_dataflow",
    "DatabricksSubmitRunOperator": "databricks_asset_bundle, databricks_resource",
    "DatabricksRunNowOperator": "databricks_asset_bundle, databricks_resource",
    "DbtCloudRunJobOperator": "dbt_cloud_trigger_job",
    "AirbyteTriggerSyncOperator": "airbyte_sync_trigger_job",
    "FivetranOperator": "fivetran_sync_trigger_job",
    "AzureDataFactoryRunPipelineOperator": "azure_data_factory",
    "SQLColumnCheckOperator": "enhanced_data_quality_checks (null_check, uniqueness_check, range_check as asset checks)",
    "SlackWebhookOperator": "slack_notification_job",
    "SlackAPIPostOperator": "slack_notification_job",
    "EmailOperator": "no email component; use a Dagster alert",
    "S3CreateObjectOperator": "s3_create_object_asset",
    "LoopGateOperator": "loop_gate_asset (Airflow 3.4 task-group loops: body callable, until callable, max_iterations)",
    "S3ListPrefixesOperator": "s3_list_keys_asset (common_prefixes metadata)",
    "KubernetesPodOperator": "k8s_job_asset",
}


# --------------------------------------------------------------------------------------------------------------------
# Per-DAG conversion
# --------------------------------------------------------------------------------------------------------------------
def cron_grain(expr: str) -> dict[str, Any] | None:
    """The partition grain of a cron string (hourly, daily, weekly, monthly), or None when the cadence is not one of those."""
    f = (expr or "").split()
    if len(f) != 5 or f[3] != "*":
        return None
    minute, hour, dom, _, dow = f
    isint = lambda x: x.isdigit()
    if isint(minute) and hour == "*" and dom == "*" and dow == "*":
        return {"grain": "hourly"}
    if isint(minute) and isint(hour) and dom == "*" and dow == "*":
        return {"grain": "daily"}
    if isint(minute) and isint(hour) and dom == "*" and isint(dow):
        return {"grain": "weekly", "offset": int(dow) % 7}
    if isint(minute) and isint(hour) and isint(dom) and dow == "*":
        return {"grain": "monthly", "offset": int(dom)}
    return None


GRAIN_ORDER = {"hourly": 0, "daily": 1, "weekly": 2, "monthly": 3}


def partitions_code(info: dict[str, Any]) -> str:
    """Dagster's own time partitions for a grain. `end_offset=1` includes the window that is running now: Airflow's run at a tick works on that tick's partition."""
    if info.get("dynamic"):
        return f"dg.DynamicPartitionsDefinition(name={info['dynamic']!r})"
    tz, start, g = info["tz"], info["start"], info["grain"]
    if g == "hourly" and len(start) == 10:
        start += "-00:00"                                # Dagster's hourly partitions take a start in the hourly key format
    cls = {"hourly": "HourlyPartitionsDefinition", "daily": "DailyPartitionsDefinition", "weekly": "WeeklyPartitionsDefinition", "monthly": "MonthlyPartitionsDefinition"}[g]
    extra = f", day_offset={info['offset']}" if g in ("weekly", "monthly") else ""
    return f"dg.{cls}(start_date={start!r}, timezone={tz!r}{extra}, end_offset=1)"


def mapper_kind(m: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """(class name, arguments) of a serialized Airflow partition mapper."""
    if not m:
        return "IdentityMapper", {}
    return str(m.get("__type", "")).rsplit(".", 1)[-1], m.get("__var") or {}


def same_partitions(info: dict[str, Any]) -> dict[str, Any]:
    """The partitioning without the firing cron: producers that fire at :00, :15 and :30 of every hour have the same hourly partitions."""
    return {k: v for k, v in info.items() if k not in ("cron", "via")}


def mapped_partitions(up: dict[str, Any] | None, mapper: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    """The partitions of a downstream asset for one upstream asset and mapper: (info, None) or (None, why it is not converted)."""
    kind, args = mapper_kind(mapper)
    if up is None:
        return None, "an upstream asset that is not a time-partitioned asset converted in this run"
    up = same_partitions(up)
    if kind == "IdentityMapper":
        return up, None
    if kind in ("RollupMapper", "FanOutMapper"):
        return rollup_or_fan_out(kind, args, up)
    coarser = {"StartOfHourMapper": "hourly", "StartOfDayMapper": "daily", "StartOfWeekMapper": "weekly", "StartOfMonthMapper": "monthly"}
    if kind in coarser:
        g = coarser[kind]
        if GRAIN_ORDER[g] < GRAIN_ORDER[up["grain"]]:
            return None, f"{kind} over {up['grain']} partitions (a finer grain than the upstream has)"
        return ({**up, "grain": g, "offset": 1 if g in ("weekly", "monthly") else 0} if g != up["grain"] else up), None
    return None, f"partition mapper {kind} (not converted yet)"


KEY_FORMATS = {"hourly": "%Y-%m-%d-%H:%M", "daily": "%Y-%m-%d", "weekly": "%Y-%m-%d", "monthly": "%Y-%m-%d"}


def rollup_or_fan_out(kind: str, args: dict[str, Any], up: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """RollupMapper (many upstream partitions -> one run) and FanOutMapper (one upstream partition -> many runs): the downstream partitions and the
    settings of the registry's partition_window_sensor, which does the waiting. Only time windows are converted here."""
    wk, wa = mapper_kind(args.get("window"))
    wt = {"DayWindow": "day", "WeekWindow": "week", "MonthWindow": "month"}.get(wk)
    if wt is None:
        return None, f"{kind} with {wk} (only day, week and month windows are converted)"
    rollup = kind == "RollupMapper"
    if rollup:
        need, down = {"day": ("hourly", "daily"), "week": ("daily", "weekly"), "month": ("daily", "monthly")}[wt]
    else:
        if wt == "month":
            return None, "FanOutMapper over a month window (the number of days varies)"
        need, down = {"day": ("daily", "hourly"), "week": ("weekly", "daily")}[wt]
    if up["grain"] != need:
        return None, f"{kind} over a {wt} window needs {need} upstream partitions, not {up['grain']}"
    via: dict[str, Any] = {"mode": "rollup" if rollup else "fan_out", "window_type": wt, "upstream_key_format": KEY_FORMATS[up["grain"]],
                           "downstream_key_format": KEY_FORMATS[down]}
    if rollup:
        pk, pa = mapper_kind(args.get("wait_policy"))
        if pk == "MinimumCount":
            via.update(wait_policy="minimum_count", minimum_count=int(pa.get("n")))
        elif pk in ("WaitForAll", "IdentityMapper"):
            via.update(wait_policy="all")
        else:
            return None, f"RollupMapper with the wait policy {pk}"
    else:
        via["max_downstream_keys"] = args.get("max_downstream_keys") or {"day": 24, "week": 7}[wt]
        via["direction"] = wa.get("direction") or "forward"
    info = {"grain": down, "tz": up["tz"], "start": up["start"], **({"offset": 1} if down in ("weekly", "monthly") else {}), "via": via}
    return info, None


def build_task_catalog(ir: dict[str, Any]) -> None:
    """The asset key every task will have, so that one DAG can depend on a task of another."""
    TASK_KEYS.clear()
    DAG_LEAVES.clear()
    for d in ir["dags"]:
        ups = {u for t in d["tasks"] for u in t["upstream"]}
        leaves = []
        for t in d["tasks"]:
            key = key_from_asset(t["outlets"][0]) if t.get("outlets") else [safe(d["dag_id"])] + [safe(x) for x in t["task_id"].split(".")]
            TASK_KEYS[(d["dag_id"], t["task_id"])] = key
            if t["task_id"] not in ups and t["type"] not in ELIDED:
                leaves.append(key)
        DAG_LEAVES[d["dag_id"]] = leaves


def external_sensor_targets(t: dict[str, Any], dag_ids: set[str], types: dict[tuple[str, str], str]) -> tuple[list[str], list[str]]:
    """(references to the assets an ExternalTaskSensor waits for, reasons it cannot be converted)."""
    a, tf = t.get("args") or {}, t.get("template_fields") or {}
    why: list[str] = []
    if t["upstream"]:
        why.append(f"{t['task_id']}: an ExternalTaskSensor that is not the first step of its DAG")
    if a.get("execution_delta") or a.get("execution_date_fn"):
        why.append(f"{t['task_id']}: an ExternalTaskSensor with an execution delta or date function (it waits for a run at another logical date)")
    if (a.get("allowed_states") or ["success"]) != ["success"] or a.get("failed_states") or a.get("check_existence") or a.get("external_task_group_id"):
        why.append(f"{t['task_id']}: an ExternalTaskSensor with custom states, a task group or check_existence")
    ext = a.get("external_dag_id") or tf.get("external_dag_id")
    if ext not in dag_ids:
        why.append(f"{t['task_id']}: the awaited DAG {ext!r} is not in this conversion")
    if why:
        return [], why
    ids = a.get("external_task_ids") or tf.get("external_task_ids") or ([tf["external_task_id"]] if tf.get("external_task_id") else [])
    keys = []
    for tid in ids:
        if (ext, tid) not in TASK_KEYS:
            return [], [f"{t['task_id']}: the awaited task {ext}.{tid} was not found"]
        if types.get((ext, tid)) in ELIDED:
            return [], [f"{t['task_id']}: the awaited task {ext}.{tid} is an empty operator (it produces nothing to depend on)"]
        keys.append(TASK_KEYS[(ext, tid)])
    if not ids:
        keys = list(DAG_LEAVES.get(ext, []))
        if not keys:
            return [], [f"{t['task_id']}: the awaited DAG {ext!r} has no tasks that produce assets"]
    return ["keyparts:" + "|".join(k) for k in keys], []


def build_partition_catalog(ir: dict[str, Any]) -> None:
    """Which assets are time-partitioned: the outlets of every DAG scheduled by a CronPartitionTimetable, then the assets of partition-triggered
    DAGs through their mappers (repeated until nothing new, so a chain of consumers resolves)."""
    PARTITIONS.clear()
    RUNTIME_PARTS.clear()
    ASSET_NAMES.clear()
    dags = ir["dags"]
    for d in dags:
        for t in d["tasks"]:
            for o in t.get("outlet_assets") or []:
                if o["name"] != o["uri"]:
                    ASSET_NAMES[o["name"]] = o["uri"]
    for d in dags:
        if d["timetable"].get("kind") == "partitioned_at_runtime":
            for t in d["tasks"]:
                for o in t.get("outlets") or []:
                    RUNTIME_PARTS["/".join(key_from_asset(o))] = "_".join(key_from_asset(o))
    for d in dags:
        tt = d["timetable"]
        if tt.get("kind") != "cron_partition":
            continue
        ser = (tt.get("partition") or {}).get("__var") or {}
        g = cron_grain(ser.get("expression", ""))
        if not g or ser.get("run_offset") not in (None, 0):
            continue
        info = {**g, "tz": ser.get("timezone") or "UTC", "start": PARTITION_START, "cron": ser.get("expression")}
        for t in d["tasks"]:
            for k in task_keys(d, t):
                PARTITIONS[k] = info
    for _ in range(4):
        for d in dags:
            tt = d["timetable"]
            if tt.get("kind") != "partitioned_asset":
                continue
            ups = [PARTITIONS.get("/".join(key_from_asset(a))) for a in tt.get("assets") or []]
            maps = [mapped_partitions(u, ((tt.get("partition") or {}).get("__var") or {}).get("default_partition_mapper")) for u in ups]
            infos = [m[0] for m in maps]
            if infos and all(i is not None for i in infos) and all(i == infos[0] for i in infos):
                for t in d["tasks"]:
                    for k in task_keys(d, t):
                        PARTITIONS.setdefault(k, same_partitions(infos[0]))


def task_keys(dag: dict[str, Any], t: dict[str, Any]) -> list[str]:
    """The asset key(s) a task produces, as strings: its outlets, or the DAG and task name."""
    if t.get("outlets"):
        return ["/".join(key_from_asset(o)) for o in t["outlets"]]
    return ["/".join([safe(dag["dag_id"])] + [safe(x) for x in t["task_id"].split(".")])]


def src_leaves(v: tuple[str, Any]) -> list[str]:
    """Task ids a mapped task's source reads (in order, once each)."""
    def walk(sp: Any) -> list[str]:
        k = sp[0]
        if k == "task":
            return [sp[1]]
        if k == "map":
            return walk(sp[1])
        if k in ("concat", "zip"):
            return [x for q in sp[1] for x in walk(q)]
        return []
    return list(dict.fromkeys(walk(v[1] if v[0] == "expr" else v)))


def source_spec(a: dict[str, Any], tasks: dict[str, Any], why: list[str], tid: str) -> tuple | None:
    """A `.map(f)`, `.concat(...)` or `.zip(...)` expand source as a tree: ('lit', list) ('task', id) ('map', spec, [callable]) ('concat', [spec]) ('zip', [spec], fill)."""
    if a.get("kind") == "literal" and isinstance(a.get("value"), list):
        return ("lit", a["value"])
    if a.get("kind") != "xcom":
        why.append(f"{tid}: mapping over a {a.get('kind')} source")
        return None
    w = a.get("wrapper")
    if w in ("PlainXComArg", "XComArg") and a.get("key") == "return_value" and a.get("task_id") in tasks:
        return ("task", a["task_id"])
    if w == "MapXComArg":
        inner = source_spec(a.get("inner") or {}, tasks, why, tid)
        fns = a.get("callables") or []
        if inner is None or not fns:
            return None
        return ("map", inner, fns)
    if w in ("ConcatXComArg", "ZipXComArg"):
        parts = [source_spec(x, tasks, why, tid) for x in a.get("parts") or []]
        if not parts or any(x is None for x in parts):
            return None
        return ("concat", parts) if w == "ConcatXComArg" else ("zip", parts, a.get("fillvalue"))
    why.append(f"{tid}: mapping over {w or a.get('kind')} (only a literal list, a task's return value, .map, .concat and .zip are converted)")
    return None


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
        if t["type"] not in ELIDED | PY_TASKS | BASH | BASH_DEC | BRANCH | SHORT | CLOCK_BRANCH | SENSORS | S3_OPS | SNOWFLAKE_OPS and not is_custom(t):
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
            elif a["kind"] == "xcom" and a.get("wrapper") in ("PlainXComArg", "XComArg") and a.get("key") == "return_value" and a.get("task_id") in tasks:
                srcs[name] = ("task", a["task_id"])
            elif (spec := source_spec(a, tasks, why, tid)) is not None:
                srcs[name] = ("expr", spec)
            elif a.get("wrapper") in ("MapXComArg", "ConcatXComArg", "ZipXComArg"):
                break                                                           # source_spec said why
            else:
                why.append(f"{tid}: mapping over {a['kind']}{':' + a['wrapper'] if a.get('wrapper') else ''} for `{name}` "
                           "(only a literal list or a task's plain return value is converted)")
                break
        else:
            mode = "kwargs" if e["form"] == "expand_kwargs" else ("args" if list(srcs) == ["op_args"] else "kw")
            if t["type"] in S3_OPS | SNOWFLAKE_OPS:
                bad = ["op_args"] if mode == "args" else []                  # the item's values are the operator's own arguments
            elif mode != "kw":
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
            elif len(srcs) > 1 and any(x in tasks and tasks[x].get("is_mapped") for v in srcs.values() for x in src_leaves(v)):
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
        elif a["kind"] == "xcom" and a.get("wrapper") in ("PlainXComArg", "XComArg") and a.get("key") == "return_value" and t["upstream"] == [a["task_id"]]:
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


def sla_wrap(body: str, t: dict[str, Any], key: str) -> str:
    """An Airflow `sla` is a deadline after the scheduled time; a miss is recorded and the task still runs. The step reports a breach as an observation on its asset."""
    if not t.get("sla_s"):
        return body
    inner = "\n".join(("    " + ln if ln.strip() else ln) for ln in body.split("\n"))
    return f"    try:\n{inner}\n    finally:\n        af_sla_check(context, {float(t['sla_s'])!r}, {key!r})"


COMPONENT_TASKS = {"BashOperator", "EmptyOperator", "DummyOperator", "S3CopyObjectOperator", "S3DeleteObjectsOperator", "S3ListOperator",
                   "LocalFilesystemToS3Operator", "S3CreateObjectOperator"} | {"PythonOperator", "_PythonDecoratedOperator"}


def _plain(v: Any) -> bool:
    """A value that can sit in component YAML as is: no template markers, nothing that refers to another task."""
    if isinstance(v, str):
        return "{{" not in v and "{%" not in v and "{#" not in v
    if isinstance(v, dict):
        return all(_plain(x) for x in v.values()) and not ({"xcom_from", "repr", "seconds", "datetime"} & set(v))
    if isinstance(v, (list, tuple)):
        return all(_plain(x) for x in v)
    return True


def component_files(dag: dict[str, Any], summary: dict[str, Any], pkg: str) -> tuple[dict[str, str], dict[str, Any]] | None:
    """The DAG as registry components, or None when some part of it has no component form (the caller then keeps the generated Python).

    Returns ({relative path: content}, summary overrides). One component per task in a `defs.yaml` (documents separated by `---`), a schedule
    component for the cron, and a module with the copied callables for Python tasks."""
    if summary.get("mapping") or summary.get("partitions") or summary.get("triggers") or summary.get("pools") or summary.get("max_active_runs") \
            or summary.get("run_timeout_s") or summary.get("caveats") or summary.get("uses_context") or summary.get("snowflake_resources"):
        return None
    tasks = {t["task_id"]: t for t in dag["tasks"]}
    dag_id, name = dag["dag_id"], safe(dag["dag_id"])
    if any(t["type"] not in COMPONENT_TASKS or t.get("is_mapped") or t["trigger_rule"] != "all_success" or t["outlets"] or t["inlets"] or t.get("sla_s")
           or t.get("is_setup") or t.get("is_teardown") or t.get("callback_fns") or t.get("callbacks") or t.get("pool")
           for t in tasks.values()):
        return None
    real = [t for t in tasks.values() if t["type"] not in ELIDED]
    if not real:
        return None
    consumed = {x for t in real for x in xcom_refs(t.get("op_args")) | xcom_refs(t.get("op_kwargs")) | xcom_refs(t["template_fields"])}
    if consumed:
        return None
    aname = {t["task_id"]: safe(f"{dag_id}__{t['task_id']}") for t in real}

    def eff_up(tid: str) -> list[str]:
        out: list[str] = []
        for u in tasks[tid]["upstream"]:
            out += eff_up(u) if tasks[u]["type"] in ELIDED else [u]
        return list(dict.fromkeys(out))

    docs: list[str] = []
    callables: list[str] = ["from ..runtime import *  # noqa: F401,F403", ""]
    support: dict[str, int] = {}

    def doc(typ: str, attrs: dict[str, Any]) -> None:
        docs.append(f"type: dagster_community_components.{typ}\nattributes:\n" + "\n".join(f"  {k}: {json.dumps(v)}" for k, v in attrs.items() if v is not None))

    for t in real:
        tid, ty, tf = t["task_id"], t["type"], t["template_fields"]
        a = t.get("args") or {}
        attrs: dict[str, Any] = {"asset_name": aname[tid], "group_name": name}
        ups = [aname[u] for u in eff_up(tid)]
        if ups:
            attrs["deps"] = ups
        if t["retries"]:
            attrs["retry_policy_max_retries"] = int(t["retries"])
            attrs["retry_policy_delay_seconds"] = int(t["retry_delay_s"] or 300)
            attrs["retry_policy_backoff"] = "exponential" if t.get("retry_exponential_backoff") else "linear"
        if ty == "BashOperator":
            cmd = (t.get("template_files") or {}).get("bash_command") or tf.get("bash_command")
            env, cwd = tf.get("env"), tf.get("cwd")
            if not isinstance(cmd, str) or not _plain([cmd, env, cwd]) or (env is not None and not isinstance(env, dict)):
                return None
            attrs.update(command=cmd, env_vars=env or None, cwd=cwd, skip_on_exit_code=99, timeout_seconds=int(t["execution_timeout_s"]) if t.get("execution_timeout_s") else None)
            doc("ShellCommandAssetComponent", attrs)
        elif ty in PY_TASKS:
            meta: dict[str, Any] = {}
            try:
                code, sup = resolve_callable(t.get("callable"), set(), meta)
            except Defer:
                return None
            if meta.get("needs_ctx") or meta.get("needs_ti") or meta.get("async") or not _plain([t.get("op_args"), t.get("op_kwargs")]):
                return None
            fn = f"task_{safe(tid)}"
            for line in sup:
                support.setdefault(line, len(support))
            callables.append(re.sub(r"^def\s+\w+", f"def {fn}", code, count=1) + "\n")
            attrs.pop("asset_name")
            attrs.update(asset_key=aname[tid], callable_path=f"{pkg}.callables.{name}:{fn}", args=t.get("op_args") or None, kwargs=t.get("op_kwargs") or None,
                         timeout_seconds=None)
            doc("PythonCallableAssetComponent", attrs)
        elif ty in S3_FIELDS:
            if not _plain(list(tf.values())):
                return None
            conn = tf.get("aws_conn_id") or "aws_default"
            attrs["resource_key"] = conn
            if ty == "S3ListOperator":
                if tf.get("apply_wildcard"):
                    return None
                attrs.update(bucket=tf.get("bucket"), prefix=tf.get("prefix") or None, delimiter=tf.get("delimiter") or None)
                doc("S3ListKeysAssetComponent", attrs)
            elif ty == "S3CopyObjectOperator":
                if any(tf.get(k) for k in ("acl_policy", "meta_data_directive", "kms_key_id", "kms_encryption_type", "source_version_id")):
                    return None
                if tf.get("source_bucket_name") or tf.get("dest_bucket_name"):
                    attrs.update(source_bucket=tf.get("source_bucket_name"), source_key=tf.get("source_bucket_key"), dest_bucket=tf.get("dest_bucket_name"), dest_key=tf.get("dest_bucket_key"))
                else:
                    attrs.update(source_s3_url=tf.get("source_bucket_key"), dest_s3_url=tf.get("dest_bucket_key"))
                doc("S3CopyObjectAssetComponent", attrs)
            elif ty == "S3DeleteObjectsOperator":
                if tf.get("prefix") or tf.get("from_datetime") or tf.get("to_datetime") or not isinstance(tf.get("keys"), (list, str)):
                    return None
                attrs.update(bucket=tf.get("bucket"), keys=[tf["keys"]] if isinstance(tf["keys"], str) else tf["keys"])
                doc("S3DeleteObjectsAssetComponent", attrs)
            elif ty == "S3CreateObjectOperator":
                if tf.get("compression") or tf.get("encrypt") or tf.get("acl_policy") or not isinstance(tf.get("data"), str) or tf.get("encoding"):
                    return None
                key = tf.get("s3_key")
                if tf.get("s3_bucket"):
                    attrs.update(bucket=tf["s3_bucket"], key=key)
                else:
                    attrs.update(dest_s3_url=key)
                attrs.update(data=tf["data"], replace=bool(tf.get("replace")))
                doc("S3CreateObjectAssetComponent", attrs)
            else:
                if tf.get("gzip") or tf.get("encrypt") or tf.get("acl_policy"):
                    return None
                attrs.update(local_path=tf.get("filename"), bucket=tf.get("dest_bucket"), key=tf.get("dest_key"), replace=bool(tf.get("replace")))
                doc("S3UploadFileAssetComponent", attrs)
        else:
            return None
    if summary.get("schedule"):
        doc("CronScheduleComponent", {"schedule_name": f"{name}_schedule", "job_name": name, "cron_expression": summary["schedule"],
                                      "asset_keys": [aname[t["task_id"]] for t in real], "execution_timezone": summary.get("timezone") or "UTC"})
    files = {f"defs/{name}/defs.yaml": "\n---\n".join(docs) + "\n"}
    if len(callables) > 2:
        files[f"callables/{name}.py"] = "\n".join([callables[0], "", *[s_ for s_, _ in sorted(support.items(), key=lambda kv: kv[1])], "", *callables[1:]])
    overrides = {"keys": [[aname[t["task_id"]]] for t in real], "task_keys": {t["task_id"]: aname[t["task_id"]] for t in real}, "output": "components",
                 "external": [], "notes": [*summary.get("notes", []), "registry components: one per task (asset keys are `<dag>__<task>`, single-part); "
                 "retries and timeouts are component settings" + ("; the DAG has no schedule so no job is defined" if not summary.get("schedule") else "")]}
    return files, overrides


def expand_loops(dag: dict[str, Any]) -> dict[str, Any]:
    """Airflow 3.4 task-group loops. A loop group (its Python tasks plus the gate) becomes ONE task: its callable runs the group's tasks in order once per
    iteration, handing each the `loop` object (index from 1, `previous` the preceding iteration's result), and consults the `until` callable after each.
    A group with a mapped, non-Python or externally fed task is left as it is (its gate then becomes a stub)."""
    gates = [t for t in dag["tasks"] if t["type"] == "LoopGateOperator" and (t.get("loop") or {}).get("group") and (t["loop"].get("max_iterations"))]
    if not gates:
        return dag
    tasks = list(dag["tasks"])
    for gate in gates:
        grp = gate["loop"]["group"]
        body = [t for t in tasks if t.get("group") == grp and t is not gate and t["type"] != "LoopGateOperator"]
        ids = {t["task_id"] for t in body}
        ok = bool(body) and all(t["type"] in PY_TASKS and not t.get("is_mapped") and isinstance(t.get("callable"), dict) and "source" in t["callable"] for t in body)
        refs_ok = all(r in ids for t in body for r in xcom_refs(t.get("op_args")) | xcom_refs(t.get("op_kwargs")))
        until = gate["loop"].get("until")
        if not (ok and refs_ok) or (until is not None and "source" not in until):
            continue
        order = [t["task_id"] for t in body]                                  # IR order is topological within a group
        down_in = {u for t in body for u in t["upstream"]}
        terminal = next((x for x in reversed(order) if x in set(gate["upstream"])), order[-1])
        names: dict[str, str] = {}
        src: list[str] = []

        def nested(info: dict[str, Any], hint: str) -> str:
            tree = ast.parse(textwrap.dedent(info["source"]))
            fn = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
            fn.decorator_list = []
            nm = f"{safe(hint)}_{len(names)}"
            names[nm] = nm
            fn.name = nm
            src.append(textwrap.indent(ast.unparse(fn), "    "))
            return nm
        fns = {t["task_id"]: nested(t["callable"], t["callable"].get("name") or t["task_id"]) for t in body}
        until_fn = nested(until, until.get("name") or "until") if until else None
        calls = []
        for t in body:
            tid = t["task_id"]

            def arg(v: Any) -> str:
                if isinstance(v, dict) and "xcom_from" in v:
                    return f"v[{v['xcom_from']!r}]"
                return repr(v)
            a = [arg(x) for x in (t.get("op_args") or [])] + [f"{k}={arg(x)}" for k, x in (t.get("op_kwargs") or {}).items()]
            if "loop" in (t["callable"].get("params") or []):
                a.append("loop=loop")
            calls.append(f"        v[{tid!r}] = {fns[tid]}({', '.join(a)})")
        outer = (f"def loop_{safe(grp)}():\n" + "\n".join(src) + "\n    def _body(loop):\n        v = {}\n" + "\n".join(calls) + f"\n        return v[{terminal!r}]\n"
                 f"    return af_loop(_body, {until_fn or 'None'}, {int(gate['loop']['max_iterations'])})\n")
        ext_up = sorted({u for t in body for u in t["upstream"] if u not in ids})
        synth = {**gate, "task_id": grp, "type": "PythonOperator", "group": None, "loop": None, "is_mapped": False, "expand": None,
                 "callable": {"name": f"loop_{safe(grp)}", "module": body[0]["callable"].get("module"), "file": body[0]["callable"].get("file"), "source": outer, "params": []},
                 "op_args": [], "op_kwargs": {}, "template_fields": {}, "upstream": ext_up, "retries": gate.get("retries") or 0, "outlets": [], "inlets": []}
        rename = {gate["task_id"]: grp, **{i: grp for i in ids}}
        out = []
        for t in tasks:
            if t is gate:
                out.append(synth)
            elif t["task_id"] in ids:
                continue
            else:
                ups = list(dict.fromkeys(rename.get(u, u) for u in t["upstream"]))
                out.append({**t, "upstream": ups} if ups != t["upstream"] else t)
        tasks = out
    return {**dag, "tasks": tasks}


def convert_dag(dag: dict[str, Any], note: str, produced: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """(module source, summary). Raises Defer if any part of the DAG cannot be converted."""
    reasons: list[str] = []
    caveats: list[dict[str, str]] = []                         # kind "stub": a task written as a marked stub; "degraded": translated with a known difference
    catchup_note = ""
    ext_trigs: list[str] = []

    def soften(msg: str, tid: str = "") -> None:
        """A setting that cannot be mapped: a degraded caveat (the DAG converts without it) unless --caveats off, which defers the DAG."""
        if CAVEATS != "off":
            caveats.append({"task": tid, "kind": "degraded", "reason": msg.split(": ", 1)[-1] if tid else msg})
        else:
            reasons.append(msg)

    ext_ids = [t["task_id"] for t in dag["tasks"] if t["type"] == "ExternalTaskSensor"]
    if ext_ids:                                              # waiting for another DAG's task becomes a dependency on that task's asset
        for t in dag["tasks"]:
            if t["task_id"] in ext_ids:
                refs, why = external_sensor_targets(t, ALL_DAG_IDS, TASK_TYPES)
                if why and CAVEATS != "off":                  # keep the step as an empty one in its place; the wait itself is dropped
                    caveats += [{"task": t["task_id"], "kind": "degraded", "reason": f"the wait for the other DAG was dropped (add a dependency or sensor by hand): {w.split(': ', 1)[-1]}"} for w in why]
                    refs, why = [], []
                ext_trigs += refs
                reasons += why
        dag = {**dag, "tasks": [({**t, "type": "EmptyOperator"} if t["task_id"] in ext_ids else t) for t in dag["tasks"]]}
    dag = expand_loops(dag)
    if CAVEATS != "off":                                     # tasks the converter cannot translate become marked stubs; the DAG keeps its shape
        soft_known = ELIDED | PY_TASKS | BASH | BASH_DEC | S3_OPS | SNOWFLAKE_OPS | SENSORS | TRIGGER | BRANCH | SHORT | CLOCK_BRANCH

        def soft_reason(t: dict[str, Any]) -> str | None:
            if t["type"] in TRIGGER:
                tgt = (t.get("args") or {}).get("trigger_dag_id")
                if isinstance(tgt, str) and "{{" not in tgt and tgt not in ALL_DAG_IDS:
                    return f"TriggerDagRunOperator target DAG {tgt!r} is not among the converted DAGs"
            if t["type"] not in soft_known and not is_custom(t):
                hint = OPERATOR_COMPONENTS.get(t["type"])
                return f"operator {t['type']} has no conversion yet" + (f" (registry: {hint})" if hint else "")
            if is_custom(t):                                   # a user-defined operator that cannot be copied or built from plain arguments
                if any(isinstance(v, dict) and "repr" in v for v in (t.get("args") or {}).values()):
                    return f"a constructor argument of {t['type']} that cannot be written as a plain value"
                try:
                    resolve_operator_class(t.get("operator_class"), set(), {})
                except Defer as e:
                    return str(e)
            if t["type"] in BASH and not t.get("is_mapped") and not isinstance(((t.get("template_files") or {}).get("bash_command") or (t.get("template_fields") or {}).get("bash_command")), str):
                return "bash_command is not a string (a callable or another object)"
            _, jprob = jinja_vars(task_templates(t))
            return jprob
        stubbed = {t["task_id"]: soft_reason(t) for t in dag["tasks"]}
        dag = {**dag, "tasks": [({**t, "type": "PythonOperator", "callable": None, "_caveat": stubbed[t["task_id"]], "is_mapped": False, "expand": None,
                                  "op_args": [], "op_kwargs": {}, "template_fields": {}, "callbacks": [], "callback_fns": {}, "execution_timeout_s": None,
                                  "operator_class": None}
                                 if stubbed.get(t["task_id"]) else t) for t in dag["tasks"]]}
    dag_id = dag["dag_id"]
    if dag.get("is_subdag"):
        raise Defer(f"a SubDAG of {dag.get('parent_dag_id')} (it runs inside the parent's SubDagOperator; SubDAGs were removed in Airflow 3: use a task group or a separate job)")
    tasks = {t["task_id"]: t for t in dag["tasks"]}
    tt = dag["timetable"]
    if tt.get("kind") == "delta":
        cron, why = delta_to_cron(tt.get("interval_seconds"), dag.get("start_date"))
        if cron:
            tt = {**tt, "kind": "cron_data_interval" if tt["class"] == "DeltaDataIntervalTimetable" else "cron_trigger", "cron": cron}
            catchup_note = (catchup_note + " " if catchup_note else "") + f"the interval schedule every {int(tt['interval_seconds'])} s is the cron `{cron}` (the interval divides the day and the start date is aligned)"
        else:
            reasons.append(f"schedule type {tt['class']} ({why})")
    triggers: list[str] = []
    rt_producer = False                                       # a PartitionedAtRuntime DAG: its outlets are dynamically partitioned assets
    trig_mode = "eager"
    trig_tree: Any = None
    ppart: dict[str, Any] | None = None                       # time partitions of this DAG's assets (Airflow 3.2+ partition timetables)
    if tt["kind"] in ("cron_trigger", "cron_data_interval", "manual", "once"):
        pass
    elif tt["kind"] == "cron_partition":
        ser = (tt.get("partition") or {}).get("__var") or {}
        grain = cron_grain(ser.get("expression", ""))
        if not grain:
            reasons.append(f"CronPartitionTimetable with the cron {ser.get('expression')!r} (only hourly, daily, weekly and monthly cadences are converted)")
        elif ser.get("run_offset") not in (None, 0):
            reasons.append("CronPartitionTimetable with a run_offset")
        else:
            ppart = {"role": "producer", "info": {**grain, "tz": ser.get("timezone") or "UTC", "start": PARTITION_START, "cron": ser["expression"]}}
    elif tt["kind"] in ("asset_triggered", "partitioned_asset"):
        leaves, logic = tt.get("assets") or [], tt.get("asset_logic")
        if tt["kind"] == "partitioned_asset":
            ser = (tt.get("partition") or {}).get("__var") or {}
            if ser.get("partition_mapper_config"):
                reasons.append("PartitionedAssetTimetable with a mapper per asset (not converted yet)")
            outs = [mapped_partitions(PARTITIONS.get("/".join(key_from_asset(a))), ser.get("default_partition_mapper")) for a in leaves]
            rt_names = {RUNTIME_PARTS.get("/".join(key_from_asset(a))) for a in leaves}
            if leaves and None not in rt_names and len(rt_names) == 1:           # the upstream's partition keys are chosen at run time
                if mapper_kind(ser.get("default_partition_mapper"))[0] != "IdentityMapper":
                    reasons.append(f"a runtime-partitioned upstream with the {mapper_kind(ser.get('default_partition_mapper'))[0]} (only the identity mapping is converted)")
                else:
                    ppart = {"role": "consumer", "info": {"dynamic": next(iter(rt_names)), "grain": "dynamic", "start": "", "tz": "UTC"}, "via": None}
            elif any(o[0] is None for o in outs):
                reasons.extend(dict.fromkeys(o[1] for o in outs if o[0] is None))
            elif any(same_partitions(o[0]) != same_partitions(outs[0][0]) for o in outs):
                reasons.append("upstream assets with different partitions")
            elif outs and outs[0][0].get("via") and len(leaves) > 1:
                reasons.append("a rollup or fan-out over several upstream assets (the sensor watches one)")
            elif outs:
                ppart = {"role": "consumer", "info": same_partitions(outs[0][0]), "via": outs[0][0].get("via")}
        if not leaves and CAVEATS != "off":
            caveats.append({"task": "", "kind": "degraded", "reason": "the DAG is scheduled on an asset alias that the extractor could not resolve to assets; it converts without a trigger (add an automation condition on the assets that feed the alias)"})
            tt = {**tt, "kind": "manual", "cron": None}
        elif not leaves:
            reasons.append("asset-triggered schedule with no assets in the IR")
        elif len(leaves) == 1 or logic == "any":
            triggers = list(leaves)                               # one asset, or any of several: eager() fires on any update
        elif logic == "all":
            triggers, trig_mode = list(leaves), "and"             # Airflow waits for all; verified against Dagster's evaluator
        elif tt.get("asset_tree"):
            triggers, trig_mode, trig_tree = list(leaves), "tree", tt["asset_tree"]     # nested AND/OR: written as the same combination of conditions
        else:
            reasons.append(f"schedule mixes AND and OR over {len(leaves)} assets")
    elif tt["kind"] == "partitioned_at_runtime":
        tt = {**tt, "kind": "manual", "cron": None}                  # launched on demand: the key comes from the launch or from the task
        rt_producer = True
    elif tt["class"] == "ContinuousTimetable" and CAVEATS != "off":
        tt = {**tt, "kind": "cron_trigger", "cron": "* * * * *"}
        caveats.append({"task": "", "kind": "degraded", "reason": "ContinuousTimetable (a new run as soon as the last finishes) became a schedule every minute; limit it to one run at a time with a run-queue tag limit"})
    elif tt["class"] == "AfterWorkdayTimetable" and CAVEATS != "off":
        tt = {**tt, "kind": "cron_data_interval", "cron": "0 0 * * 2-6"}
        caveats.append({"task": "", "kind": "degraded", "reason": "AfterWorkdayTimetable became the cron `0 0 * * 2-6` (each weekday's interval, run after it ends); holidays are not modeled"})
    else:
        reasons.append(f"schedule type {tt['class']} ({tt['kind']})")
    if ext_trigs and not reasons:
        if tt["kind"] in ("asset_triggered", "partitioned_asset"):
            triggers = list(dict.fromkeys([*triggers, *ext_trigs]))
        else:
            triggers = ext_trigs
        trig_mode = trig_mode if trig_mode == "tree" else ("and" if len(triggers) > 1 else "eager")
        if tt["kind"] not in ("asset_triggered", "partitioned_asset"):
            tt = {**tt, "kind": "manual", "cron": None}
        catchup_note = (catchup_note + " " if catchup_note else "") + (
            "the ExternalTaskSensor became a dependency on " + ", ".join("/".join(key_from_asset(x)) for x in ext_trigs) +
            ": this DAG's assets update when those do, and the DAG's own schedule no longer starts it (Airflow also waited for the same logical date, which is not matched)")
    if dag.get("has_callbacks"):
        if CAVEATS != "off":
            caveats.append({"task": "", "kind": "degraded", "reason": "DAG-level callbacks (on_failure_callback, on_success_callback, sla_miss_callback) were not converted; attach them with `@on_hooks` or a run-status sensor"})
        else:
            reasons.append("DAG-level callbacks")
    ds = dag.get("dag_settings") or {}
    if ds.get("fail_stop"):
        soften("fail_stop=True (Airflow cancels running tasks on the first failure; Dagster lets independent steps finish)")
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
    run_timeout = None
    dt_ = ds.get("dagrun_timeout")
    if dt_:
        if isinstance(dt_, dict) and dt_.get("seconds"):
            run_timeout = int(dt_["seconds"])                  # a tag Dagster's run monitoring reads: the deployment must enable run monitoring
        else:
            reasons.append("dagrun_timeout (not readable as a number of seconds)")
    if ds.get("max_consecutive_failed_dag_runs"):
        soften("max_consecutive_failed_dag_runs (auto-pause after failures is not mapped)")
    if ds.get("render_template_as_native_obj"):
        soften("render_template_as_native_obj (templates render to strings here, not to Python objects)")
    if dag.get("catchup") and tt["kind"] in ("cron_trigger", "cron_data_interval"):
        if CATCHUP == "ignore" or CAVEATS != "off":
            if CATCHUP != "ignore":
                caveats.append({"task": "", "kind": "degraded", "reason": "catchup=True: Airflow replays every interval since the start date; this schedule runs from now on (backfill history with Dagster partitions if needed)"})
            catchup_note = (f"catchup=True: Airflow runs every interval missed since the start date ({(dag.get('start_date') or 'none')[:10]}); the converted schedule "
                            "runs from now on. Replay history with a Dagster backfill if you need it")
        else:
            reasons.append("catchup=True needs partitions and a backfill (convert with --catchup ignore to run from now on)")
    mplan, mwhy = plan_mapping(dag, tasks)
    reasons += mwhy
    if mplan and triggers and mplan["mode"] != "graph":
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
        for flag, why in (("depends_on_past", "depends_on_past (a run waiting for the previous run's task) is not enforced"),
                          ("wait_for_downstream", "wait_for_downstream is not enforced")):
            if t.get(flag):
                soften(f"{tid}: {why}", tid)
        if t.get("max_active_tis_per_dagrun"):
            soften(f"{tid}: max_active_tis_per_dagrun is not enforced", tid)
        mtis = t.get("max_active_tis_per_dag")
        if mtis:
            if _pos(mtis) is None or t.get("pool"):
                reasons.append(f"{tid}: max_active_tis_per_dag={mtis}" + (" together with a named pool" if t.get("pool") else ""))
            else:
                pool_limits[f"{safe(dag_id)}__{safe(tid)}"] = mtis   # a Dagster pool of that size, shared by every run
        if t.get("pool"):
            pool_limits.setdefault(t["pool"], None)
        if t.get("pool_slots") not in (None, 1):
            soften(f"{tid}: pool_slots={t['pool_slots']} is not enforced (one slot is used)", tid)
        cbs = [c for c in (t.get("callbacks") or []) if not (t["is_mapped"] and c in ("pre_execute", "post_execute"))]   # a mapped wrapper overrides them; mapping already defers
        cb_ok = ("on_failure_callback", "on_success_callback", "on_retry_callback")       # converted with @on_hooks (asset form only)
        unsupported = [c for c in cbs if c not in cb_ok or not (t.get("callback_fns") or {}).get(c)]
        if unsupported or (cbs and (mplan and mplan["mode"] == "graph")):
            soften(f"{tid}: task callbacks or hooks {cbs} were not converted (attach them with `@on_hooks`)", tid)
            if CAVEATS != "off":
                t["callback_fns"] = {}
        if t.get("execution_timeout_s") and ty not in BASH | BASH_DEC:
            soften(f"{tid}: execution_timeout on a non-shell task is not enforced (a Python callable cannot be interrupted safely)", tid)
        if len(t["outlets"]) > 1:
            reasons.append(f"{tid}: {len(t['outlets'])} outlets")
        if ty in TRIGGER:
            a = t.get("args") or {}
            for k in ("reset_dag_run", "trigger_run_id", "logical_date", "skip_when_already_exists", "fail_when_dag_is_paused"):
                if a.get(k):
                    soften(f"{tid}: TriggerDagRunOperator {k} is not applied", tid)
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
        if ty not in ELIDED | PY_TASKS | BASH | BASH_DEC | S3_OPS | SNOWFLAKE_OPS | SENSORS | TRIGGER and not is_custom(t) and not (mplan and mplan["mode"] == "graph" and ty in BRANCH | SHORT | CLOCK_BRANCH):
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
    part_var = f"{safe(dag_id)}_partitions"
    if ppart and (mplan or any(t["type"] in BRANCH | SHORT | SENSORS | TRIGGER for t in tasks.values())):
        raise Defer("a partitioned DAG with mapped, branching, sensor or trigger steps (not converted yet)")

    elided = set() if gplan else ELIDED       # in the graph form an EmptyOperator stays a step: its trigger rule is what joins skipped branches

    def eff_up(tid: str) -> list[str]:
        out: list[str] = []
        for u in tasks[tid]["upstream"]:
            out += eff_up(u) if tasks[u]["type"] in elided else [u]
        return list(dict.fromkeys(out))

    real = [t for t in dag["tasks"] if t["type"] not in elided]
    if not real:
        raise Defer("only EmptyOperators: nothing to run")
    notes: list[str] = [catchup_note] if catchup_note else []
    if any(t["type"] == "LatestOnlyOperator" for t in tasks.values()):
        notes.append("LatestOnlyOperator is dropped: it skips runs that are not the latest, and a Dagster schedule does not catch up on missed ticks (a backfill you launch will run everything)")
    if any(t["type"] == "ExternalTaskMarker" for t in tasks.values()):
        notes.append("ExternalTaskMarker is dropped: it only declares which tasks to clear in another DAG")

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
    rt_outlets: dict[str, list[str]] = {}                    # task -> the key of the dynamically partitioned asset it records partitions of
    for t in real:
        keyparts[t["task_id"]] = key_from_asset(t["outlets"][0]) if t["outlets"] else [safe(dag_id)] + [safe(p) for p in t["task_id"].split(".")]
        if rt_producer and t["outlets"] and "/".join(keyparts[t["task_id"]]) in RUNTIME_PARTS:     # the step is its own asset; the partitioned asset it feeds is separate
            rt_outlets[t["task_id"]] = keyparts[t["task_id"]]
            keyparts[t["task_id"]] = [safe(dag_id)] + [safe(p) for p in t["task_id"].split(".")]
        k = "/".join(keyparts[t["task_id"]])
        if k in produced and produced[k] != dag_id:
            if CAVEATS == "off":
                raise Defer(f"{t['task_id']}: asset key {k} is already produced by DAG {produced[k]} (one producer per asset)")
            keyparts[t["task_id"]] = [*keyparts[t["task_id"]], "by_" + safe(dag_id)]      # a second producer gets its own asset under the first one's key
            caveats.append({"task": t["task_id"], "kind": "degraded", "reason": f"the asset {k} is also produced by DAG {produced[k]}; Dagster allows one producer per asset, so this DAG's output is {k}/by_{safe(dag_id)}"})
            k = "/".join(keyparts[t["task_id"]])
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
            if CAVEATS == "off":
                raise Defer(f"tasks {sorted(tids)} update the same asset {k} but are not ordered: one producer per asset, so the owner must choose")
            for tid in order[1:]:                                   # the first keeps the asset; each other writer gets its own asset under it
                keyparts[tid] = [*keyparts[tid], "by_" + safe(tid)]
            caveats.append({"task": "", "kind": "degraded", "reason": f"tasks {sorted(tids)} update the same asset {k} without an order; Dagster allows one producer per asset, so only {order[0]} keeps it and the others write {k}/by_<task>"})
            continue
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
        if t.get("_caveat"):
            caveats.append({"task": tid, "kind": "stub", "reason": t["_caveat"]})
            resolved[tid] = (f"def _stub(*args, **kwargs):\n    # AIRFLOW-CAVEAT: {t['_caveat'][:160]!r}\n    return af_caveat({t['_caveat'][:300]!r}, skip={CAVEATS == 'skip'})\n", [], {})
            continue
        try:
            if is_custom(t):
                if any(isinstance(v, dict) and "repr" in v for v in (t.get("args") or {}).values()):
                    raise Defer(f"{tid}: a constructor argument of {t['type']} that cannot be written as a plain value")
                code, sup = resolve_operator_class(t.get("operator_class"), shims_used, meta)
                resolved[tid] = (code, sup, meta)
                ctx_tasks.add(tid)
            elif ty in PY_TASKS | BASH_DEC | BRANCH | SHORT | SENSOR_CALL:
                code, sup = resolve_callable(t.get("callable"), shims_used, meta)
                resolved[tid] = (code, sup, meta)
        except Defer as e:
            if CAVEATS == "off" or is_custom(t):          # a custom operator class is built and called as a whole; no stub shape yet
                raise
            caveats.append({"task": tid, "kind": "stub", "reason": str(e)})
            ctx_tasks.discard(tid)
            resolved[tid] = (f"def _stub(*args, **kwargs):\n    # AIRFLOW-CAVEAT: {str(e)[:160]!r}\n    return af_caveat({str(e)[:300]!r}, skip={CAVEATS == 'skip'})\n", [], {})
        names, _ = jinja_vars(task_templates(t))
        if meta.get("needs_ctx") or names or ty == "TimeDeltaSensor" or (ty in CLOCK_BRANCH and (t.get("args") or {}).get("use_task_logical_date")):
            ctx_tasks.add(tid)
        if meta.get("needs_ti") or {"ti", "task_instance"} & names:
            ti_inputs[tid] = [u for u in ancestors(tid) if u in keyparts]      # xcom_pull may name any task that ran before
            consumed |= set(ti_inputs[tid])
    map_fns: dict[str, list[str]] = {}                                # mapped task -> names of its `.map(f)` functions, in source order
    map_blocks: list[str] = []
    for tid, m_ in ((gplan or {}).get("mapped") or {}).items():
        def collect(sp: Any, tid: str = tid) -> None:
            if sp[0] == "map":
                collect(sp[1])
                for info in sp[2]:
                    code, sup = resolve_callable(info, shims_used, {})
                    for sline in sup:
                        support.setdefault(sline, len(support))
                    nm = f"_af_map_{safe(tid)}_{len(map_fns.setdefault(tid, []))}"
                    map_fns[tid].append(nm)
                    map_blocks.append(re.sub(r"^def\s+\w+", f"def {nm}", code, count=1))
            elif sp[0] in ("concat", "zip"):
                for q in sp[1]:
                    collect(q)
        for _, v in m_["srcs"].items():
            if v[0] == "expr":
                collect(v[1])
    cb_code: dict[str, list[tuple[str, str]]] = {}                 # task -> [(kind, function name)] and the generated adapters
    cb_blocks: list[str] = []
    for t in real:
        for kind, fns in (t.get("callback_fns") or {}).items():
            for k, info in enumerate(fns):
                try:
                    code, sup = resolve_callable(info, shims_used, {}, context_arg=True)
                except Defer as e:
                    if CAVEATS == "off":
                        raise
                    caveats.append({"task": t["task_id"], "kind": "degraded", "reason": f"{kind} could not be copied and was dropped: {e}"})
                    continue
                for sline in sup:
                    support.setdefault(sline, len(support))
                fn_name = f"_af_cb_{safe(t['task_id'])}_{kind[3:-9]}_{k}"
                cb_blocks.append(re.sub(r"^def\s+\w+", f"def {fn_name}", code, count=1))
                cb_code.setdefault(t["task_id"], []).append((kind, fn_name))
    uses_ctx = bool(ctx_tasks)
    if gplan and any(ti_inputs.values()):
        if CAVEATS == "off":
            raise Defer("a graph-form DAG where a callable pulls XComs by task id (not converted yet)")
        for tid_, ups_ in list(ti_inputs.items()):                  # the tasks that pull by id become stubs; the rest of the DAG converts
            if ups_:
                reason_ = "the callable pulls XComs by task id (ti.xcom_pull), which a graph-form DAG does not rebuild yet"
                caveats.append({"task": tid_, "kind": "stub", "reason": reason_})
                resolved[tid_] = (f"def _stub(*args, **kwargs):\n    # AIRFLOW-CAVEAT: {reason_!r}\n    return af_caveat({reason_!r}, skip={CAVEATS == 'skip'})\n", [], {})
                ctx_tasks.discard(tid_)
                ti_inputs[tid_] = []
    if pplan and (uses_ctx or any(resolved[x][2].get("needs_ti") for x in pplan["args"] if x in resolved)):
        raise Defer("dynamic task mapping where a callable uses the task context or pulls XComs (not converted yet)")
    dag_params = dag.get("params") or {}
    graph_outs: list[str] = []

    def custom_body(t: dict[str, Any], ctx_line: str, pyname_support: bool = True) -> str:
        """A user-defined operator: its copied class, created with the operator's own arguments, then execute(context)."""
        code, sup, _ = resolved[t["task_id"]]
        for sline in sup:
            support.setdefault(sline, len(support))
        support.setdefault(code, len(support))
        args = {k: v for k, v in (t.get("args") or {}).items() if k not in ("task_id",)}
        return ctx_line + f"    op = {t['operator_class']['name']}(task_id={t['task_id']!r}, **{args!r})\n    return run_operator(op, ctx)"

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
                deco.append(f"retry_policy=dg.RetryPolicy(max_retries={t['retries']}, delay={int(t['retry_delay_s'] or 300)}{', backoff=dg.Backoff.EXPONENTIAL' if t.get('retry_exponential_backoff') else ''})")
            if t["pool"]:
                deco.append(f"pool={t['pool']!r}")
            elif f"{safe(dag_id)}__{safe(tid)}" in pool_limits:
                deco.append(f"pool={safe(dag_id) + '__' + safe(tid)!r}")
            m = mp.get(tid)
            src_tasks = {x for v in m["srcs"].values() for x in src_leaves(v)} if m else set()
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
            if is_custom(t):
                body = custom_body(t, ctx_line)
            elif ty in SENSORS - SENSOR_CALL:
                body = ctx_line + sensor_body(t, params, ctxn)
            elif ty in PY_TASKS | BASH_DEC | BRANCH | SHORT | SENSOR_CALL:
                code, sup, meta = resolved[tid]
                for sline in sup:
                    support.setdefault(sline, len(support))
                pyname = "_af_" + safe(tid)
                ops.append(re.sub(r"^(async\s+)?def\s+\w+", lambda mm: f"{mm.group(1) or ''}def {pyname}", code, count=1) + "\n")
                if meta.get("async"):
                    ops.append(f"{pyname} = af_sync({pyname})\n")
                if ty in ISOLATED | ISOLATED_BRANCH and not code.startswith("def _stub"):
                    ia, itf = t.get("args") or {}, t.get("template_fields") or {}
                    reqs = itf.get("requirements") if isinstance(itf, dict) else None
                    cfg = {"requirements": reqs if reqs else None, "python": itf.get("python") if isinstance(itf, dict) else None,
                           "python_version": ia.get("python_version"), "system_site_packages": bool(ia.get("system_site_packages")),
                           "pip_options": ia.get("pip_install_options"), "index_urls": ia.get("index_urls"), "env_vars": ia.get("env_vars"),
                           "inherit_env": ia.get("inherit_env", True), "support": list(sup)}
                    cfg = {k: v for k, v in cfg.items() if v not in (None, [], False) or k == "inherit_env"}
                    ops.append(f"{pyname} = af_isolated({pyname}, **{cfg!r})\n")
                    if cfg.get("python"):
                        notes.append(f"{tid}: runs with the interpreter {cfg['python']!r} from the Airflow DAG; point `python` at one that exists where Dagster runs")
                if m:
                    kw = f"{{**{m['fixed']!r}, **combo}}"
                    posl = "list(item)" if m["mode"] == "args" else "[]"
                    inner = (f"call_with_context({pyname}, {posl}, {kw}, ctx)" if meta.get("needs_ctx")
                             else f"{pyname}(*{posl}, **{kw})")
                else:
                    try:
                        pos = [expr(a, params, ctxn) for a in (t.get("op_args") or [])]
                        kws = {k: expr(v, params, ctxn) for k, v in (t.get("op_kwargs") or {}).items()}
                        if isinstance((t.get("template_fields") or {}).get("templates_dict"), dict) and ("templates_dict" in str((t.get("callable") or {}).get("params", "")) or "templates_dict" in str((t.get("callable") or {}).get("source", ""))):
                            kws["templates_dict"] = expr(t["template_fields"]["templates_dict"], params, ctxn)      # rendered templates handed to the callable
                        if "self" in ((t.get("callable") or {}).get("params") or []) and t.get("outlet_assets"):          # an @asset function receives its own asset
                            kws["self"] = "Asset(name=%r, uri=%r)" % (t["outlet_assets"][0].get("name"), t["outlet_assets"][0].get("uri"))
                        inner = (f"call_with_context({pyname}, [{', '.join(pos)}], {{{', '.join(f'{k!r}: {v}' for k, v in kws.items())}}}, ctx)"
                                 if meta.get("needs_ctx") else f"{pyname}(" + ", ".join(pos + [f"{k}={v}" for k, v in kws.items()]) + ")")
                    except Defer as e:
                        if CAVEATS == "off":
                            raise
                        caveats.append({"task": tid, "kind": "stub", "reason": str(e)})
                        inner = f"af_caveat({str(e)[:300]!r}, skip={CAVEATS == 'skip'})"      # AIRFLOW-CAVEAT: the call's arguments could not be wired
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
            elif ty in S3_OPS and m:                                        # a mapped provider operator: the fixed arguments merged with the item's, at run time
                conn = (t.get("args") or {}).get("partial_kwargs", {}).get("aws_conn_id") or "aws_default"
                resources.add(conn)
                deco.append(f"required_resource_keys={{{conn!r}}}")
                body = (combo + ctx_line + f"    client = getattr(context.resources, {conn!r})\n"
                        f"    return af_s3_mapped(client, {ty!r}, {{**{mapped_base(t)!r}, **combo}})")
            elif ty in SNOWFLAKE_OPS and m:
                base = mapped_base(t)
                conn = base.get("snowflake_conn_id") or base.get("conn_id") or "snowflake_default"
                ov = {k: base.get(k) for k in ("warehouse", "database", "role", "schema") if base.get(k)}
                sf_resources.add(conn)
                deco.append(f"required_resource_keys={{{conn!r}}}")
                body = combo + ctx_line + f"    af_snowflake_mapped(context, {ty!r}, {conn!r}, {ov!r}, {{**{base!r}, **combo}})\n    return None"
            elif ty in S3_OPS:
                stmt, conn = s3_call(t, params, ctxn)
                resources.add(conn)
                deco.append(f"required_resource_keys={{{conn!r}}}")
                body = ctx_line + f"    client = getattr(context.resources, {conn!r})     # dagster-aws attaches the boto client\n    " + (stmt if stmt.startswith("return") else f"{stmt}\n    return None")
            elif ty in SNOWFLAKE_OPS:
                stmt, conn = snowflake_call(t, ctxn)
                sf_resources.add(conn)
                deco.append(f"required_resource_keys={{{conn!r}}}")
                body = ctx_line + f"    {stmt}\n    return None"
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
            body = sla_wrap(body, t, "/".join(keyparts[tid]))
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
                    leaf_ids = list(dict.fromkeys(x for _, v in srcs for x in src_leaves(v)))
                    task_srcs = [(None, x) for x in leaf_ids]
                    fan_params = [f"src_{k}" for k in range(len(leaf_ids))]
                    pname = {x: f"src_{k}" for k, x in enumerate(leaf_ids)}
                    fn_names = map_fns.get(tid, [])
                    counter = iter(range(10 ** 6))

                    def spec_expr(sp: Any) -> str:
                        if sp[0] == "lit":
                            return repr(sp[1])
                        if sp[0] == "task":
                            return pname[sp[1]]
                        if sp[0] == "map":
                            inner_expr = spec_expr(sp[1])                      # numbered in the same order the functions were collected
                            chain = [fn_names[next(counter)] for _ in sp[2]]
                            return f"af_map_items([{', '.join(chain)}], {inner_expr})"
                        if sp[0] == "concat":
                            return "[" + ", ".join(f"*{spec_expr(q)}" for q in sp[1]) + "]"
                        parts = ", ".join(spec_expr(q) for q in sp[1])
                        return f"list(itertools.zip_longest({parts}, fillvalue={sp[2]!r}))" if sp[2] is not None else f"list(zip({parts}))"

                    lists = [repr(v[1]) if v[0] == "lit" else pname[v[1]] if v[0] == "task" else spec_expr(v[1]) for _, v in srcs]
                    guard = ", ".join(fan_params)
                    names = [n for n, _ in srcs]
                    item = "vals[0]" if len(srcs) == 1 else f"dict(zip({names!r}, vals))"
                    ops.append(f"@dg.op(name={safe(dag_id) + '__fan_' + safe(tid)!r}, out=dg.DynamicOut())\n"
                               f"def fan_{safe(tid)}({', '.join(fan_params)}):\n"
                               f"    srcs = [{guard}]\n"
                               "    if any(isinstance(x, Skipped) for x in srcs):\n"
                               "        return\n"
                               "    if not all(isinstance(x, (list, tuple)) for x in srcs):\n"
                               "        raise dg.Failure(description='expected a list of items to map over')\n"
                               f"    lists = [{', '.join(lists)}]\n"
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
        def is_effective_leaf(x: str) -> bool:                          # Airflow's DagRun._tis_for_dagrun_state: teardowns do not decide the run unless they ask to
            for y in order:
                if x in tasks[y]["upstream"] and (not tasks[y].get("is_teardown") or tasks[y].get("on_failure_fail_dagrun")):
                    return False
            return not tasks[x].get("is_teardown") or bool(tasks[x].get("on_failure_fail_dagrun"))
        leaf_ids = {x for x in order if is_effective_leaf(x)} or {x for x in order if not any(x in tasks[y]["upstream"] for y in order)}
        if fail_aware:                                                  # the run fails at the end if a leaf task failed
            fin_params = [f"in_{k}" for k in range(len(order))]
            ops.append(f"@dg.op(name={safe(dag_id) + '__finish'!r})\ndef op_finish(context, {', '.join(fin_params)}):\n"
                       f"    af_finish(context, {{{', '.join(f'{tid!r}: in_{k}' for k, tid in enumerate(order))}}}, leaves={sorted(leaf_ids)!r})\n")
            lines.append(f"    fin = op_finish({', '.join(val[x] for x in order)})")
            graph_outs.append("run_check")
        trig_cond = ""
        if triggers:                                                    # the whole graph runs when the triggering assets update (no per-task dependencies in a graph asset)
            keys_ = [key_from_asset(a) for a in triggers]
            for k_ in keys_:
                if "/".join(k_) not in own_keys:
                    external.add(tuple(k_))
            terms_ = [f"dg.AutomationCondition.asset_matches(dg.AssetKey({k_!r}), dg.AutomationCondition.newly_updated()).since_last_handled()" for k_ in keys_]
            trig_cond = (tree_condition(trig_tree) if trig_mode == "tree" else
                         "(" + (" & " if trig_mode == "and" else " | ").join(terms_) + " & ~dg.AutomationCondition.any_deps_in_progress() & ~dg.AutomationCondition.in_progress())")
            notes.append("the graph asset is triggered by an automation condition on " + ", ".join("/".join(k_) for k_ in keys_)
                         + " (a graph asset cannot declare dependencies, so the lineage edge from those assets is not drawn)")
        outs_decl = ",\n        ".join(
            f"{outs[tid]!r}: dg.AssetOut(key={keyparts[tid]!r}, kinds={{{('shell' if tasks[tid]['type'] in BASH | BASH_DEC else 'python')!r}}}, "
            + (f"automation_condition={trig_cond}, " if trig_cond else "") +
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
        if ppart and ppart.get("via"):
            pass                                              # the partition window sensor requests this asset's runs
        elif triggers and trig_mode == "tree" and tid in roots:
            deco.append("automation_condition=" + tree_condition(trig_tree))
        elif triggers and trig_mode == "and" and tid in roots:
            deco.append("automation_condition=" + and_condition([key_from_asset(a) for a in triggers]))
        elif triggers:
            deco.append("automation_condition=dg.AutomationCondition.eager()")
        if pplan and tid in pplan["args"]:
            deco.append(f"partitions_def={safe(dag_id)}_items")
        elif ppart:
            deco.append(f"partitions_def={part_var}")
        if t["retries"]:
            deco.append(f"retry_policy=dg.RetryPolicy(max_retries={t['retries']}, delay={int(t['retry_delay_s'] or 300)}{', backoff=dg.Backoff.EXPONENTIAL' if t.get('retry_exponential_backoff') else ''})")
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
            if isinstance(conf, dict) and "repr" in conf and CAVEATS != "off":      # a callable or object: the run is launched without it
                caveats.append({"task": tid, "kind": "degraded", "reason": "the conf passed to the triggered DAG is not a plain value and was dropped"})
                conf = None
            body = ctx_line + f"    trigger_dag_run(context, __name__, {t['args']['trigger_dag_id']!r}, {expr(conf, params, ctxn) if conf else 'None'}{'' if t['args'].get('wait_for_completion') else ', wait=False'})"
            deco.append("kinds={'dagster'}")
            pre = ""
            ret_none = True
            notes.append(f"{tid} launches the job of DAG {t['args']['trigger_dag_id']!r} " + ("and waits for it; its run fails if the launched run fails" if t["args"].get("wait_for_completion") else "and does not wait (the run executes in the background of this step's process)") + " (the DAG must be converted into the same package)")
        elif is_custom(t):
            body = custom_body(t, ctx_line)
            if tid not in consumed and not RETURN_ALL:
                body = body.replace("    return run_operator(", "    run_operator(")
            deco.append("kinds={'python'}")
            pre = ""
            ret_none = tid not in consumed and not RETURN_ALL
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
            code = re.sub(r"^(async\s+)?def\s+\w+", lambda mm: f"{mm.group(1) or ''}def {pyname}", code, count=1)
            if meta.get("async"):
                code += f"\n\n\n{pyname} = af_sync({pyname})"
            arg_caveat = ""
            try:
                pos = [expr(a, params, ctxn) for a in (t.get("op_args") or [])]
                kws = {k: expr(v, params, ctxn) for k, v in (t.get("op_kwargs") or {}).items()}
                if isinstance((t.get("template_fields") or {}).get("templates_dict"), dict) and ("templates_dict" in str((t.get("callable") or {}).get("params", "")) or "templates_dict" in str((t.get("callable") or {}).get("source", ""))):
                    kws["templates_dict"] = expr(t["template_fields"]["templates_dict"], params, ctxn)      # rendered templates handed to the callable
                if "self" in ((t.get("callable") or {}).get("params") or []) and t.get("outlet_assets"):          # an @asset function receives its own asset
                    kws["self"] = "Asset(name=%r, uri=%r)" % (t["outlet_assets"][0].get("name"), t["outlet_assets"][0].get("uri"))
            except Defer as e:
                if CAVEATS == "off":
                    raise
                arg_caveat, pos, kws = str(e), [], {}
                caveats.append({"task": tid, "kind": "stub", "reason": arg_caveat})      # AIRFLOW-CAVEAT: the call's arguments could not be wired
            is_mapped_task = bool(pplan and tid in pplan["args"])
            if is_mapped_task:
                kws = {**{k: repr(v) for k, v in pplan["fixed"][tid].items()}, pplan["args"][tid]: "item"}
            is_producer = bool(pplan and pplan.get("producer") == tid)
            if meta.get("needs_ctx"):
                inner = f"call_with_context({pyname}, [{', '.join(pos)}], {{{', '.join(f'{k!r}: {v}' for k, v in kws.items())}}}, ctx)"
            else:
                inner = f"{pyname}(" + ", ".join(pos + [f"{k}={v}" for k, v in kws.items()]) + ")"
            if arg_caveat:
                inner = f"af_caveat({arg_caveat[:300]!r}, skip={CAVEATS == 'skip'})"
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
        body = sla_wrap(body, t, "/".join(keyparts[tid]))
        if tid in rt_outlets:
            inner_ = "\n".join(("    " + ln if ln.strip() else ln) for ln in body.split("\n"))
            body = f"    def _run():\n{inner_}\n    _result = _run()\n    af_runtime_partition_tag(context, {rt_outlets[tid]!r})\n    return _result"
        blocks.append(pre + "@dg.asset(\n    " + ",\n    ".join(deco) + ",\n)\n" + hook_deco + "def " + fname + sig + ":\n" + body + "\n")

    keys = [keyparts[t["task_id"]] for t in real] + ([[safe(dag_id), "run_check"]] if graph_outs else [])
    imports = (["import itertools"] if gplan else []) + ["import dagster as dg"] + (["from dagster_community_components import on_hooks"] if cb_code else []) \
        + (["from dagster_community_components import PartitionWindowSensorComponent"] if ppart and ppart.get("via") else []) + ["", "from ..runtime import *  # noqa: F401,F403"]
    mod = [f'"""Converted from Airflow DAG `{dag_id}` ({dag["file"]}) by convert_ir.py.', "", note, '"""', *imports, ""]
    if support:
        mod += [s for s, _ in sorted(support.items(), key=lambda kv: kv[1])] + [""]
    if map_blocks:
        mod += ["\n\n".join(map_blocks), ""]
    if cb_blocks:
        mod += ["\n\n".join(cb_blocks), ""]
    if ppart:
        mod += [f"{part_var} = {partitions_code(ppart['info'])}", ""]
        if ppart["info"].get("dynamic"):
            notes.append(f"the assets are partitioned by the dynamic partition set {ppart['info']['dynamic']!r}, whose keys the upstream producer registers while it runs; "
                         "the assets update for each key the upstream records (Airflow's identity partition mapping)")
        else:
          notes.append(f"the assets are {ppart['info']['grain']} partitions starting {ppart['info']['start']} (set with --partition-start; Airflow's partitions have no start date). "
                     "Partition keys are Dagster's own format for the grain, not the key_format of the Airflow timetable"
                     + ("; each tick runs the partition whose window contains the tick, as Airflow does" if ppart["role"] == "producer"
                        else "; the assets update when an upstream partition does, for the partition the mapper names"))
    for tid_, okey in rt_outlets.items():
        mod += [f"{safe(tid_)}_runtime_asset = dg.AssetSpec(key={okey!r}, partitions_def=dg.DynamicPartitionsDefinition(name={RUNTIME_PARTS['/'.join(okey)]!r}), "
                f"group_name={safe(dag_id)!r}, description='Partitioned by keys the step {tid_} records while it runs', metadata={{'airflow_dag': {dag_id!r}}})", ""]
        notes.append(f"{tid_} records the partition keys of {'/'.join(okey)} while it runs (the key comes from `outlet_events[...].add_partitions` or, when the run is launched "
                     "with the tag `airflow/partition_key`, from that tag): the step is the asset " + "/".join(keyparts[tid_]) + ", the partitioned asset is reported separately")
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
    pd_arg = (f", partitions_def={safe(dag_id)}_items" if pplan and pplan["kind"] == "literal" else (f", partitions_def={part_var}" if ppart else ""))
    job_tags = {**({"airflow/dag_id": dag_id} if max_runs else {}), **({"dagster/max_runtime": str(run_timeout)} if run_timeout else {})}
    out += (f"\n{safe(dag_id)}_job = dg.define_asset_job({safe(dag_id)!r}, selection=[dg.AssetKey(k) for k in {job_keys!r}]{pd_arg}"
            + (f", tags={job_tags!r}" if job_tags else "")
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
    elif ppart and ppart.get("via"):
        v = ppart["via"]
        up_key = "/".join(key_from_asset(triggers[0]))
        args_s = ", ".join(f"{k}={v[k]!r}" for k in ("mode", "window_type", "upstream_key_format", "downstream_key_format", "wait_policy", "minimum_count", "max_downstream_keys", "direction") if k in v)
        out += (f"\n{safe(dag_id)}_window_sensor = PartitionWindowSensorComponent(sensor_name={safe(dag_id) + '_window'!r}, upstream_asset_key={up_key!r}, "
                f"job_name={safe(dag_id)!r}, {args_s}, default_status='running').build_defs(None).sensors[0]\n")
    elif ppart and ppart["role"] == "producer":
        out += (f"\n\n@dg.schedule(name={safe(dag_id) + '_schedule'!r}, job={safe(dag_id)}_job, cron_schedule={ppart['info']['cron']!r}, "
                f"execution_timezone={ppart['info']['tz']!r})\n"
                f"def {safe(dag_id)}_schedule(context):\n"
                f"    return dg.RunRequest(partition_key={part_var}.get_partition_key_for_timestamp(context.scheduled_execution_time.timestamp()))\n")
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
                 "resources": sorted(resources), "snowflake_resources": sorted(sf_resources), "shims": sorted(shims_used), "notes": notes, "caveats": caveats, "catalog_keys": [list(v) for v in rt_outlets.values()], "local_modules": sorted(local_modules(third_party(support), {t["callable"].get("file") for t in real if t.get("callable")})),
                 "requirements": sorted((third_party(support) - local_modules(third_party(support), {t["callable"].get("file") for t in real if t.get("callable")})) | ({"croniter"} if uses_ctx and tt["kind"] == "cron_data_interval" else set()) | ({"dagster-community-components"} if cb_code or (ppart and ppart.get("via")) else set())), "external": sorted("/".join(e) for e in external), "keys": keys,
                 "uses_context": uses_ctx, "run_timeout_s": run_timeout, "partitions": (({"role": ppart["role"], **ppart["info"]}) if ppart else None), "mapping": ({"kind": pplan["kind"], "mapped": sorted(pplan["args"]), "producer": pplan.get("producer")} if pplan else
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
    ap.add_argument("--dataset-keys", choices=["path", "script-orchestrator"], default="path",
                    help="how Airflow Dataset/Asset names become Dagster asset keys: path parts (s3/bucket/a/b), or the flat airflow_dataset_<name> keys "
                         "the script_orchestrator component uses, so both tools produce the same lineage")
    ap.add_argument("--catchup", choices=["defer", "ignore"], default="defer",
                    help="catchup=True runs every interval missed since start_date (it is the default in Airflow 2 when the DAG does not say): defer the DAG (default), "
                         "or convert it and say in the report that history is not replayed")
    ap.add_argument("--caveats", choices=["off", "raise", "skip"], default="raise",
                    help="a task whose callable or arguments cannot be translated (it uses the Airflow API, an unrecognised import, a template the runtime does not rebuild) "
                         "becomes a marked stub and the DAG still converts: raise (default) makes the stub fail with an AIRFLOW-CAVEAT message when it runs, "
                         "skip makes it return nothing, off defers the whole DAG with the reason")
    ap.add_argument("--output", choices=["python", "components"], default="python",
                    help="python (default): generated Python for every DAG. components: registry components (a defs.yaml per DAG with one component per task, "
                         "a schedule component, post-processing where needed) for each DAG whose every task has a component form; the others stay Python")
    ap.add_argument("--partition-start", default="2025-01-01", help="where time-partitioned assets begin (Airflow's partitions have no start date)")
    ap.add_argument("--return-all", action="store_true", help="return every Python task's value (stored by the IO manager), for parity runs")
    a = ap.parse_args(argv)
    global RETURN_ALL, MAP_MODE, AF_MAJOR, DATASET_KEYS, CATCHUP, CAVEATS, OUTPUT
    OUTPUT = a.output
    CAVEATS = a.caveats
    RETURN_ALL = a.return_all
    MAP_MODE = a.mapping
    DATASET_KEYS = a.dataset_keys
    CATCHUP = a.catchup
    ir = json.loads(a.ir.read_text())
    PARTITION_START_SET = a.partition_start
    globals()["PARTITION_START"] = PARTITION_START_SET
    build_partition_catalog(ir)
    build_task_catalog(ir)
    global ALL_DAG_IDS, TASK_TYPES
    ALL_DAG_IDS = {d["dag_id"] for d in ir["dags"]}
    TASK_TYPES = {(d["dag_id"], t["task_id"]): t["type"] for d in ir["dags"] for t in d["tasks"]}
    _m = re.match(r"(\d+)", str(ir.get("airflow_version") or ""))
    AF_MAJOR = int(_m.group(1)) if _m else 2
    a.out_dir.mkdir(parents=True, exist_ok=True)
    (a.out_dir / "__init__.py").touch()
    _v = re.match(r"(\d+)\.(\d+)", str(ir.get("airflow_version") or ""))
    iso = bool(_v) and (int(_v.group(1)), int(_v.group(2))) < (2, 8)
    (a.out_dir.parent / "runtime.py").write_text(RUNTIME.replace("AF_ISO_DT = False", f"AF_ISO_DT = {iso}", 1).replace('DATASET_KEYS_MODE = "path"', f'DATASET_KEYS_MODE = "{DATASET_KEYS}"', 1))
    note = ("Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 "
            "(trigger timetable) and Airflow 2 (data-interval timetable).")
    report: dict[str, Any] = {"airflow_version": ir.get("airflow_version"), "dataset_keys": DATASET_KEYS, "dags": {}}
    produced: dict[str, str] = {}
    resources: set[str] = set()
    sf_resources: set[str] = set()
    external: set[str] = set()
    for dag in ir["dags"]:
        name = safe(dag["dag_id"])
        try:
            code, summary = convert_dag(dag, note, produced)
            compile(code, name, "exec")
            comp = component_files(dag, summary, a.out_dir.parent.name) if OUTPUT == "components" else None
            if comp:
                files, over = comp
                for rel, text in files.items():
                    dest = a.out_dir.parent / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(text)
                    if rel.startswith("callables/"):
                        (dest.parent / "__init__.py").touch()
                summary = {**summary, **over}
            else:
                (a.out_dir / f"{name}.py").write_text(code)
                summary = {**summary, "output": "python"}
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
    owned = {"/".join(k) for v in report["dags"].values() if v["status"] == "converted" for k in [*v["keys"], *v.get("catalog_keys", [])]}
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
