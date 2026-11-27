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
import os
import re
import textwrap
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import connections as CN
import cosmos_ops as K
import gen_tests as G
import pattern_emit as PE
import provider_ops as P
import provider_ops_more
import registry_map as RM

provider_ops_more.register()          # more provider operators: Slack API, S3 key sensor, GCS, SSH, Docker, Lambda, BigQuery query jobs, Spark submit

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
                 "prev_data_interval_start_success", "prev_data_interval_end_success", "task_instance_key_str", "task_state_store", "asset_state_store",
                 "context", "var", "conn", "test_mode"}                                                                  # `test_mode` is False (a scheduled / triggered run, not `airflow tasks test`); `context`: the context dict itself, `var` / `conn`: Variables and Connections from the environment
JINJA_VARS = SUPPORTED_CTX | {"macros", "var"}
USER_MACROS: set[str] = set()     # the DAG's `user_defined_macros` names (set per DAG): a template may use them
CTX_ATTRS = {"dag_run": {"run_id", "conf", "logical_date", "execution_date", "dag_id", "partition_key", "partition_date", "state"}, "dag": {"dag_id"}, "task": {"task_id", "dag_id", "render_template", "params"},
             "ti": {"dag_id", "task_id", "run_id", "try_number", "map_index", "xcom_pull", "xcom_push", "state"},
             "task_instance": {"dag_id", "task_id", "run_id", "try_number", "map_index", "xcom_pull", "xcom_push", "state"}}
SUPPORTED_MACROS = {"ds_add", "ds_format", "ds_format_locale", "datetime", "timedelta", "json", "yaml", "time", "uuid", "random", "dateutil"}
SHIM_IMPORTS = {(m, "WeekDay") for m in ("airflow.providers.standard.utils.weekday", "airflow.utils.weekday")} | {("airflow.sdk", "LoopDecision"), ("airflow.models", "Variable"), ("airflow.sdk", "Variable"), ("airflow.models.variable", "Variable"),
                ("airflow.operators.python", "get_current_context"), ("airflow.sdk", "get_current_context"),
                ("airflow.providers.amazon.aws.hooks.s3", "S3Hook"), ("airflow.hooks.base", "BaseHook"), ("airflow.hooks.base_hook", "BaseHook"), ("airflow.sdk", "BaseHook"),
                ("airflow.exceptions", "AirflowNotFoundException"), ("airflow.sdk.exceptions", "AirflowNotFoundException")} | \
    {(m, "Connection") for m in ("airflow.models", "airflow.models.connection", "airflow.sdk", "airflow.sdk.definitions.connection")} | \
    {(m, "ObjectStoragePath") for m in ("airflow.io.path", "airflow.sdk", "airflow.sdk.io", "airflow.sdk.io.path")} | \
    {(m, "BaseNotifier") for m in ("airflow.notifications.basenotifier", "airflow.sdk.bases.notifier", "airflow.sdk")} | \
    {(m, "BaseOperator") for m in ("airflow.models", "airflow.models.baseoperator", "airflow.sdk", "airflow.sdk.bases.operator")} | \
    {(m, n) for m in ("airflow", "airflow.sdk", "airflow.datasets", "airflow.assets", "airflow.sdk.definitions.asset", "airflow.datasets.metadata") for n in ("Asset", "Dataset", "Metadata", "AssetAlias", "DatasetAlias")} | \
    {("airflow.utils.decorators", "apply_defaults")} | {(m, "Context") for m in ("airflow.utils.context", "airflow.sdk", "airflow.sdk.definitions.context")} | \
    {(m, n) for m in ("airflow.exceptions", "airflow.sdk.exceptions", "airflow.sdk") for n in ("AirflowException", "AirflowSkipException", "AirflowFailException")} | \
    {(m, "PokeReturnValue") for m in ("airflow.sensors.base", "airflow.sdk", "airflow.sdk.bases.sensor")} | \
    {("airflow.sdk.execution_time.context", "NEVER_EXPIRE"), ("airflow.configuration", "conf"), ("asgiref.sync", "sync_to_async")} | \
    {(m, n) for m in ("airflow.triggers.base", "airflow.sdk.triggers.base", "airflow.sdk") for n in ("BaseTrigger", "TriggerEvent", "StartTriggerArgs")}
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
SENSORS = SENSOR_CALL | {"BashSensor", "FileSensor", "TimeSensor", "TimeDeltaSensor", "HttpSensor"} | P.SENSOR_TYPES   # converted as a polling step (Airflow's default `poke` behavior)
CLOCK_BRANCH = {"BranchDateTimeOperator", "BranchDayOfWeekOperator"}      # branch on the clock or the logical date: no callable
GATE_RULES = {"all_success", "all_failed", "all_done", "one_success", "one_failed", "one_done", "none_failed", "none_failed_min_one_success",
              "none_skipped", "all_skipped", "all_done_min_one_success", "always", "all_done_setup_success"}
PLAIN_RULES = {"all_success", "none_failed", "none_failed_min_one_success"}               # behave like all_success when a failure stops the run anyway
S3_OPS = {"S3ListOperator", "S3CopyObjectOperator", "S3DeleteObjectsOperator", "LocalFilesystemToS3Operator", "S3CreateObjectOperator", "S3ListPrefixesOperator", "S3CreateBucketOperator", "S3DeleteBucketOperator"}
SNOWFLAKE_OPS = {"CopyFromExternalStageToSnowflakeOperator", "SnowflakeOperator", "SQLExecuteQueryOperator"}
PROV = P.TYPES                  # provider operators translated by provider_ops.py (HTTP, SQL on other databases, Kubernetes pods, email, Slack, dbt Cloud, Airbyte, Fivetran, Databricks)
COSMOS_STEP = {"CosmosDbtStep"}      # a per-task Cosmos operator, run through dagster_dbt.DbtCliResource (cosmos_ops.py)
PROV_STEPS = PROV - P.SENSOR_TYPES          # the ones that run as a step of their own (the sensor is a polling step)


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
DBT_DIR = "dbt"          # --dbt-dir: where the dbt projects of converted Cosmos DAGs are expected, inside the package
GRANULARITY = "product"   # --granularity product (default: an asset is a data product; steps folded into the asset they feed, see the report's `granularity`) | task (fallback: one asset per task, faithful)
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


def _annotation_names(node: ast.AST) -> set[str]:
    """Names used in the argument and return annotations of a definition (and, for a class, of its methods): a copy without `from __future__ import annotations` evaluates them when it is defined."""
    out: set[str] = set()
    for a in ast.walk(node):
        anns = [a.annotation] if isinstance(a, ast.arg) and a.annotation is not None else ([a.returns] if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)) and a.returns is not None else [])
        for ann in anns:
            out |= {n.id for n in ast.walk(ann) if isinstance(n, ast.Name)}
    return out


def _deps_of(node: ast.AST) -> set[str]:
    """The module-level names a copied definition needs: a function's free names, or for a class the free names of each method (parameters and locals are not dependencies), its class-level statements,
    decorators and bases, minus the names the class defines itself."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _free_names(node)
    if not isinstance(node, ast.ClassDef):
        return _loads(node)
    free: set[str] = set()
    own: set[str] = set()
    for st in node.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            own.add(st.name)
            free |= _free_names(st)
            for d in st.decorator_list:
                free |= _loads(d)
        else:
            free |= _loads(st)
            own |= {t.id for a in ast.walk(st) if isinstance(a, ast.Assign) for t in a.targets if isinstance(t, ast.Name)}
    for b in [*node.bases, *node.decorator_list]:
        free |= _loads(b)
    return free - own - {node.name}


_PLAIN_CALLS = {"dict", "list", "set", "tuple", "str", "int", "float", "bool", "len", "range", "sorted", "min", "max", "sum", "frozenset", "reversed", "abs", "round"}


def _plain_local(value: ast.AST) -> bool:
    """A DAG-function local whose value is plain data: literals, containers, names, attribute reads, operators, f-strings, comprehensions, and builtin calls over those."""
    for n in ast.walk(value):
        if isinstance(n, (ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda)):
            return False
        if isinstance(n, ast.Call) and not (isinstance(n.func, ast.Name) and n.func.id in _PLAIN_CALLS):
            return False
    return True


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

        # plain helper functions defined inside the DAG function or `with DAG(...)` block (a skip condition, a date helper): usable when they use nothing but module names
        for top in self.tree.body:
            body = top.body if isinstance(top, (ast.FunctionDef, ast.With)) else []
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.decorator_list and node.name not in self.defs:
                    self.defs[node.name] = ("def", node)
            # a helper that closes over the DAG function's locals: the locals it reads are copied in too when they are plain values (constants, containers, f-strings,
            # arithmetic over other such names; no call except a builtin). A local that holds a task's output, an Airflow object or any other call stays unresolved: loud.
            for node in body:
                tgt = (node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else getattr(node, "target", None)) if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
                if isinstance(tgt, ast.Name) and getattr(node, "value", None) is not None and tgt.id not in self.defs and _plain_local(node.value):
                    self.defs[tgt.id] = ("assign", node)

    def seg(self, node: ast.AST) -> str:
        text = ast.get_source_segment(self.src, node) or ""
        if getattr(node, "col_offset", 0):                     # a nested definition: restore the first line's indent so the block dedents
            text = textwrap.dedent(" " * node.col_offset + text)
        return text


def mentions_airflow(text: str) -> bool:
    """Whether code refers to the `airflow` package: a name `airflow`, an import of `airflow...`, or a string that is itself a dotted `airflow.x` path. The word inside an ordinary string (a bucket or host name such as
    `aws_default@airflow-tutorial-data`, a message) is not a reference."""
    try:
        tree = ast.parse(textwrap.dedent(text))
    except SyntaxError:
        return bool(re.search(r"\bairflow\b", text))
    for n in ast.walk(tree):
        if (isinstance(n, ast.Name) and n.id == "airflow") or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "airflow") \
                or (isinstance(n, ast.Import) and any(a.name.split(".")[0] == "airflow" for a in n.names)) \
                or (isinstance(n, ast.Constant) and isinstance(n.value, str) and re.fullmatch(r"airflow(\.\w+)+", n.value)):
            return True
    return False


def _loads(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)} - set(dir(builtins))


def repo_local_module_file(source_file: str, mod: str) -> Path | None:
    """A module imported under the `airflow.` namespace that is a file of the PROJECT being converted (Airflow's own example DAGs keep `airflow/example_dags/libs/helper.py` beside the DAGs),
    not the installed Airflow package: found by walking up from the DAG file."""
    rel = Path(*mod.split("."))
    here = Path(source_file).resolve().parent if source_file else None
    for r in ([here, *here.parents] if here else []):
        for cand in (r / rel.with_suffix(".py"), r / rel / "__init__.py"):
            if cand.exists() and "site-packages" not in cand.parts:
                return cand
    return None


AIRFLOW_METADATA_EQUIVALENTS = [
    (r"\bDagRun\b|\bDagBag\b|\bDagModel\b|\bDagRunState\b", "DagRun / DagBag", "context.instance.get_runs(...) and context.run (a DagsterInstance), or run tags for the logical date"),
    (r"\bTaskInstance\b|\bTaskInstanceKey\b|\bTaskInstanceState\b", "TaskInstance", "context.instance.get_run_records(...) / the run's step events (DagsterInstance); for the current step, the op context"),
    (r"\bXCom\b|\bBaseXCom\b", "XCom", "the value a step returns (an upstream asset's value through the IO manager) or context.instance.get_latest_materialization_event(AssetKey(...)).asset_materialization.metadata"),
    (r"\bprovide_session\b|\bcreate_session\b|\bsettings\b|\bSession\b|\bsession\b", "provide_session / a SQLAlchemy session on the Airflow metadata DB",
     "a DagsterInstance (context.instance): there is no SQL session on Dagster's storage to hand a callable"),
    (r"\bPool\b|\bTrigger\b|\bLog\b|\bSlaMiss\b", "an Airflow metadata model", "the matching Dagster concept (pool= on an op, a sensor, the event log) through context.instance"),
]


def airflow_api_hint(text: str, what: str = "") -> str:
    """For a callable that reaches the Airflow metadata database or its session: which call, and the Dagster equivalent. Empty when the text uses none of them."""
    for pat, call, eq in AIRFLOW_METADATA_EQUIVALENTS:
        m = re.search(pat, text)
        if m:
            return f" [{what or m.group(0)}: {call}; the Dagster equivalent is {eq}]"
    return ""


def inline_local_helper(modfile: Path, name: str, out: dict[int, str], counter: list[int], seen: set[tuple[str, str]], where: str) -> None:
    """Copy the definition of `name` from a repo-local module (and what it uses, from that module) into `out`, dependencies first. Anything that is not plain Python stays a loud deferral."""
    if (str(modfile), name) in seen:
        return
    seen.add((str(modfile), name))
    idx2 = ModuleIndex(str(modfile))
    hit = idx2.defs.get(name)
    if hit is None:
        raise Defer(f"{name!r} is not defined in the repo-local module {modfile.name} (used by {where})")
    kind, node = hit
    seg = idx2.seg(node)
    if kind == "import":
        mod = getattr(node, "module", None) or ""
        if mod.split(".")[0] == "airflow" or mentions_airflow(seg):
            raise Defer(f"{name!r} (from {modfile.name}) depends on Airflow")
        counter[0] += 1
        out[counter[0]] = seg
        return
    if mentions_airflow(seg):
        raise Defer(f"{name!r} (from the repo-local module {modfile.name}) depends on Airflow")
    for dep in sorted((_free_names(node) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else _loads(node)) - {name}):
        if dep in idx2.defs:
            inline_local_helper(modfile, dep, out, counter, seen, name)
    counter[0] += 1
    out[counter[0]] = seg


def resolve_operator_class(info: dict[str, Any] | None, shims: set[str] | None = None, meta: dict[str, Any] | None = None, entry: str = "execute", base: str = "BaseOperator") -> tuple[str, list[str]]:
    """The source of a user-defined operator class (an `execute` method on a BaseOperator subclass) and the module-level code it needs. Also used for a notifier (`entry="notify"`, `base="BaseNotifier"`)."""
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
    starts_from_trigger = entry == "execute" and cls is not None and any(isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "start_trigger_args" for t in st.targets) for st in cls.body)
    if cls is None or (entry not in methods and not starts_from_trigger):
        raise Defer(f"operator class without a {entry} method" + (" (and no `start_trigger_args`: Airflow itself raises NotImplementedError when it runs the task)" if entry == "execute" else ""))
    inner_shims = [n for n in ast.walk(cls) if isinstance(n, ast.ImportFrom) and n.module and all((n.module, a.name) in SHIM_IMPORTS for a in n.names)]
    for n in inner_shims:
        shims.update(a.name for a in n.names)
    body_text = ast.unparse(ast.Module(body=[*cls.body], type_ignores=[]))
    for n in inner_shims:
        body_text = body_text.replace(ast.unparse(n), "")
    if mentions_airflow(body_text):
        raise Defer("operator class uses the Airflow API (a direct airflow reference)")
    if re.search(r"\bself\.(?:xcom_push|xcom_pull)\b|\bset_xcom\b", body_text):                 # `context["ti"].xcom_push(...)` is rebuilt; the BaseOperator's own methods are not
        raise Defer("operator class calls self.xcom_push / self.xcom_pull (use context['ti'].xcom_push / xcom_pull, which the runtime rebuilds)")
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
            if name in ("af_loop", "af_loop_ti"):                                # provided by the generated runtime (a converted task-group loop)
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
                    raise Defer(f"operator class depends on an Airflow import ({mod}.{al.name if al is not None else ''})" + airflow_api_hint(al.name if al is not None else mod, al.name if al is not None else mod))
                needed[node.lineno] = idx.seg(node)
                continue
            if mentions_airflow(idx.seg(node)):
                raise Defer(f"{name!r} (used by the operator class) depends on Airflow")
            needed[node.lineno] = idx.seg(node)
            need(_deps_of(node), name)
            need({n_ for n_ in _annotation_names(node) if n_ in idx.defs and not (idx.defs[n_][0] == "import" and (getattr(idx.defs[n_][1], "module", None) or "").split(".")[0] == "airflow" and (getattr(idx.defs[n_][1], "module", None), n_) not in SHIM_IMPORTS)}, name)

    free = set()
    for m in methods.values():
        free |= _free_names(m)
    for st in cls.body:                                          # class-level statements and the base classes run in the module's scope
        if not isinstance(st, ast.FunctionDef):
            free |= _loads(st)
    for b in cls.bases:
        free |= _loads(b)
    need(free - {base, "apply_defaults", "Context", "super", "self"}, cls.name)
    for n in cls.bases:
        if not (isinstance(n, ast.Name) and n.id == base):
            raise Defer(f"operator class extends {ast.unparse(n)}, not {base}")
    if base != "BaseOperator":
        shims.add(base)
    lines = textwrap.dedent(info["source"]).splitlines()
    for n in inner_shims:
        indent = re.match(r"\s*", lines[n.lineno - 1]).group(0)
        lines[n.lineno - 1:n.end_lineno] = [indent + "pass"] + [""] * (n.end_lineno - n.lineno)
    start = next(i for i, l in enumerate(lines) if re.match(r"class\s", l))
    body_src = "\n".join(lines[start:])
    for m_ in set(re.findall(r"trigger_cls\s*=\s*['\"][\w.]*?(\w+)['\"]", body_src)):          # `StartTriggerArgs(trigger_cls="pkg.mod.MyTrigger")`: the class defined in the operator's module is used directly
        if idx.defs.get(m_, ("", None))[0] == "def" and isinstance(idx.defs[m_][1], ast.ClassDef):
            need({m_}, cls.name)
            body_src = re.sub(r"(trigger_cls\s*=\s*)['\"][\w.]*?" + m_ + r"['\"]", r"\1" + m_, body_src)
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
    if mentions_airflow(body_text) or re.search(r"\bset_xcom\b", body_text):
        raise Defer("callable uses the Airflow API (a direct airflow reference)" + airflow_api_hint("\n".join(ln for ln in body_text.splitlines() if "airflow" in ln)))
    for n in (m for stmt in [*fn.body, *fn.args.defaults, *[d for d in fn.args.kw_defaults if d is not None]] for m in ast.walk(stmt)):    # the body, not the decorators (`@task.sensor` is not the `task` context object)
        obj = n.value if isinstance(n, ast.Attribute) else None
        if isinstance(obj, ast.Subscript) and isinstance(obj.slice, ast.Constant):
            obj_name = obj.slice.value
        else:
            obj_name = obj.id if isinstance(obj, ast.Name) else None
        if obj_name in CTX_ATTRS and n.attr not in CTX_ATTRS[obj_name]:
            raise Defer(f"callable uses {obj_name}.{n.attr}, which the runtime's {obj_name} does not provide")
    conds = []
    cond_names: set[str] = set()                                 # @task.run_if(cond) / @task.skip_if(cond): the condition decides at run time whether the task skips itself
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in ("run_if", "skip_if") and dec.args:
            if not isinstance(dec.args[0], (ast.Lambda, ast.Name)):
                raise Defer(f"@task.{dec.func.attr} with a condition that is neither a lambda nor a function defined in the DAG file")
            conds.append((dec.func.attr == "run_if", ast.unparse(dec.args[0])))
            if isinstance(dec.args[0], ast.Name):
                cond_names.add(dec.args[0].id)                   # a named condition function is copied with the callable's other support code
    meta["conds"] = conds[::-1]                                  # decorators apply bottom up
    meta["skips"] = "AirflowSkipException" in info["source"] or bool(conds)
    meta["var_kw"] = fn.args.kwarg.arg if fn.args.kwarg else None
    meta["ctx_named"] = ctx_named
    meta["needs_ti"] = bool({"ti", "task_instance"} & set(ctx_named)) or bool(re.search(r"\bxcom_pull\b|\bti\b|\btask_instance\b", body_text))
    _no_pull = re.sub(r"\b(?:ti|task_instance)\.(?:map_index|dag_id|task_id|run_id|try_number|xcom_push)\b", "", body_text)
    meta["needs_xcom"] = bool(re.search(r"\bxcom_pull\b|\bti\b|\btask_instance\b", _no_pull))           # `ti.map_index` and the like need the ti but pull nothing: only an xcom_pull needs the upstream values wired in
    meta["needs_ctx"] = bool(conds) or bool(ctx_named) or bool(fn.args.kwarg) or "get_current_context" in body_text or bool(re.search(r"\bMetadata\b|\boutlet_events\b|\binlet_events\b", body_text))
    idx = ModuleIndex(info["file"])
    needed: dict[int, str] = {}
    seen: set[str] = set()
    local_counter = [0]
    file_used = [False]

    def need(names: set[str], where: str) -> None:
        for name in sorted(names):
            if name in seen:
                continue
            seen.add(name)
            if name in ("af_loop", "af_loop_ti"):                                # provided by the generated runtime (a converted task-group loop)
                continue
            if name == "__file__":                               # the DAG file's own path: a shim constant (overridable by env), not the converted module's path
                file_used[0] = True
                continue
            if name in (info.get("closure") or {}):               # DAG factory: the enclosing function's argument or local, as it was for THIS generated DAG (recorded at extraction)
                needed[-30_000_000 + len(needed)] = f"{name} = {info['closure'][name]}"
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
                    lf = repo_local_module_file(info["file"], mod) if al is not None else None
                    if lf is None:
                        raise Defer(f"callable depends on an Airflow import ({mod}.{al.name if al is not None else ''})" + airflow_api_hint(al.name if al is not None else mod, al.name if al is not None else mod))
                    loc: dict[int, str] = {}                      # the project's own helper that happens to live under the `airflow.` namespace: copied in as plain Python
                    inline_local_helper(lf, al.name, loc, local_counter, set(), info["name"])
                    needed.update({-10_000_000 + k: v for k, v in loc.items()})
                    if al.asname and al.asname != al.name:
                        needed[-10_000_000 + local_counter[0] + 1] = f"{al.asname} = {al.name}"
                        local_counter[0] += 1
                    continue
                needed[node.lineno] = idx.seg(node)
                continue
            if re.fullmatch(r"\s*\w+\s*=\s*\w+\.getLogger\(\s*['\"]airflow\.task['\"]\s*\)\s*", idx.seg(node)):     # `t_log = logging.getLogger("airflow.task")`: only a logger name, no Airflow code
                needed[node.lineno] = idx.seg(node)
                need(_loads(node), name)
                continue
            if mentions_airflow(idx.seg(node)):
                raise Defer(f"{name!r} (used by the callable) depends on Airflow")
            seg_ = idx.seg(node)
            if re.match(rf"\s*{re.escape(name)}\s*=\s*ObjectStoragePath\(", seg_):          # a module-level `base = ObjectStoragePath("s3://conn@bucket/")`: built on first use, so an unset connection fails the task that uses it, not the import of the module
                seg_ = seg_.replace("ObjectStoragePath(", "af_lazy_path(", 1)
            needed[node.lineno] = seg_
            need(_deps_of(node), name)
            need({n_ for n_ in _annotation_names(node) if n_ in idx.defs and not (idx.defs[n_][0] == "import" and (getattr(idx.defs[n_][1], "module", None) or "").split(".")[0] == "airflow" and (getattr(idx.defs[n_][1], "module", None), n_) not in SHIM_IMPORTS)}, name)

    need(_free_names(fn), info["name"])
    need(cond_names, info["name"])
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
    code_text = "\n".join(lines[start:])
    if file_used[0]:
        code_text = re.sub(r"\b__file__\b", "_AF_DAG_FILE", code_text)
        needed[-20_000_000] = (f"import os\n\n_AF_DAG_FILE = os.environ.get('AIRFLOW_DAG_FILE', {str(info['file'])!r})   # the original DAG file's path: set AIRFLOW_DAG_FILE where that file lives in "
                               "your deployment (`__file__` of the converted module would be a different file)")
    return code_text, [needed[k] for k in sorted(needed)]


# --------------------------------------------------------------------------------------------------------------------
# helpers shared by the per-task handlers
# --------------------------------------------------------------------------------------------------------------------
def is_custom(t: dict[str, Any]) -> bool:
    """A user-defined operator (an `execute` method on a BaseOperator subclass in the project, not a provider's) that nothing else here maps."""
    known = ELIDED | PY_TASKS | BASH | BASH_DEC | BRANCH | SHORT | CLOCK_BRANCH | SENSORS | TRIGGER | S3_OPS | SNOWFLAKE_OPS | PROV | COSMOS_STEP | {"ExternalTaskSensor"}
    return bool(t.get("operator_class")) and t["type"] not in known


def _walk_values(v: Any):
    yield v
    if isinstance(v, dict):
        for x in v.values():
            yield from _walk_values(x)
    elif isinstance(v, list):
        for x in v:
            yield from _walk_values(x)


def asset_fn_kwargs(t: dict[str, Any], kws: dict[str, str]) -> None:
    """An Airflow 3 `@asset` function receives its own asset as `self` and, as a parameter named after it, each asset it reads (an inlet): both are handed over as the runtime's `Asset` objects."""
    params = (t.get("callable") or {}).get("params") or []
    if "self" in params and t.get("outlet_assets"):
        kws["self"] = "Asset(name=%r, uri=%r)" % (t["outlet_assets"][0].get("name"), t["outlet_assets"][0].get("uri"))
    if t["type"] == "_AssetMainOperator":
        inl = {x for x in (t.get("inlets") or []) if isinstance(x, str)}
        for pn in params:
            if pn not in kws and pn not in ("self", "context") and pn in inl:
                kws[pn] = f"Asset(name={pn!r})"


def unplain_ctor_args(t: dict[str, Any]) -> list[str]:
    """Constructor arguments of a user-defined operator that cannot be written as code: an object the IR could only describe with its repr."""
    return [k for k, v in (t.get("args") or {}).items() if any(isinstance(x, dict) and "repr" in x for x in _walk_values(v))]


def ctor_expr(v: Any, params: dict[str, str]) -> str:
    """A constructor argument as Python: plain values as literals, and the structured forms the IR uses (datetime, time, timedelta, an ObjectStoragePath, an upstream value, a group item)."""
    if isinstance(v, dict):
        if "datetime" in v:
            return f"af_datetime.fromisoformat({v['datetime']!r})"
        if "time" in v:
            return f"af_time.fromisoformat({v['time']!r})"
        if "seconds" in v:
            return f"af_timedelta(seconds={v['seconds']!r})"
        if {"object_storage_path", "xcom_from", "group_arg", "repr", "asset_obj"} & set(v):
            return expr(v, params)
        return "{" + ", ".join(f"{k!r}: {ctor_expr(x, params)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(ctor_expr(x, params) for x in v) + "]"
    return repr(v)


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


GROUP_ITEM: list[str | None] = [None]                    # the variable holding the mapped task group's item while a member task's step is written


def expr(v: Any, params: dict[str, str], ctx: str | None = None) -> str:
    if isinstance(v, dict):
        if "group_arg" in v:                                   # an argument of the mapped task group the task sits in: the group's current item
            if GROUP_ITEM[0] is None or v["group_arg"] is None:
                raise Defer("a mapped task group argument outside its group")
            return f"{GROUP_ITEM[0]}[{v['group_arg']!r}]"
        if "xcom_from" in v:
            if v["xcom_from"] not in params:
                raise Defer(f"XCom reference to {v['xcom_from']!r}, which is not an upstream task the converter can wire")
            if v.get("key") not in (None, "return_value"):         # a key of a task that returns a dict with multiple_outputs
                return f"af_xcom_key({params[v['xcom_from']]}, {v['key']!r})"
            return params[v["xcom_from"]]
        if "object_storage_path" in v:                          # an ObjectStoragePath argument: rebuilt at run time by the runtime's pathlib/UPath shim
            return f"ObjectStoragePath({v['object_storage_path']!r})"
        if "asset_obj" in v:                                    # an airflow Dataset / Asset handed to a callable: the runtime's plain class (uri, name, extra)
            a_ = v["asset_obj"]
            if a_["cls"] == "Dataset":
                return f"Dataset({a_['uri']!r}, {expr(a_.get('extra'), params, ctx)})"
            return f"Asset(name={a_.get('name')!r}, uri={a_.get('uri')!r}, extra={expr(a_.get('extra'), params, ctx)})"
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
    bad = names - JINJA_VARS - USER_MACROS - set(env.globals)
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
    "S3ListPrefixesOperator": ("bucket", "prefix", "delimiter", "aws_conn_id"),
    "S3CreateBucketOperator": ("bucket_name", "region_name", "aws_conn_id"),
    "S3DeleteBucketOperator": ("bucket_name", "force_delete", "aws_conn_id"),
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
    h = P.handler_for(t)
    if h:
        return h.fields(t)
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
    if ty == "S3CreateBucketOperator":
        return f"s3_create_bucket(client, {g('bucket_name')}, {g('region_name')})", conn
    if ty == "S3DeleteBucketOperator":
        return f"s3_delete_bucket(client, {g('bucket_name')}, {bool(f.get('force_delete'))!r})", conn
    if ty == "S3ListPrefixesOperator":
        return f"return s3_list_prefixes(client, {g('bucket')}, {g('prefix')}, {g('delimiter')})", conn
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
    "KubernetesPodOperator": {"official": "dagster-k8s execute_k8s_job (PipesK8sClient needs a container that speaks the Pipes protocol)", "registry": ["k8s_job_asset"],
                              "gap": "k8s_job_asset has no args, labels, node selector or kubeconfig fields; the converter calls execute_k8s_job itself"},
    "SimpleHttpOperator": {"registry": ["http_external_asset", "http_webhook_job", "http_resource"], "gap": "no component returns the response body as the asset value; the converter calls requests itself"},
    "HttpOperator": {"registry": ["http_external_asset", "http_webhook_job", "http_resource"], "gap": "no component returns the response body as the asset value; the converter calls requests itself"},
    "HttpSensor": {"registry": ["http_poll_sensor"], "gap": "http_poll_sensor starts a run when a response changes; it does not hold a pipeline step open until an endpoint answers"},
    "EmailOperator": {"registry": ["smtp_send_asset"], "gap": "smtp_send_asset sends one email per row of an upstream DataFrame; there is no component that sends one message with a fixed body"},
    "SlackWebhookOperator": {"registry": ["slack_notification_job", "slack_message_send"], "gap": "slack_notification_job is a job, not a step in a DAG; slack_message_send needs an upstream DataFrame"},
    "DbtCloudRunJobOperator": {"official": "dagster-dbt DbtCloudClientResource", "registry": ["dbt_cloud_trigger_job"], "gap": "dbt_cloud_trigger_job is a job, not a step in a DAG"},
    "AirbyteTriggerSyncOperator": {"official": "dagster-airbyte AirbyteResource", "registry": ["airbyte_sync_trigger_job"], "gap": "airbyte_sync_trigger_job is a job, not a step in a DAG"},
    "FivetranOperator": {"official": "dagster-fivetran FivetranResource", "registry": ["fivetran_sync_trigger_job"], "gap": "fivetran_sync_trigger_job is a job, not a step in a DAG, and does not wait"},
    "DatabricksRunNowOperator": {"official": "dagster-databricks DatabricksClientResource", "registry": ["databricks_asset_bundle"], "gap": "no component runs an existing job by id"},
    "DatabricksSubmitRunOperator": {"official": "dagster-databricks DatabricksClientResource", "registry": ["databricks_asset_bundle"], "gap": "no component submits a one-off run from a JSON body"},
    "SqlQuery": {"registry": ["sql_asset"], "gap": "sql_asset (connection_env) runs the statements but returns no rows"},
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
    "AzureDataFactoryRunPipelineOperator": "azure_data_factory",
    "SQLColumnCheckOperator": "enhanced_data_quality_checks (null_check, uniqueness_check, range_check as asset checks)",
    "SlackAPIPostOperator": "slack_notification_job",
    "S3CreateObjectOperator": "s3_create_object_asset",
    "LoopGateOperator": "loop_gate_asset (Airflow 3.4 task-group loops: body callable, until callable, max_iterations)",
    "S3ListPrefixesOperator": "s3_list_keys_asset (common_prefixes metadata)",
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


def bash_skip_codes(t: dict[str, Any]) -> list[int]:
    a = t.get("args") or {}
    v = a.get("skip_on_exit_code", [99])
    return [] if v is None else ([v] if isinstance(v, int) else [int(x) for x in v])


def skx(t: dict[str, Any]) -> str:
    codes = bash_skip_codes(t)
    return "" if codes == [99] else f", skip_on_exit_code={codes!r}"


def bash_skips(t: dict[str, Any]) -> bool:
    """A shell task whose command visibly exits with one of its skip codes (Airflow's default is 99): it can skip, so it needs the graph form's skip handling."""
    if t.get("type") not in BASH | BASH_DEC:
        return False
    text = " ".join(str(x) for x in ((t.get("callable") or {}).get("source"), (t.get("template_fields") or {}).get("bash_command"),
                                     (t.get("template_files") or {}).get("bash_command")) if x)
    return any(int(c) in bash_skip_codes(t) for c in re.findall(r"\bexit\s+(\d+)", text))


def plan_groups(dag: dict[str, Any], tasks: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Mapped task groups (`@task_group.expand(...)`): ({group id: plan}, {group id: why it is not converted}).
    A convertible group is a literal list, a task's plain return value, or `expand_kwargs` over one of those; its members are plain steps (no branches, mapping or gated trigger rules).
    The plan is the verified native form (rewrite-patterns/mapped_tasks.py, `mapped_group`): a `@dg.graph` of the member steps mapped over a DynamicOut of the group's items."""
    ids = {t["mapped_group"] for t in tasks.values() if t.get("mapped_group")}
    good: dict[str, Any] = {}
    bad: dict[str, str] = {}
    for gid in sorted(ids):
        members = [tid for tid, t in tasks.items() if t.get("mapped_group") == gid]
        g = (dag.get("mapped_groups") or {}).get(gid)
        why = None
        if not g:
            why = "the extractor did not record what the group iterates over (re-extract the IR)"
        elif g.get("form") not in ("expand", "expand_kwargs") or not g.get("args"):
            why = f"the group is mapped with {g.get('form')}"
        else:
            srcs: dict[str, tuple[str, Any]] = {}
            for name, a in g["args"].items():
                if a.get("kind") == "literal" and isinstance(a.get("value"), list):
                    srcs[name] = ("lit", a["value"])
                elif a.get("kind") == "xcom" and a.get("wrapper") in ("PlainXComArg", "XComArg") and a.get("key") == "return_value" and a.get("task_id") in tasks \
                        and tasks[a["task_id"]].get("mapped_group") != gid:
                    srcs[name] = ("task", a["task_id"])
                else:
                    why = f"the group iterates over {a.get('wrapper') or a.get('kind')} for `{name}` (only a literal list or a task's plain return value is converted)"
                    break
            if why is None:
                for tid in members:
                    t = tasks[tid]
                    if t.get("is_mapped"):
                        why = f"{tid} is a mapped task inside the mapped group"
                    elif t["type"] in BRANCH | SHORT | CLOCK_BRANCH or t.get("is_setup") or t.get("is_teardown"):
                        why = f"{tid} branches, short-circuits, or has a setup/teardown role (not converted inside a mapped group)"
                    elif t["type"] in SENSORS:
                        why = f"{tid} is a sensor inside the mapped group"
                    if why:
                        break
        if why:
            bad[gid] = why
        else:
            good[gid] = {"members": members, "srcs": srcs, "mode": "kwargs" if g["form"] == "expand_kwargs" else "kw"}
    return good, bad


def plan_graph(dag: dict[str, Any], tasks: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Dynamic task mapping as Dagster dynamic outputs: the whole DAG becomes one graph asset in which a mapped task is a mapped op
    and a task after it receives the collected list. Python and shell tasks only; `expand` with literal lists or other tasks' outputs."""
    mapped = {k: t for k, t in tasks.items() if t.get("is_mapped")}
    gated = any(t["type"] in BRANCH | SHORT | CLOCK_BRANCH or (t["type"] in SENSORS and (t.get("sensor") or {}).get("soft_fail"))
                or t["trigger_rule"] in GATE_RULES - PLAIN_RULES or t.get("is_setup") or t.get("is_teardown")
                or re.search(r"AirflowSkipException|task\.(run_if|skip_if)\(", (t.get("callable") or {}).get("source", "")) or bash_skips(t) for t in tasks.values())
    groups = dag.get("_mapped_groups_plan") or {}
    if not mapped and not gated and not groups:
        return None, []
    why: list[str] = []
    for tid, t in tasks.items():
        if t["type"] not in ELIDED | PY_TASKS | BASH | BASH_DEC | BRANCH | SHORT | CLOCK_BRANCH | SENSORS | S3_OPS | SNOWFLAKE_OPS | PROV and not is_custom(t):
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
            mode = "kwargs" if e["form"] == "expand_kwargs" else ("args" if list(srcs) == ["op_args"] else
                                                                  "opargs" if t["type"] in PY_TASKS and not is_custom(t) and set(srcs) <= {"op_args", "op_kwargs"} else "kw")      # opargs: `expand(op_args=..., op_kwargs=...)`, cross product of the two (each item's args and kwargs)
            if is_custom(t):
                bad = ["op_args"] if mode == "args" else []                  # the item's values are constructor arguments of the user-defined operator
            elif t["type"] in S3_OPS | SNOWFLAKE_OPS:
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
    return {"mode": "graph", "mapped": mp, "groups": groups}, []


def plan_mapping(dag: dict[str, Any], tasks: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    if tasks and all(t["type"] in ELIDED for t in tasks.values()):
        return {"mode": "graph", "mapped": {}}, []              # nothing to run, but the steps and their trigger rules are kept
    if MAP_MODE == "graph" or dag.get("_mapped_groups_plan") or any(t["type"] in BRANCH | SHORT | CLOCK_BRANCH or (t["type"] in SENSORS and (t.get("sensor") or {}).get("soft_fail"))
                                  or t["trigger_rule"] in GATE_RULES - PLAIN_RULES or t.get("is_setup") or t.get("is_teardown")
                                  or re.search(r"AirflowSkipException|task\.(run_if|skip_if)\(", (t.get("callable") or {}).get("source", "")) or bash_skips(t) for t in tasks.values()):
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


def retry_delay_s(t: dict[str, Any]) -> int:
    """The task's retry delay in seconds: Airflow's default is 5 minutes, but an explicit zero is zero."""
    return 300 if t.get("retry_delay_s") is None else int(t["retry_delay_s"])


def sla_wrap(body: str, t: dict[str, Any], key: str) -> str:
    """An Airflow `sla` is a deadline after the scheduled time; a miss is recorded and the task still runs. The step reports a breach as an observation on its asset."""
    if not t.get("sla_s"):
        return body
    inner = "\n".join(("    " + ln if ln.strip() else ln) for ln in body.split("\n"))
    return f"    try:\n{inner}\n    finally:\n        af_sla_check(context, {float(t['sla_s'])!r}, {key!r})"


COMPONENT_TASKS = {"BashOperator", "EmptyOperator", "DummyOperator", "DockerOperator"} | {"PythonOperator", "_PythonDecoratedOperator"} | {"BashSensor", "TimeSensor"}
# Service-calling operators are NOT asset components here: each is an asset in `defs/<dag>_steps.py` that calls a resource in plain Python (see registry_map.py).
STEP_TASKS = (S3_OPS | PROV_STEPS | {"SqlQuery"}) - {"DockerOperator"}
REG_RESOURCES: dict[str, tuple[str, dict[str, Any]]] = {}      # resource key -> (registry component type, attributes), written once to defs/registry_resources/defs.yaml


def _plain(v: Any) -> bool:
    """A value that can sit in component YAML as is: no template markers, nothing that refers to another task."""
    if isinstance(v, str):
        return "{{" not in v and "{%" not in v and "{#" not in v
    if isinstance(v, dict):
        return all(_plain(x) for x in v.values()) and not ({"xcom_from", "repr", "seconds", "datetime", "object_storage_path", "group_arg"} & set(v))
    if isinstance(v, (list, tuple)):
        return all(_plain(x) for x in v)
    return True


def component_files(dag: dict[str, Any], summary: dict[str, Any], pkg: str) -> tuple[dict[str, str], dict[str, Any]] | None:
    """The DAG as registry components, or None when some part of it has no component form (the caller then keeps the generated Python).

    Returns ({relative path: content}, summary overrides). One component per task in a `defs.yaml` (documents separated by `---`), a schedule
    component for the cron, and a module with the copied callables for Python tasks."""
    dag = normalise_provider_tasks(dag)
    own = {t_["task_id"] for t_ in dag["tasks"] if t_["type"] in STEP_TASKS | {"DockerOperator"}}      # tasks whose Python-route caveats are replaced or carried below
    if summary.get("mapping") or summary.get("partitions") or summary.get("triggers") or summary.get("pools") or summary.get("max_active_runs") \
            or summary.get("run_timeout_s") or any(c.get("task") not in own for c in summary.get("caveats") or []) or summary.get("uses_context") or summary.get("snowflake_resources"):
        return None
    tasks = {t["task_id"]: t for t in dag["tasks"]}
    dag_id, name = dag["dag_id"], safe(dag["dag_id"])
    if any(t["type"] not in COMPONENT_TASKS | STEP_TASKS or t.get("is_mapped") or t["trigger_rule"] != "all_success" or t["outlets"] or t["inlets"] or t.get("sla_s")
           or t.get("is_setup") or t.get("is_teardown") or t.get("callback_fns") or t.get("callbacks") or t.get("pool")
           or (t.get("sensor") or {}).get("soft_fail")
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
    reg_res: dict[str, tuple[str, dict[str, Any]]] = {}
    step_fns: list[str] = []
    step_caveats: list[dict[str, Any]] = []
    step_reqs: set[str] = set()
    step_resources: set[str] = set()
    step_conns: dict[str, str] = {}
    uses_os = False

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
            attrs["retry_policy_delay_seconds"] = retry_delay_s(t)
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
        elif ty in STEP_TASKS:                                    # a service call: an asset in defs/<dag>_steps.py that calls a resource in plain Python
            a_ = P._a(t)
            if t.get("execution_timeout_s") or not _plain([a_, tf]):
                return None
            ex_ = lambda v: expr(v, {}, None)
            try:
                plan = RM.plan_for(t, ex_)
                if plan is not None:
                    reg_res[plan.resource_key] = (plan.resource_type, plan.attrs)
                    step_reqs.update(plan.requirements)
                    step_caveats += [{"task": tid, "kind": "degraded", "reason": f"registry resource {RM.RESOURCE_TYPE_IDS[plan.resource_type]}: {c}"} for c in plan.caveats]
                    step_caveats.append({"task": tid, "kind": "degraded", "reason": f"registry resource {RM.RESOURCE_TYPE_IDS[plan.resource_type]} was run against a recording fake, not a live service: check it against your environment"})
                    stmt, keys_, kind_ = plan.body, {plan.resource_key}, RM.RESOURCE_TYPE_IDS[plan.resource_type].split("_resource")[0]
                    if plan.needs_os:
                        uses_os = True
                elif ty in S3_OPS:
                    stmt, conn_ = s3_call(t, {}, None)
                    step_resources.add(conn_)
                    step_reqs.add("dagster-aws")
                    stmt = f"client = getattr(context.resources, {conn_!r})\n" + (stmt if stmt.startswith("return") else stmt)
                    keys_, kind_ = {conn_}, "s3"
                else:
                    if P.handler_for(t).check(t)[0]:
                        return None
                    pv = P.handler_for(t).build(t, ex_)
                    if pv.conn:
                        step_conns[pv.conn] = pv.kind
                    step_reqs.update(P.REQUIREMENTS.get(pv.kind, []))
                    stmt, keys_, kind_ = pv.stmt, ({pv.conn} if pv.resource else set()), pv.kinds_tag
                    step_caveats += [c for c in summary.get("caveats") or [] if c.get("task") == tid]
            except Defer:
                return None
            stmt = stmt[len("return "):] if stmt.startswith("return ") else stmt
            retry = ""
            if t["retries"]:
                retry = (f", retry_policy=dg.RetryPolicy(max_retries={int(t['retries'])}, delay={retry_delay_s(t)}, "
                         f"backoff=dg.Backoff.{'EXPONENTIAL' if t.get('retry_exponential_backoff') else 'LINEAR'})")
            step_fns.append(f"@dg.asset(key=dg.AssetKey({aname[tid]!r}), group_name={name!r}, deps=[{', '.join(f'dg.AssetKey({u!r})' for u in ups)}], "
                            f"required_resource_keys={('{' + ', '.join(repr(k) for k in sorted(keys_)) + '}') if keys_ else 'set()'}, kinds={{{kind_!r}}}{retry})\n"
                            f"def {safe(aname[tid])}(context: dg.AssetExecutionContext):\n" + "".join(f"    {ln}\n" for ln in stmt.split("\n")) + "    return None\n")
        elif ty == "DockerOperator":                              # registry component docker_container_asset: a container run to completion is a job, not a service call
            a_ = P._a(t)
            if P.handler_for(t).check(t)[0] or not _plain(a_):
                return None
            import shlex
            if any(a_.get(k) for k in ("user", "working_dir", "entrypoint", "tty", "force_pull", "auto_remove")) or not isinstance(a_.get("image"), str):
                return None
            cmd_ = a_.get("command")
            attrs.update(image=a_["image"], command=(shlex.split(cmd_) if isinstance(cmd_, str) else cmd_) or None, env_vars=a_.get("environment") or None, network=a_.get("network_mode") or None)
            doc("DockerContainerAssetComponent", attrs)
        elif ty in ("BashSensor", "TimeSensor"):                 # wait_until_asset
            sn = t.get("sensor") or {}
            attrs.update(poll_seconds=sn.get("poke_interval"), timeout_seconds=sn.get("timeout"))
            if ty == "BashSensor":
                cmd = (t.get("template_fields") or {}).get("bash_command")
                if not isinstance(cmd, str) or not _plain(cmd) or (t.get("template_fields") or {}).get("env") or (a.get("retry_exit_code") is None):
                    return None                                  # without retry_exit_code Airflow keeps polling on any failure; the component fails at once
                attrs.update(condition="shell_exit_zero", command=cmd, retry_exit_code=a["retry_exit_code"])
            else:
                import datetime as _d
                import zoneinfo
                tzn = summary.get("timezone") or "UTC"
                if not sn.get("target_datetime"):
                    return None
                at = _d.datetime.fromisoformat(sn["target_datetime"]).astimezone(zoneinfo.ZoneInfo(tzn)).time()
                if at.second or at.microsecond:
                    return None                                  # time_of_day has minute precision
                attrs.update(condition="time_of_day", time_of_day=at.strftime("%H:%M"), timezone=tzn)
            doc("WaitUntilAssetComponent", attrs)
        else:
            return None
    if summary.get("schedule"):
        doc("CronScheduleComponent", {"schedule_name": f"{name}_schedule", "job_name": name, "cron_expression": summary["schedule"],
                                      "asset_keys": [aname[t["task_id"]] for t in real], "execution_timezone": summary.get("timezone") or "UTC"})
    for k_, (typ_, at_) in reg_res.items():
        if k_ in REG_RESOURCES and REG_RESOURCES[k_] != (typ_, at_):
            return None                                           # the same resource key with different settings in another DAG: keep this DAG as Python
    files = {f"defs/{name}/defs.yaml": "\n---\n".join(docs) + "\n"} if docs else {}
    if len(callables) > 2:
        files[f"callables/{name}.py"] = "\n".join([callables[0], "", *[s_ for s_, _ in sorted(support.items(), key=lambda kv: kv[1])], "", *callables[1:]])
    if step_fns:
        files[f"defs/{name}_steps.py"] = ('"""Service-calling tasks: each is an asset that calls a resource in plain Python (the resources are in defs/registry_resources/defs.yaml and resources.py)."""\n'
                                           + ("import os\n" if uses_os else "") + "import dagster as dg\n\nfrom ..runtime import *  # noqa: F401,F403\n\n\n" + "\n\n".join(step_fns))
    REG_RESOURCES.update(reg_res)
    prov_reqs_all = set()
    for t_ in tasks.values():
        h_ = P.handler_for(t_)
        if h_ is not None and t_["type"] in PROV_STEPS:
            try:
                prov_reqs_all.update(P.REQUIREMENTS.get(h_.build(t_, lambda v: "None").kind, []))
            except Exception:
                pass
    reqs = (set(summary.get("requirements") or []) - prov_reqs_all - {"dagster-aws"}) | step_reqs | ({RM.DCC_REQUIREMENT} if (reg_res or docs) else set())
    reqs.discard("dagster-community-components")
    comp_caveats = [c for c in step_caveats if c["task"] in tasks]
    overrides = {"caveats": comp_caveats, "keys": [[aname[t["task_id"]]] for t in real], "task_keys": {t["task_id"]: aname[t["task_id"]] for t in real}, "output": "components",
                 "external": [], "resources": sorted(step_resources), "provider_connections": step_conns, "requirements": sorted(reqs),
                 "resource_types": sorted({typ_ for typ_, _ in reg_res.values()}), "registry_resources": sorted(reg_res),
                 "notes": [*summary.get("notes", []), "registry components: asset keys are `<dag>__<task>`, single-part; shell, Python, container and wait tasks are asset components (retries and timeouts are component settings); "
                 "service-calling tasks are assets in defs/<dag>_steps.py that call a resource in plain Python" + ("; the DAG has no schedule so no job is defined" if not summary.get("schedule") else "")]}
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
        def loop_mapped_ok(t):                                               # `.expand(name=<an XCom of a task in this group>)`: the supported mapped shape inside a loop body
            ex = t.get("expand") or {}
            return (ex.get("form") == "expand" and not ex.get("op_args") and not ex.get("fixed") and not ex.get("partial") and bool(ex.get("args"))
                    and all(isinstance(v, dict) and v.get("kind") == "xcom" and v.get("wrapper") == "PlainXComArg" and v.get("key") == "return_value" and v.get("task_id") in ids for v in ex["args"].values()))
        ok = bool(body) and all(t["type"] in PY_TASKS and (not t.get("is_mapped") or loop_mapped_ok(t)) and isinstance(t.get("callable"), dict) and "source" in t["callable"] for t in body)
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
            params_ = t["callable"].get("params") or []
            if "loop" in params_:
                a.append("loop=loop")
            if t.get("is_mapped"):                                           # one call per item (the cross product when several arguments are mapped); `ti.map_index` is the item's position
                keys_ = list(t["expand"]["args"])
                srcs_ = ", ".join(f"v[{t['expand']['args'][k]['task_id']!r}]" for k in keys_)
                ti_ = ", ti=af_loop_ti(" + repr(tid) + ", _i)" if "ti" in params_ else ""
                a_ = ", ".join(a + [f"{k}=_x[{n}]" for n, k in enumerate(keys_)])
                calls.append(f"        v[{tid!r}] = [{fns[tid]}({a_}{ti_}) for _i, _x in enumerate(itertools.product({srcs_}))]")
                continue
            if "ti" in params_:
                a.append(f"ti=af_loop_ti({tid!r}, None)")
            calls.append(f"        v[{tid!r}] = {fns[tid]}({', '.join(a)})")
        outer = (f"def loop_{safe(grp)}():\n    import itertools\n" + "\n".join(src) + "\n    def _body(loop):\n        v = {}\n" + "\n".join(calls) + f"\n        return v[{terminal!r}]\n"
                 f"    return af_loop(_body, {until_fn or 'None'}, {int(gate['loop']['max_iterations'])})\n")
        ext_up = sorted({u for t in body for u in t["upstream"] if u not in ids})
        synth = {**gate, "task_id": grp, "type": "PythonOperator", "group": None, "loop": None, "is_mapped": False, "expand": None,
                 "callable": {"name": f"loop_{safe(grp)}", "module": body[0]["callable"].get("module"), "file": body[0]["callable"].get("file"), "source": outer, "params": []},
                 "loop_members": sorted(ids | {gate["task_id"]}),
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


def loop_members_report(dag: dict[str, Any], keyparts: dict[str, Any], fold_into: dict[str, str]) -> dict[str, str]:
    """Airflow's own task ids inside a converted task-group loop (`group.task`, `group.__loop_gate`) -> the asset key of the loop's step, so a comparison can account for them. They have no step of their own."""
    out: dict[str, str] = {}
    for t in dag["tasks"]:
        grp = t["task_id"]
        for m in t.get("loop_members") or []:
            target = fold_into.get(grp, grp)                  # a loop folded into the step that consumes it: that step's asset
            if target in keyparts:
                out[m] = "/".join(keyparts[target])
    return out


def stock_base(t: dict[str, Any]) -> str | None:
    """The stock operator a user-defined class stands for when it only overrides `__init__` (and so inherits `execute`): its nearest base the converter maps, else None."""
    oc = t.get("operator_class") or {}
    if not oc.get("source") or not oc.get("mro"):
        return None
    known = PY_TASKS | BASH | BASH_DEC | S3_OPS | SNOWFLAKE_OPS | PROV | TRIGGER | SENSORS | CLOCK_BRANCH | BRANCH | SHORT
    if t["type"] in known or t["type"] in ELIDED:
        return None
    try:
        cls = next(n for n in ast.parse(oc["source"]).body if isinstance(n, ast.ClassDef))
    except (SyntaxError, StopIteration):
        return None
    if any(not isinstance(st, ast.FunctionDef) or st.name != "__init__" for st in cls.body if not (isinstance(st, ast.Expr) and isinstance(getattr(st, "value", None), ast.Constant))):
        return None                                         # it defines methods or class attributes of its own: its behaviour is its own
    return next((b for b in oc["mro"] if b in known), None)


def normalise_provider_tasks(dag: dict[str, Any]) -> dict[str, Any]:
    """Give provider operators that share a handler one type: SimpleHttpOperator is HttpOperator, and SQL operators on a connection that is not Snowflake
    (SQLExecuteQueryOperator, PostgresOperator, MySqlOperator, ...) are `SqlQuery`. The type the DAG used stays in `orig_type` for messages."""
    out = []
    for t in dag["tasks"]:
        ty = t["type"]
        new = None
        base_ = stock_base(t)
        if base_:                                           # `class MyBash(BashOperator): def __init__(...)`: nothing but the constructor differs, so the task is the stock operator (the instance's own settings are already in the IR)
            t = {**t, "type": base_, "orig_type": ty, "operator_class": None}
            ty = base_
        if t.get("template_missing") and ty in PY_TASKS and isinstance(t.get("callable"), dict) and t["callable"].get("name"):
            path_ = next(iter(t["template_missing"].values()))      # Airflow loads a templated string that ends with the operator's template extension as a file; a missing file fails the task (jinja2.TemplateNotFound) before the callable runs
            cname = t["callable"]["name"]
            t = {**t, "callable": {**t["callable"], "name": cname, "params": [], "closure": None,
                                   "source": f"def {cname}():\n    from jinja2 import TemplateNotFound\n    raise TemplateNotFound({path_!r})\n"},
                 "op_args": [], "op_kwargs": {}, "template_fields": {k: v for k, v in t["template_fields"].items() if k not in ("templates_dict", "op_args", "op_kwargs")}}
        tfiles = {k: v for k, v in (t.get("template_files") or {}).items() if k.startswith(("op_kwargs.", "op_args.", "templates_dict."))}
        if tfiles and ty in PY_TASKS:                       # `templates_exts`: a callable argument naming a template file receives the file's text (rendered like any template string)
            kw, ar = dict(t.get("op_kwargs") or {}), list(t.get("op_args") or [])
            td = dict((t.get("template_fields") or {}).get("templates_dict") or {})
            for k, c in tfiles.items():
                kind, name = k.split(".", 1)
                if kind == "op_kwargs":
                    kw[name] = c
                elif kind == "templates_dict":
                    td[name] = c
                else:
                    ar[int(name)] = c
            t = {**t, "op_kwargs": kw, "op_args": ar, "template_fields": {**t["template_fields"], "op_kwargs": kw, "op_args": ar, **({"templates_dict": td} if td else {})}}
        if ty in P.ALIASES:
            new = P.ALIASES[ty]
        elif ty in P.SQL_OPS and not (ty == "SQLExecuteQueryOperator" and "snowflake" in P.sql_conn({**t, "type": ty}).lower()):
            new = "SqlQuery"
        out.append({**t, "type": new, "orig_type": ty} if new else t)
    return {**dag, "tasks": out}


def tname(t: dict[str, Any]) -> str:
    return t.get("orig_type") or t["type"]


def convert_dag(dag: dict[str, Any], note: str, produced: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """(module source, summary). Raises Defer if any part of the DAG cannot be converted."""
    USER_MACROS.clear()
    USER_MACROS.update(dag.get("user_defined_macros") or {})
    dag = normalise_provider_tasks(dag)
    dag, cosmos_clusters, cosmos_stubs = K.plan(dag, produced, DBT_DIR)        # Cosmos groups become dbt components, per-task operators become steps, the rest stub
    task_info: dict[str, dict[str, Any]] = {}            # per asset-form task: key, dependencies, pool, retries (written into the generated test)
    check_blocks: list[str] = []                         # generated structural asset checks
    check_list: list[dict[str, str]] = []
    cosmos_steps: list[dict[str, Any]] = []
    cosmos_files: dict[str, str] = {}
    cosmos_reqs: set[str] = set()
    reasons: list[str] = []
    caveats: list[dict[str, str]] = []                         # kind "stub": a task written as a marked stub; "degraded": translated with a known difference
    catchup_note = ""
    ext_trigs: list[str] = []

    dop_tasks: set[str] = set()                               # tasks whose asset step starts with the depends_on_past guard (the library's verified check)

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
    mg_good, mg_bad = plan_groups(dag, {t["task_id"]: t for t in dag["tasks"]})
    dag = {**dag, "_mapped_groups_plan": mg_good}
    if CAVEATS == "off":
        reasons += [f"{tid}: {why}" for tid, why in cosmos_stubs.items()]
        reasons += [f"{t['task_id']}: in the mapped task group {t['mapped_group']!r} (not converted: {mg_bad[t['mapped_group']]})" for t in dag["tasks"] if t.get("mapped_group") in mg_bad]
    if CAVEATS != "off":                                     # tasks the converter cannot translate become marked stubs; the DAG keeps its shape
        soft_known = ELIDED | PY_TASKS | BASH | BASH_DEC | S3_OPS | SNOWFLAKE_OPS | PROV | SENSORS | TRIGGER | BRANCH | SHORT | CLOCK_BRANCH | COSMOS_STEP

        def soft_reason(t: dict[str, Any]) -> str | None:
            if t["task_id"] in cosmos_stubs:
                return cosmos_stubs[t["task_id"]]
            if t.get("mapped_group") in mg_bad:                # the group's expand() values reach its tasks as arguments that cannot be wired: say so instead of running with a dangling argument
                return f"{tname(t)} is in the mapped task group {t['mapped_group']!r} (@task_group.expand(...)): this group is not converted: {mg_bad[t['mapped_group']]}"
            if t["type"] in TRIGGER:
                tgt = (t.get("args") or {}).get("trigger_dag_id")
                if isinstance(tgt, str) and "{{" not in tgt and tgt not in ALL_DAG_IDS:
                    return f"TriggerDagRunOperator target DAG {tgt!r} is not among the converted DAGs"
            h = P.handler_for(t)
            if h:                                              # a provider operator: its own refusals, then the template check below
                if t.get("is_mapped"):
                    return f"mapped {tname(t)} is not converted (only mapped S3 and Snowflake operators are)"
                stubs, _ = h.check(t)
                if stubs:
                    return f"{tname(t)}: " + "; ".join(stubs)
            if t["type"] not in soft_known and not is_custom(t):
                hint = OPERATOR_COMPONENTS.get(t["type"])
                return f"operator {t['type']} has no conversion yet" + (f" (registry: {hint})" if hint else "")
            if is_custom(t):                                   # a user-defined operator that cannot be copied or built from plain arguments
                if unplain_ctor_args(t):
                    return f"constructor argument(s) {unplain_ctor_args(t)} of {t['type']} cannot be written as a plain value (the IR could only record their repr)"
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
                    mk_ = mapper_kind(ser.get('default_partition_mapper'))[0]
                    reasons.append(f"a runtime-partitioned upstream (the producer chooses partition keys while it runs: dynamic partitions) with the {mk_}: "
                                   + ("each upstream key fans out to several downstream keys" if mk_ == "FanOutMapper" else "several upstream keys roll up into one downstream key" if mk_ == "RollupMapper" else "a non-identity mapping")
                                   + ". Not converted: Dagster has no built-in mapping between dynamic partition sets of different keys (only the identity mapping is converted); the equivalent is a custom "
                                   "`dg.PartitionMapping` subclass on the downstream asset's `AssetDep` plus a sensor that adds the downstream keys, which depends on how Airflow's mapper derives them from the key")
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
        if "callback_fns" in dag:                                   # the IR carries the callables: on_success/on_failure become run-status sensors; sla_miss_callback has no equivalent
            dag_unconv = ["sla_miss_callback"] if dag.get("sla_miss_callback") else []
            if not (dag.get("callback_fns") or {}) and not dag_unconv:
                dag_unconv = ["on_success_callback / on_failure_callback (not callable objects the IR could copy)"]
        else:
            dag_unconv = ["on_failure_callback", "on_success_callback", "sla_miss_callback"]       # an IR from before the callbacks were recorded: re-extract
        if dag_unconv and CAVEATS != "off":
            caveats.append({"task": "", "kind": "degraded", "reason": f"DAG-level callbacks {dag_unconv} were not converted (" + ("re-extract the IR to convert on_success_callback / on_failure_callback; " if "callback_fns" not in dag else "") + "Airflow's sla_miss_callback has no Dagster run-level equivalent: use freshness checks or an alert policy)"})
        elif dag_unconv:
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
                if flag == "depends_on_past" and DOP_GUARD and not mplan and not ppart and ty in PY_TASKS | BASH | BASH_DEC | PROV_STEPS | S3_OPS | SNOWFLAKE_OPS:
                    dop_tasks.add(tid)            # asset form: the library's step-event guard runs first in the step
                    soften(f"{tid}: depends_on_past is checked by failing the step when the previous run of the task failed (Airflow waits instead); a first run, or a run after a success, goes ahead", tid)
                else:
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
        runif_ = bool(re.search(r"@task\.(?:run_if|skip_if)\(", (t.get("callable") or {}).get("source", "")))      # `@task.run_if` / `skip_if` ARE a pre_execute hook, and the converter runs them as the skip condition (af_run_if)
        cbs = [c for c in (t.get("callbacks") or []) if not (t["is_mapped"] and c in ("pre_execute", "post_execute")) and not (runif_ and c == "pre_execute")]   # a mapped wrapper overrides them; mapping already defers
        cb_ok = ("on_failure_callback", "on_success_callback", "on_retry_callback", "on_execute_callback")       # converted (asset form only): @on_hooks for the first three, a call at the start of the step for on_execute
        unsupported = [c for c in cbs if c not in cb_ok or not (t.get("callback_fns") or {}).get(c)]
        in_graph = bool(cbs and (mplan and mplan["mode"] == "graph"))
        if unsupported or in_graph:
            what_ = cbs if in_graph else unsupported
            soften(f"{tid}: task callbacks or hooks {what_} were not converted (on_skipped_callback, pre_execute and post_execute have no Dagster equivalent; the callbacks that are converted need the asset form)", tid)
            if CAVEATS != "off" and in_graph:
                t["callback_fns"] = {}
        if t.get("execution_timeout_s") and ty not in BASH | BASH_DEC:
            soften(f"{tid}: execution_timeout on a non-shell task is not enforced (a Python callable cannot be interrupted safely)", tid)
        if len(t["outlets"]) > 1:
            reasons.append(f"{tid}: {len(t['outlets'])} outlets")
        h_ = P.handler_for(t)
        if h_ and not t.get("_caveat"):
            if t.get("is_mapped"):
                reasons.append(f"{tid}: mapped {tname(t)} is not converted")
            else:
                stubs_, deg_ = h_.check(t)
                reasons += [f"{tid}: {tname(t)}: {x}" for x in stubs_]
                for x in deg_:
                    soften(f"{tid}: {tname(t)}: {x}", tid)
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
        if ty not in ELIDED | PY_TASKS | BASH | BASH_DEC | S3_OPS | SNOWFLAKE_OPS | PROV | SENSORS | TRIGGER | COSMOS_STEP and not is_custom(t) and not (mplan and mplan["mode"] == "graph" and ty in BRANCH | SHORT | CLOCK_BRANCH):
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
    has_cosmos = bool(cosmos_clusters) or any(t["type"] in COSMOS_STEP for t in tasks.values())
    if has_cosmos and (gplan or ppart or triggers):
        raise Defer("Cosmos dbt tasks in a DAG with dynamic mapping, branching, partitions or an asset trigger (not converted yet)")

    # in the graph form an EmptyOperator stays a step: its trigger rule is what joins skipped branches. An EmptyOperator that declares outlets or inlets is NOT glue: the
    # outlet is the dataset it publishes, so it stays an asset (returning None) and carries the asset key
    elided = set() if gplan else {tid for tid, t0 in tasks.items() if t0["type"] in ELIDED and not (t0["outlets"] or t0["inlets"])}

    def eff_up(tid: str) -> list[str]:
        out: list[str] = []
        for u in tasks[tid]["upstream"]:
            out += eff_up(u) if u in elided else [u]
        return list(dict.fromkeys(out))

    real = [t for t in dag["tasks"] if t["task_id"] not in elided]
    if not real and not cosmos_clusters:
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
    cosmos_out: list[dict[str, Any]] = []
    cosmos_keys: list[list[str]] = []
    for c in cosmos_clusters:
        up = [keyparts[x] for u in c["ext_up_tasks"] for x in (eff_up(u) if u in elided else [u]) if x in keyparts] + c["ext_up_keys"]
        up = [list(k) for k in dict.fromkeys(tuple(k) for k in up)]
        cosmos_files[f"defs/{c['dir']}/defs.yaml"] = K.component_yaml(c, up)
        cosmos_files.update(c["profile"]["generated"])
        cosmos_keys += c["keys"]
        for pl in c["pools"]:
            pool_limits.setdefault(pl, None)
        if len(c["pools"]) > 1:
            caveats.append({"task": c["members"][0], "kind": "degraded", "reason": f"the Cosmos tasks use several pools {c['pools']}; the component takes one op pool, so none was applied"})
        cosmos_reqs.update(["dagster-dbt", *c["requirements"]])
        caveats += [{**cv, "task": cv["task"] or c["members"][0]} for cv in c["caveats"]]
        notes += c["notes"]
        notes.append(f"Cosmos {'DbtDag' if not c['gid'] else 'DbtTaskGroup ' + repr(c['gid'])} ({len(c['members'])} Airflow tasks) became one dbt component, defs/{c['dir']}/defs.yaml: "
                     f"{len(c['keys'])} dbt assets (one per model, seed or snapshot) with lineage between them and the tests as asset checks. The dbt project is used "
                     f"unchanged: copy it from {c['project_path']} to <package>/{c['dbt_dir']}/{c['project']}; with `dg dev` the component refreshes its manifest, in a deployment run "
                     "`dg utils refresh-defs-state` at build time")
        cosmos_out.append({"id": c["id"], "dir": c["dir"], "members": c["members"], "project": c["project"], "airflow_project_path": c["project_path"], "select": c["select"],
                           "exclude": c["exclude"], "selector": c["selector"], "cli_args": c["cli_args"], "mode": c["mode"], "retries": c["retries"], "profiles_generated": sorted(c["profile"]["generated"]), "keys": ["/".join(k) for k in c["keys"]],
                           "upstream_keys": ["/".join(k) for k in up], "profile": c["profile"]["profile"], "target": c["profile"]["target"], "environment": c["env"],
                           "test_behavior": c["test_behavior"], "prefix": c["prefix"], "pools": c["pools"]})
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
                if unplain_ctor_args(t):
                    raise Defer(f"{tid}: constructor argument(s) {unplain_ctor_args(t)} of {t['type']} cannot be written as a plain value (the IR could only record their repr)")
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
        if meta.get("needs_xcom") or {"ti", "task_instance"} & names:
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
    dag_cb: list[tuple[str, str]] = []                             # DAG-level callbacks: (kind, function name), run in run-status sensors
    cb_blocks: list[str] = []
    notifier_classes: set[str] = set()

    def copy_callback(info: Any, fn_name: str, owner: str, kind: str) -> bool:
        """Write one callback (a function, or a notifier instance) as `fn_name`; False (with a degraded caveat) when it cannot be copied."""
        try:
            if info and info.get("notifier"):                      # `on_failure_callback=MyNotifier()`: the class (BaseNotifier is a runtime shim) and an instance with the state it had
                n_ = info["notifier"]
                body_src, sup_ = resolve_operator_class(n_["class"], shims_used, {}, entry="notify", base="BaseNotifier")
                cname = n_["class"]["name"]
                for sline in sup_:
                    support.setdefault(sline, len(support))
                if cname not in notifier_classes:
                    notifier_classes.add(cname)
                    cb_blocks.append(body_src)
                cb_blocks.append(f"{fn_name} = {cname}.__new__({cname})\n{fn_name}.__dict__.update({{{', '.join(f'{k!r}: {v}' for k, v in n_['state'].items())}}})")
                if n_.get("state_dropped"):
                    caveats.append({"task": owner, "kind": "degraded", "reason": f"{kind}: notifier {cname} attributes {n_['state_dropped']} are not plain values and were not copied (its __init__ is not run)"})
                return True
            code, sup = resolve_callable(info, shims_used, {}, context_arg=True)
        except Defer as e:
            if CAVEATS == "off":
                raise
            caveats.append({"task": owner, "kind": "degraded", "reason": f"{kind} could not be copied and was dropped: {e}"})
            return False
        for sline in sup:
            support.setdefault(sline, len(support))
        cb_blocks.append(re.sub(r"^def\s+\w+", f"def {fn_name}", code, count=1))
        return True

    for t in real:
        for kind, fns in (t.get("callback_fns") or {}).items():
            for k, info in enumerate(fns):
                fn_name = f"_af_cb_{safe(t['task_id'])}_{kind[3:-9]}_{k}"
                if copy_callback(info, fn_name, t["task_id"], kind):
                    cb_code.setdefault(t["task_id"], []).append((kind, fn_name))
    for kind, fns in (dag.get("callback_fns") or {}).items():
        for k, info in enumerate(fns):
            fn_name = f"_af_dagcb_{safe(dag_id)}_{kind[3:-9]}_{k}"
            if copy_callback(info, fn_name, "", kind):
                dag_cb.append((kind, fn_name))
    if dag_cb:
        notes.append("DAG-level callbacks run in run-status sensors on this DAG's job (" + ", ".join(sorted({k_ for k_, _ in dag_cb})) + "): they need the sensor daemon, run after the run ends rather than inside it, "
                     "and receive a minimal context (dag, dag_run, run_id, logical_date, reason, exception message; no task instance)")
    if cb_code:
        notes.append("task callbacks: on_execute_callback is called at the start of each attempt; on_success/on_failure/on_retry through `@on_hooks`; ti.state is set to 'running', 'success', 'failed' or "
                     "'up_for_retry' (Airflow's own transient states such as 'scheduled' are not reproduced); notifier instances run through a plain `BaseNotifier` shim (their template_fields are not rendered)")
    uses_ctx = bool(ctx_tasks)
    if gplan and any(ti_inputs.values()):
        if CAVEATS == "off" and any(tid_ in {m_ for G_ in (gplan.get("groups") or {}).values() for m_ in G_["members"]} and ups_ for tid_, ups_ in ti_inputs.items()):
            raise Defer("a mapped task group where a member pulls XComs by task id (not converted yet)")
        grouped_ = {m_ for G_ in (gplan.get("groups") or {}).values() for m_ in G_["members"]}
        for tid_, ups_ in list(ti_inputs.items()):                  # a task that pulls by id outside a mapped group is wired to the producers' values; one inside a group stays a stub
            if ups_ and tid_ in grouped_:
                reason_ = "the callable pulls XComs by task id (ti.xcom_pull) inside a mapped task group, which the graph form does not rebuild yet"
                caveats.append({"task": tid_, "kind": "stub", "reason": reason_})
                resolved[tid_] = (f"def _stub(*args, **kwargs):\n    # AIRFLOW-CAVEAT: {reason_!r}\n    return af_caveat({reason_!r}, skip={CAVEATS == 'skip'})\n", [], {})
                ctx_tasks.discard(tid_)
                ti_inputs[tid_] = []
    if pplan and (uses_ctx or any(resolved[x][2].get("needs_ti") for x in pplan["args"] if x in resolved)):
        raise Defer("dynamic task mapping where a callable uses the task context or pulls XComs (not converted yet)")
    dag_params = dict(dag.get("params") or {})
    if USER_MACROS:                                              # `user_defined_macros={"my_json": ...}`: names a template may use, carried to af_context under a reserved key
        dag_params["__macros__"] = dict(dag["user_defined_macros"])
    graph_outs: list[str] = []

    prov_conns: dict[str, str] = {}                              # connection id -> kind of the provider operators that use it (listed in the report)
    prov_reqs: set[str] = set()

    def prov_for(t: dict[str, Any], params: dict[str, str], ctxn: str | None) -> "P.Prov":
        """The body of a provider operator (see provider_ops.py), with its connection and the packages it needs recorded."""
        pv = P.handler_for(t).build(t, lambda v: expr(v, params, ctxn))
        if pv.conn:
            prov_conns[pv.conn] = pv.kind
        prov_reqs.update(P.REQUIREMENTS.get(pv.kind, []))
        return pv

    def custom_body(t: dict[str, Any], ctx_line: str, params: dict[str, str] | None = None, mapped: bool = False) -> str:
        """A user-defined operator: its copied class, created with the operator's own arguments, then execute(context)."""
        code, sup, _ = resolved[t["task_id"]]
        for sline in sup:
            support.setdefault(sline, len(support))
        support.setdefault(code, len(support))
        args = {k: v for k, v in (t.get("args") or {}).items() if k not in ("task_id",)}
        special = any(isinstance(x, dict) and ({"datetime", "time", "seconds", "object_storage_path", "xcom_from", "group_arg"} & set(x)) for v in args.values() for x in _walk_values(v))
        base = "{" + ", ".join(f"{k!r}: {ctor_expr(v, params or {})}" for k, v in args.items()) + "}" if special else repr(args)
        kw = f"**{{**{base}, **combo}}" if mapped else f"**{base}"        # a mapped operator: the fixed (partial) arguments with this item's
        return ctx_line + f"    op = {t['operator_class']['name']}(task_id={t['task_id']!r}, {kw})\n    return run_operator(op, ctx)"

    def sensor_body(t: dict[str, Any], params: dict[str, str], ctxn: str | None, call_inner: str | None = None) -> str:
        """A sensor as a polling step: Airflow's default `poke` loop (check, give up after the timeout, sleep poke_interval)."""
        ty, tid, tf, a, sn = t["type"], t["task_id"], t["template_fields"], t.get("args") or {}, t.get("sensor") or {}
        tzn = tz_name(tt["timezone"]) if tt.get("timezone") else "UTC"
        setup = ""
        if ty in SENSOR_CALL:
            check = call_inner
        elif ty in P.SENSOR_TYPES:                                     # HttpSensor
            check = prov_for(t, params, ctxn).stmt
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
        groups = gplan.get("groups") or {}                                 # mapped task groups: their member steps are written together, as the body of one graph
        grp_of = {m_: g_ for g_, G_ in groups.items() for m_ in G_["members"]}
        for G_ in groups.values():
            G_.update(lines=[], val={}, ext={}, done=[])

        def visit(x: str) -> None:
            if x in seen_t:
                return
            g_ = grp_of.get(x)
            if g_ is not None and x not in seen_t:
                G_ = groups[g_]
                for m_ in G_["members"]:
                    for u in eff_up(m_):
                        if grp_of.get(u) != g_:
                            visit(u)
                for v_ in G_["srcs"].values():
                    if v_[0] == "task":
                        visit(v_[1])

                def inner(y: str) -> None:
                    if y in seen_t:
                        return
                    seen_t.add(y)
                    for u in eff_up(y):
                        if grp_of.get(u) == g_:
                            inner(u)
                    order.append(y)
                for m_ in G_["members"]:
                    inner(m_)
                return
            seen_t.add(x)
            for u in eff_up(x):
                visit(u)
            order.append(x)
        for t in real:
            visit(t["task_id"])
        mp = gplan["mapped"]
        fail_aware = any(tasks[x]["trigger_rule"] in GATE_RULES - PLAIN_RULES for x in order) or bool(groups)     # a failure must not stop the steps that react to it; inside a mapped task group a failed member is a value, so the group's other members keep their own states
        idx = {tid: i for i, tid in enumerate(order)}
        ops: list[str] = []
        lines: list[str] = []
        outs: dict[str, str] = {}
        val: dict[str, str] = {}          # task -> variable holding its value (a collected list for a mapped task)
        dyn: dict[str, str] = {}          # mapped task -> variable holding its dynamic output
        def emit_group(g_: str, i_: int) -> None:
            """The whole mapped task group: fan-out over its items, the member steps as a graph mapped over them, the collected results, one value per member (a list over the items)."""
            G_ = groups[g_]
            gn = safe(dag_id) + "__" + safe(g_)
            members = G_["members"]
            names = list(G_["srcs"])
            leaf = [v_[1] for v_ in G_["srcs"].values() if v_[0] == "task"]
            leaf = list(dict.fromkeys(leaf))
            fp = [f"src_{k}" for k in range(len(leaf))]
            lists = ", ".join(repr(v_[1]) if v_[0] == "lit" else fp[leaf.index(v_[1])] for v_ in G_["srcs"].values())
            if G_["mode"] == "kwargs":
                item_loop = "    for n, vals in enumerate(lists[0]):\n        yield dg.DynamicOutput(dict(vals), mapping_key=f'i{n}')\n"
            else:
                item_loop = (f"    for n, vals in enumerate(itertools.product(*lists)):\n        yield dg.DynamicOutput(dict(zip({names!r}, vals)), mapping_key=f'i{{n}}')\n")
            ops.append(f"@dg.op(name={gn + '__fan'!r}, out=dg.DynamicOut())\ndef fan_{safe(g_)}({', '.join(fp)}):\n"
                       f"    srcs = [{', '.join(fp)}]\n"
                       "    if any(isinstance(x, Skipped) for x in srcs):\n        return\n"
                       "    if not all(isinstance(x, (list, tuple)) for x in srcs):\n        raise dg.Failure(description='expected a list of items to map over')\n"
                       f"    lists = [{lists}]\n"
                       "    if not all(isinstance(x, (list, tuple)) for x in lists):\n        raise dg.Failure(description='expected a list of items to map over')\n" + item_loop)
            mv = [G_["val"][m_] for m_ in members]
            ops.append(f"@dg.op(name={gn + '__pack'!r})\ndef pack_{safe(g_)}({', '.join(f'm_{k}' for k in range(len(members)))}):\n"
                       f"    return {{{', '.join(f'{m_!r}: m_{k}' for k, m_ in enumerate(members))}}}\n")
            ext = list(G_["ext"].items())
            ops.append(f"@dg.graph(name={gn + '__body'!r})\ndef group_{safe(g_)}({', '.join(['gitem'] + [e for _, e in ext])}):\n"
                       + "\n".join(G_["lines"]) + f"\n    return pack_{safe(g_)}({', '.join(mv)})\n")
            ops.append(f"@dg.op(name={gn + '__collect'!r})\ndef collect_{safe(g_)}(context, items):\n"
                       "    items = list(items)\n    return items if items else af_skip(context, False)      # Airflow skips a mapped task group that expands to nothing\n")
            for k, m_ in enumerate(members):
                ops.append(f"@dg.op(name={gn + '__pick_' + safe(m_)!r})\ndef pick_{safe(g_)}_{k}(context, results):\n"
                           "    if isinstance(results, Skipped):\n        return results                                    # an empty expansion: the group and what follows are skipped\n"
                           f"    vals = [None if isinstance(r[{m_!r}], Skipped) else r[{m_!r}] for r in results]      # the member's value over the items: Airflow's XCom list of its mapped instances\n"
                           "    if any(isinstance(v, Failed) for v in vals):                    # the member's own state: a failed instance fails it, whatever its siblings did\n"
                           "        context.add_output_metadata({'airflow/state': 'failed'})\n"
                           "    elif vals and all(isinstance(v, UpstreamFailed) for v in vals):\n"
                           "        context.add_output_metadata({'airflow/state': 'upstream_failed'})\n"
                           "    return vals\n")
            fan_args = ", ".join(val[x] for x in leaf)
            ext_args = "".join(f", {val[u]}" for u, _ in ext)
            lines.append(f"    gd{i_} = fan_{safe(g_)}({fan_args}).map(lambda item: group_{safe(g_)}(item{ext_args}))")
            lines.append(f"    gr{i_} = collect_{safe(g_)}(gd{i_}.collect())")
            for k, m_ in enumerate(members):
                lines.append(f"    v{idx[m_]} = pick_{safe(g_)}_{k}(gr{i_})")
                val[m_] = f"v{idx[m_]}"

        for tid in order:
            t = tasks[tid]
            ty = t["type"]
            ups = [u for u in eff_up(tid) if u in keyparts]
            i = idx[tid]
            g = grp_of.get(tid)
            if g is not None:                                           # a member of a mapped task group: written into the group's body, its outside inputs passed in
                G = groups[g]
                for u in ups:
                    if grp_of.get(u) != g and u not in G["ext"]:
                        G["ext"][u] = f"ext_{len(G['ext'])}"
                        G["val"][u] = G["ext"][u]
                saved_ctx = (lines, val)
                lines, val = G["lines"], G["val"]
                GROUP_ITEM[0] = "gitem"
            fname = safe("op_" + tid)
            deco = [f"name={safe(dag_id) + '__' + safe(tid)!r}"]
            if t["retries"]:                                            # declared even when a failure guard (fail_aware) raises the RetryRequested itself: the policy then states the same limit and delay
                deco.append(f"retry_policy=dg.RetryPolicy(max_retries={t['retries']}, delay={retry_delay_s(t)}{', backoff=dg.Backoff.EXPONENTIAL' if t.get('retry_exponential_backoff') else ''})")
            if t["pool"]:
                deco.append(f"pool={t['pool']!r}")
            elif f"{safe(dag_id)}__{safe(tid)}" in pool_limits:
                deco.append(f"pool={safe(dag_id) + '__' + safe(tid)!r}")
            m = mp.get(tid)
            src_tasks = {x for v in m["srcs"].values() for x in src_leaves(v)} if m else set()
            extras = [u for u in ups if u not in src_tasks]
            params = {u: f"in_{k}" for k, u in enumerate(extras if m else ups)}
            pulls = [u for u in ti_inputs.get(tid, []) if g is None and u not in ups and u in val and u in keyparts]      # `ti.xcom_pull(task_ids=...)` of a task that is not a direct upstream: its value is wired in too (it is not part of the skip logic)
            pparams = {u: f"pl_{k}" for k, u in enumerate(pulls)}
            ctxn = "ctx" if tid in ctx_tasks else None
            ctx_line = (f"    ctx = af_context(context, {dag_id!r}, {tid!r}, {({**dag_params, **(t.get('params') or {})})!r}, "
                        "{" + ", ".join(f"{u!r}: {v_}" for u, v_ in {**params, **pparams}.items()) + "}"
                        + (", keys=" + repr({u: keyparts[u] for u in ti_inputs[tid] if u in keyparts}) if ti_inputs.get(tid) else "") + ")\n") if ctxn else ""
            sig = (["context: dg.OpExecutionContext"] + (["gitem"] if g is not None else []) + (["item"] if m else []) + list(params.values()) + list(pparams.values()))
            if m:
                single = len(m["srcs"]) == 1
                kwexpr = ({"kw": f"{{{list(m['srcs'])[0]!r}: item}}", "opargs": f"{{{list(m['srcs'])[0]!r}: item}}", "args": "{}", "kwargs": "dict(item)"}[m["mode"]] if single else "dict(item)")
                combo = f"    combo = {kwexpr}\n"
            if is_custom(t):
                body = (combo if m else "") + custom_body(t, ctx_line, params, mapped=bool(m))
            elif ty in SENSORS - SENSOR_CALL:
                if ty in P.SENSOR_TYPES and prov_for(t, params, ctxn).resource:
                    deco.append(f"required_resource_keys={{{prov_for(t, params, ctxn).conn!r}}}")
                body = ctx_line + sensor_body(t, params, ctxn)
            elif ty in PY_TASKS | BASH_DEC | BRANCH | SHORT | SENSOR_CALL:
                code, sup, meta = resolved[tid]
                for sline in sup:
                    support.setdefault(sline, len(support))
                pyname = "_af_" + safe(tid)
                ops.append(re.sub(r"^(async\s+)?def\s+\w+", lambda mm: f"{mm.group(1) or ''}def {pyname}", code, count=1) + "\n")
                if meta.get("async"):
                    ops.append(f"{pyname} = af_sync({pyname})\n")
                for run_, cond_ in meta.get("conds") or []:
                    ops.append(f"{pyname} = af_run_if({pyname}, {cond_}, run={run_!r})\n")
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
                    kw = f"{{**{m['fixed']!r}, **combo.get('op_kwargs', {{}})}}" if m["mode"] == "opargs" else f"{{**{m['fixed']!r}, **combo}}"
                    posl = "list(item)" if m["mode"] == "args" else "list(combo.get('op_args', ()))" if m["mode"] == "opargs" else "[]"
                    inner = (f"call_with_context({pyname}, {posl}, {kw}, ctx)" if meta.get("needs_ctx")
                             else f"{pyname}(*{posl}, **{kw})")
                else:
                    try:
                        pos = [expr(a, params, ctxn) for a in (t.get("op_args") or [])]
                        kws = {k: expr(v, params, ctxn) for k, v in (t.get("op_kwargs") or {}).items()}
                        if isinstance((t.get("template_fields") or {}).get("templates_dict"), dict) and ("templates_dict" in str((t.get("callable") or {}).get("params", "")) or "templates_dict" in str((t.get("callable") or {}).get("source", ""))):
                            kws["templates_dict"] = expr(t["template_fields"]["templates_dict"], params, ctxn)      # rendered templates handed to the callable
                        asset_fn_kwargs(t, kws)
                        inner = (f"call_with_context({pyname}, [{', '.join(pos)}], {{{', '.join(f'{k!r}: {v}' for k, v in kws.items())}}}, ctx)"
                                 if meta.get("needs_ctx") else f"{pyname}(" + ", ".join(pos + [f"{k}={v}" for k, v in kws.items()]) + ")")
                    except Defer as e:
                        if CAVEATS == "off":
                            raise
                        caveats.append({"task": tid, "kind": "stub", "reason": str(e)})
                        inner = f"af_caveat({str(e)[:300]!r}, skip={CAVEATS == 'skip'})"      # AIRFLOW-CAVEAT: the call's arguments could not be wired
                if ty in BASH_DEC:
                    body = ((combo if m else "") + (ctx_line or f"    ctx = af_context(context, {dag_id!r}, {tid!r}, {({**dag_params, **(t.get('params') or {})})!r}, {{}})\n") + f"    cmd = {inner}\n    if not isinstance(cmd, str) or not cmd.strip():\n"
                            "        raise dg.Failure(description='@task.bash must return a non-empty command string')\n"
                            + "    if '{{' in cmd or '{%' in cmd:                      # Airflow renders the command a @task.bash returns as a template\n        cmd = render(ctx, cmd)\n" +
                            f"    return run_shell(context, cmd, env={t['template_fields'].get('env')!r}, cwd={t['template_fields'].get('cwd')!r}, timeout={t.get('execution_timeout_s')!r}{', xcom_json=True' if AF_MAJOR >= 3 else ''}{skx(t)})")
                elif ty in SENSOR_CALL:
                    body = ctx_line + sensor_body(t, params, ctxn, inner)
                elif ty in BRANCH:
                    body = ctx_line + f"    return BranchResult({inner})"
                elif ty in SHORT:
                    body = ctx_line + f"    return bool({inner})"      # the value is the plain bool Airflow records; downstream gates read short-circuit-ness from the graph (sc=)"
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
            elif ty in PROV_STEPS:
                pv = prov_for(t, params, ctxn)
                if pv.resource:
                    deco.append(f"required_resource_keys={{{pv.conn!r}}}")
                body = ctx_line + "".join(f"    {ln}\n" for ln in pv.stmt.split("\n")) + ("" if pv.stmt.startswith("return") else "    return None")
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
                            f"timeout={t.get('execution_timeout_s')!r}{', xcom_json=True' if AF_MAJOR >= 3 else ''}{skx(t)})")
                else:
                    cmd = (t.get("template_files") or {}).get("bash_command") or t["template_fields"].get("bash_command")
                    if not isinstance(cmd, str):
                        raise Defer(f"{tid}: bash_command is not a string")
                    env = t["template_fields"].get("env")
                    body = (ctx_line + f"    return run_shell(context, {expr(cmd, params, ctxn)}, env={expr(env, params, ctxn) if env else 'None'}, "
                            f"cwd={expr(t['template_fields'].get('cwd'), params, ctxn)}, timeout={t.get('execution_timeout_s')!r}{', xcom_json=True' if AF_MAJOR >= 3 else ''}{skx(t)})")
            if ty in BASH | BASH_DEC and t.get("do_xcom_push") is False:      # nothing is pushed: the value downstream is None
                body = body.replace("return run_shell(", "run_shell(") + "\n    return None"
            prefix = ""
            if params:                                                  # skip semantics: branch choices, short-circuits and trigger rules
                names = list(params.values())
                setup_arg = (f", setup={[bool(tasks[u].get('is_setup')) for u in params]!r}" if t["trigger_rule"] == "all_done_setup_success" else "")
                sc_flags = [(bool(tasks[u].get("short_circuit_hard", True)) if tasks[u]["type"] in SHORT else None) for u in params]
                sc_arg = f", sc={sc_flags!r}" if any(f is not None for f in sc_flags) else ""
                prefix = (f"    skip = af_gate(context, {t['trigger_rule']!r}, [{', '.join(names)}], {tid!r}{setup_arg}{sc_arg})\n    if skip is not None:\n        return skip\n"
                          f"    {', '.join(names)} = {', '.join(f'af_data({n})' for n in names)}\n")
            skips = (ty in PY_TASKS | BASH_DEC | BRANCH | SHORT | SENSOR_CALL and resolved.get(tid, ("", [], {}))[2].get("skips")) or bash_skips(t)
            if skips and not fail_aware:
                inner_lines = "\n".join(("    " + ln if ln.strip() else ln) for ln in body.split("\n"))
                body = "    def _run():\n" + inner_lines + "\n    return af_skippable(context, _run)"
            if fail_aware:                                              # the body runs inside a guard that turns a failure into a value
                inner_lines = "\n".join(("    " + ln if ln.strip() else ln) for ln in body.split("\n"))
                body = ("    def _run():\n" + inner_lines +
                        f"\n    return af_guard(context, _run, {int(t['retries'] or 0)}, {retry_delay_s(t)})")
            body = sla_wrap(body, t, "/".join(keyparts[tid]))
            ops.append("@dg.op(" + ", ".join(deco) + ")\ndef " + fname + "(" + ", ".join(sig) + "):\n" + prefix + body + "\n")
            if m:                                                       # the fan-out op, then the mapped op
                lit_names = [n for n, v in m["srcs"].items() if v[0] == "lit"]
                srcs = list(m["srcs"].items())
                chain = len(srcs) == 1 and srcs[0][1][0] == "task" and tasks[srcs[0][1][1]].get("is_mapped")
                extra_call = "".join(f", {val[u]}" for u in [*extras, *pulls])
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
                args = ", ".join((["gitem"] if g is not None else []) + [val[u] for u in [*ups, *pulls]])
                lines.append(f"    v{i} = {fname}({args})")
                val[tid] = f"v{i}"
            outs[tid] = f"o{i}"
            if g is not None:
                lines, val = saved_ctx
                GROUP_ITEM[0] = None
                G["done"].append(tid)
                if len(G["done"]) == len(G["members"]):
                    emit_group(g, i)
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
        # One asset per task, backed by one graph. graph_multi_asset cannot declare the edges between its own outputs, so the graph is built with
        # @dg.graph(out=GraphOut) and AssetsDefinition.from_graph(internal_asset_deps=...): every Airflow edge is an asset dependency.
        keyed = [tid for tid in order if tid in keyparts]
        gname = safe(dag_id) + "_graph"

        def kind_tag(tid: str) -> str:
            return "shell" if tasks[tid]["type"] in BASH | BASH_DEC else "python"
        internal: dict[str, list[list[str]]] = {}
        for tid in keyed:
            internal[outs[tid]] = [keyparts[u] for u in dict.fromkeys(eff_up(tid)) if u in keyparts]
        if fail_aware:
            internal["o_run_check"] = [keyparts[x] for x in keyed]
        out_names = [outs[tid] for tid in keyed] + (["o_run_check"] if fail_aware else [])
        kbo = ",\n        ".join(f"{outs[tid]!r}: dg.AssetKey({keyparts[tid]!r})" for tid in keyed) + (f",\n        'o_run_check': dg.AssetKey({[safe(dag_id), 'run_check']!r})" if fail_aware else "")
        deps_decl = ",\n        ".join(f"{n!r}: " + ("{" + ", ".join(f"dg.AssetKey({k_!r})" for k_ in internal[n]) + "}" if internal[n] else "set()") for n in out_names)
        meta_decl = ",\n        ".join(f"{outs[tid]!r}: {{'airflow_dag': {dag_id!r}, 'airflow_task': {tid!r}, 'airflow_operator': {tasks[tid]['type']!r}}}" for tid in keyed)
        tags_decl = ",\n        ".join(f"{outs[tid]!r}: {{'dagster/kind/{kind_tag(tid)}': ''}}" for tid in keyed) + (",\n        'o_run_check': {'dagster/kind/python': ''}" if fail_aware else "")
        auto_decl = (f"    automation_conditions_by_output_name={{\n        " + ",\n        ".join(f"{outs[tid]!r}: {trig_cond}" for tid in keyed) + "\n    },\n") if trig_cond else ""
        gouts = ", ".join(f"{n!r}: dg.GraphOut()" for n in out_names)
        ret = "{" + ", ".join(f"{outs[tid]!r}: {val[tid]}" for tid in keyed) + (", 'o_run_check': fin" if fail_aware else "") + "}"
        blocks.append("\n\n".join(ops) + f"\n\n@dg.graph(name={gname + '_body'!r}, out={{{gouts}}})\n"
                      f"def {gname}_body():\n" + "\n".join(lines) + f"\n    return {ret}\n\n\n"
                      f"{gname} = dg.AssetsDefinition.from_graph(\n    {gname}_body,\n    group_name={safe(dag_id)!r},\n"
                      f"    keys_by_output_name={{\n        {kbo},\n    }},\n"
                      f"    internal_asset_deps={{\n        {deps_decl},\n    }},\n"
                      f"    metadata_by_output_name={{\n        {meta_decl},\n    }},\n"
                      f"    tags_by_output_name={{\n        {tags_decl},\n    }},\n" + auto_decl + ")\n")
        notes.append("dynamic task mapping became dynamic outputs inside one graph asset: the whole DAG runs as one Dagster run, mapped tasks are mapped ops "
                     "and a task after them receives the collected list. Differences from Airflow: retries and pools apply per op; individual tasks cannot be "
                     "re-run on their own (the asset is the unit), and an empty list skips the mapped step and gives the next step an empty list.")

    # ---- product granularity: which tasks are data products, which are steps folded into the asset they feed, which are glue ------------------------------------
    elided_ids = set(elided)
    fold_into: dict[str, str] = {}
    gran_decisions: dict[str, dict[str, Any]] = {}
    if GRANULARITY == "product":
        cons: dict[str, list[str]] = {t["task_id"]: [] for t in real}
        for t in real:
            for u in eff_up(t["task_id"]):
                if u in cons:
                    cons[u].append(t["task_id"])
        foldable = PY_TASKS - ISOLATED
        for t in dag["tasks"]:
            if t["task_id"] in elided_ids and t["task_id"] not in cons:
                gran_decisions[t["task_id"]] = {"role": "glue", "reason": f"{t['type']} only joins dependencies (or has no effect once schedules do not backfill): not an asset, its edges are rewired"}
        stop = bool(gplan or pplan or ppart or triggers or rt_producer or cosmos_clusters)
        for t in real:
            tid, ty = t["task_id"], t["type"]
            why = None
            if tid in elided_ids:
                gran_decisions[tid] = {"role": "glue", "reason": "an EmptyOperator only joins dependencies: elided"}
                continue
            if stop:
                why = "product folding is not applied to graph-form, partitioned, asset-triggered, runtime-partitioned or Cosmos DAGs"
            elif t["outlets"] or t["inlets"]:
                why = "its output is persisted under an asset key (outlets or inlets)"
            elif ty not in foldable | BASH or t.get("_caveat") or t.get("is_mapped"):
                why = f"{tname(t)} is an external or special step (storage, warehouse, sensor, trigger, branch, mapped, stub), kept as its own asset"
            elif len(cons[tid]) != 1:
                why = "no downstream task consumes it" if not cons[tid] else f"{len(cons[tid])} downstream tasks consume it"
            elif t["retries"] or t.get("execution_timeout_s") or t["pool"] or t.get("max_active_tis_per_dag") or t.get("callback_fns") or t.get("sla_s") or t["trigger_rule"] != "all_success":
                why = "it has its own retries, timeout, pool, callback, SLA or trigger rule: kept as its own restart point"
            elif tid in ctx_tasks or ti_inputs.get(tid) or resolved.get(tid, ("", [], {}))[2].get("async") or resolved.get(tid, ("", [], {}))[2].get("skips"):
                why = "it uses the Airflow context, XCom pulls by task id, async code or skip logic"
            else:
                c_ = cons[tid][0]
                ct = tasks[c_]
                if ct["type"] not in foldable | BASH or ct.get("_caveat") or ct.get("is_mapped") or ct["type"] in ISOLATED:
                    why = f"its consumer {c_} is a {tname(ct)}, which cannot host the step"
                elif ti_inputs.get(c_):
                    why = f"its consumer {c_} pulls XComs by task id"
                else:
                    fold_into[tid] = c_
                    gran_decisions[tid] = {"role": "step", "into": c_, "reason": f"its value feeds only {c_} and nothing persists it or needs its own restart point: plain Python inside {c_}'s asset"}
                    continue
            gran_decisions[tid] = {"role": "product", "reason": why or "it is not a foldable step"}
        direct_fold = dict(fold_into)

        def collapse(d: dict[str, str]) -> dict[str, str]:       # a chain folds into the final consumer
            out_ = dict(d)
            for tid_ in out_:
                while out_[tid_] in out_:
                    out_[tid_] = out_[out_[tid_]]
            return out_
        while True:                                              # folding makes every folded step wait for ALL the asset's upstream assets: a step whose Airflow ancestors lack one of them stays its own asset
            fold_into = collapse(direct_fold)
            groups_: dict[str, list[str]] = {}
            for tid_, c_ in fold_into.items():
                groups_.setdefault(c_, []).append(tid_)
            offender = None
            for c_, members_ in groups_.items():
                ext_ = {u for x in [c_, *members_] for u in eff_up(x) if u not in fold_into}
                for f_ in members_:
                    missing_ = ext_ - set(ancestors(f_))
                    if missing_:
                        offender = (f_, sorted(missing_))
                        break
                if offender:
                    break
            if offender is None:
                break
            f_, missing_ = offender
            del direct_fold[f_]
            gran_decisions[f_] = {"role": "product", "reason": f"folding it into its consumer would make it wait for {', '.join(missing_)}, which Airflow does not make it wait for"}
        fold_into = collapse(direct_fold)
        for tid, c_ in fold_into.items():
            gran_decisions[tid]["into"] = c_
        real = [t for t in real if t["task_id"] not in fold_into]
        notes.append(f"granularity=product: {len(fold_into)} step task(s) were folded into the asset they feed ({', '.join(f'{k} into {v}' for k, v in sorted(fold_into.items())) or 'none'}); "
                     "they restart with that asset (plain Python steps restart from the beginning), so they no longer have asset keys of their own") if fold_into else None

    def folded_chain(tid: str) -> list[str]:
        out: list[str] = []
        for u in eff_up(tid):
            if u in fold_into:
                out += [*folded_chain(u), u]
        return list(dict.fromkeys(out))

    def expand_ups(tid: str) -> list[str]:
        out: list[str] = []
        for u in eff_up(tid):
            out += expand_ups(u) if u in fold_into else ([u] if u in keyparts else [])
        return list(dict.fromkeys(out))

    if gplan:
        emit_graph()
    for t in ([] if gplan else real):
        tid = t["task_id"]
        chain_ = folded_chain(tid) if fold_into else []
        ups = expand_ups(tid) if fold_into else [u for u in eff_up(tid) if u in keyparts]
        refs = xcom_refs(t.get("op_args")) | xcom_refs(t.get("op_kwargs")) | xcom_refs(t["template_fields"])
        for f_ in chain_:
            refs |= xcom_refs(tasks[f_].get("op_args")) | xcom_refs(tasks[f_].get("op_kwargs")) | xcom_refs(tasks[f_]["template_fields"])
        ordered = list(dict.fromkeys([*ups, *ti_inputs.get(tid, [])]))
        params = {u: f"in_{i}" for i, u in enumerate(ordered)}
        for f_ in chain_:
            params[f_] = f"_v_{safe(f_)}"
        data = {u for u in ordered if u in refs or u in ti_inputs.get(tid, [])}
        if pplan and pplan["kind"] == "xcom" and tid in pplan["args"]:
            data.add(pplan["producer"])                       # the mapped task finds its item in the producer's list
        ctxn = "ctx" if tid in ctx_tasks else None
        ctx_line = (f"    ctx = af_context(context, {dag_id!r}, {tid!r}, {({**dag_params, **(t.get('params') or {})})!r}, "
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
            deco.append(f"retry_policy=dg.RetryPolicy(max_retries={t['retries']}, delay={retry_delay_s(t)}{', backoff=dg.Backoff.EXPONENTIAL' if t.get('retry_exponential_backoff') else ''})")
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
                    f"cwd={expr(t['template_fields'].get('cwd'), params, ctxn)}, timeout={t.get('execution_timeout_s')!r}{', xcom_json=True' if AF_MAJOR >= 3 else ''}{skx(t)})")
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
            body = custom_body(t, ctx_line, params)
            if tid not in consumed and not RETURN_ALL:
                body = body.replace("    return run_operator(", "    run_operator(")
            deco.append("kinds={'python'}")
            pre = ""
            ret_none = tid not in consumed and not RETURN_ALL
        elif ty in SENSORS - SENSOR_CALL:
            if ty in P.SENSOR_TYPES and prov_for(t, params, ctxn).resource:
                deco.append(f"required_resource_keys={{{prov_for(t, params, ctxn).conn!r}}}")
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
                asset_fn_kwargs(t, kws)
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
                body = ((ctx_line or f"    ctx = af_context(context, {dag_id!r}, {tid!r}, {({**dag_params, **(t.get('params') or {})})!r}, {{}})\n") + f"    cmd = {inner}\n    if not isinstance(cmd, str) or not cmd.strip():\n"
                        f"        raise dg.Failure(description='@task.bash must return a non-empty command string')\n"
                        + "    if '{{' in cmd or '{%' in cmd:                      # Airflow renders the command a @task.bash returns as a template\n        cmd = render(ctx, cmd)\n" +
                        f"    {'' if ret_none else 'return '}run_shell(context, cmd, env={t['template_fields'].get('env')!r}, cwd={t['template_fields'].get('cwd')!r}, timeout={t.get('execution_timeout_s')!r}{', xcom_json=True' if AF_MAJOR >= 3 else ''}{skx(t)})")
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
        elif ty in COSMOS_STEP:
            cs = t["cosmos_step"]
            cosmos_steps.append({"task": tid, **{k: cs[k] for k in ("project", "project_path", "kind", "args", "profile", "target", "profiles_dir", "env", "mode")}, "profiles_generated": sorted(cs["generated"])})
            cosmos_files.update(cs["generated"])
            cosmos_reqs.update(["dagster-dbt", *cs["requirements"]])
            body = ctx_line + K.step_body(cs)
            deco.append("kinds={'dbt'}")
            pre = ""
            ret_none = True
            caveats.append({"task": tid, "kind": "degraded", "reason": "a per-task Cosmos operator runs one dbt command from a `select` string and never records which nodes it ran, so this is one asset "
                                                                         "without model-level lineage; use a DbtDag or DbtTaskGroup (or build a dbt component by hand) for per-model assets"})
            for text in cs["profile_notes"]:
                caveats.append({"task": tid, "kind": "degraded", "reason": text})
            notes += [f"{tid}: {x}" for x in cs["profile_info"]]
            if cs.get("has_callback"):
                caveats.append({"task": tid, "kind": "degraded", "reason": "the Cosmos `callback` is not applied"})
            if cs.get("mode_note"):
                notes.append(cs["mode_note"])
        elif ty in PROV_STEPS:
            pv = prov_for(t, params, ctxn)
            if pv.resource:
                deco.append(f"required_resource_keys={{{pv.conn!r}}}")
            deco.append(f"kinds={{{pv.kinds_tag!r}}}")
            stmt = pv.stmt
            if ret_none and stmt.startswith("return "):         # nothing reads the value: do not store it
                stmt = stmt[len("return "):]
            body = ctx_line + "".join(f"    {ln}\n" for ln in stmt.split("\n")).rstrip("\n")
            pre = ""
            ret_none = not stmt.startswith("return")
        elif ty in SNOWFLAKE_OPS:
            stmt, conn = snowflake_call(t, ctxn)
            sf_resources.add(conn)
            deco.append(f"required_resource_keys={{{conn!r}}}")
            deco.append("kinds={'snowflake'}")
            body = ctx_line + f"    {stmt}"
            pre = ""
            ret_none = True
        elif ty in ELIDED:                                     # an EmptyOperator that declares outlets/inlets: the asset is the dataset it publishes, nothing runs
            body = "    return None"
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
            execs = [fn for kind, fn in cb_code[tid] if kind == "on_execute_callback"]
            ctx_new = f"    ctx = af_context(context, {dag_id!r}, {tid!r}, {({**dag_params, **(t.get('params') or {})})!r}, {{}})\n"
            state_line = lambda st: f"    ctx['ti'].state = {st!r}\n"          # noqa: E731  (the callback reads ctx['ti'].state)
            adapters = ""
            kw = []
            if execs:                                           # on_execute_callback: called at the start of every attempt, before the task's own code
                adapters += f"def {ad}_execute(context):\n{ctx_new}{state_line('running')}" + "".join(f"    {fn}(ctx)\n" for fn in execs) + "\n\n"
                body = f"    {ad}_execute(context)\n" + body
            if fails or retries_cb:
                adapters += f"def {ad}_failure(context, exc):\n{ctx_new}    ctx['exception'] = exc\n"
                if fails:
                    adapters += f"    if context.retry_number >= {int(t['retries'] or 0)}:          # on_failure_callback: after the last attempt only\n        ctx['ti'].state = 'failed'\n" + "".join(f"        {fn}(ctx)\n" for fn in fails)
                if retries_cb:
                    adapters += f"    if context.retry_number < {int(t['retries'] or 0)}:           # on_retry_callback: before another attempt\n        ctx['ti'].state = 'up_for_retry'\n" + "".join(f"        {fn}(ctx)\n" for fn in retries_cb)
                adapters += "\n\n"
                kw.append(f"on_failure=[f'{{__name__}}:{ad}_failure']")
            if succ:
                adapters += f"def {ad}_success(context, result):\n{ctx_new}{state_line('success')}" + "".join(f"    {fn}(ctx)\n" for fn in succ) + "\n\n"
                kw.append(f"on_success=[f'{{__name__}}:{ad}_success']")
            pre = pre + adapters
            hook_deco = ("@on_hooks(" + ", ".join(kw) + ")\n") if kw else ""
        if chain_:                                              # the folded steps run first, in order, as plain Python inside this asset
            prelude = ""
            for f_ in chain_:
                ft = tasks[f_]
                if ft["type"] in BASH:
                    cmd_ = (ft.get("template_files") or {}).get("bash_command") or ft["template_fields"].get("bash_command")
                    envf = ft["template_fields"].get("env")
                    call_ = (f"run_shell(context, {expr(cmd_, params, None)}, env={expr(envf, params, None) if envf else 'None'}, cwd={expr(ft['template_fields'].get('cwd'), params, None)}, "
                             f"timeout=None{', xcom_json=True' if AF_MAJOR >= 3 else ''})")
                else:
                    fcode, fsup, fmeta = resolved[f_]
                    for sline in fsup:
                        support.setdefault(sline, len(support))
                    fpy = "_af_" + safe(f_)
                    pre = re.sub(r"^(async\s+)?def\s+\w+", lambda mm: f"def {fpy}", fcode, count=1) + "\n\n\n" + pre
                    fpos = [expr(a_, params, None) for a_ in (ft.get("op_args") or [])]
                    fkws = {k: expr(v_, params, None) for k, v_ in (ft.get("op_kwargs") or {}).items()}
                    call_ = f"{fpy}(" + ", ".join(fpos + [f"{k}={v_}" for k, v_ in fkws.items()]) + ")"
                prelude += f"    {params[f_]} = {call_}      # folded from Airflow task {f_!r}\n"
            body = prelude + body
        if tid in dop_tasks:
            body = "    af_depends_on_past(context)\n" + body
        body = sla_wrap(body, t, "/".join(keyparts[tid]))
        if tid in rt_outlets:
            inner_ = "\n".join(("    " + ln if ln.strip() else ln) for ln in body.split("\n"))
            body = f"    def _run():\n{inner_}\n    _result = _run()\n    af_runtime_partition_tag(context, {rt_outlets[tid]!r})\n    return _result"
        blocks.append(pre + "@dg.asset(\n    " + ",\n    ".join(deco) + ",\n)\n" + hook_deco + "def " + fname + sig + ":\n" + body + "\n")
        pool_name = t["pool"] or (f"{safe(dag_id)}__{safe(tid)}" if t.get("max_active_tis_per_dag") and f"{safe(dag_id)}__{safe(tid)}" in pool_limits else None)
        task_info[tid] = {"deps": sorted({"/".join(d) for d in deps} | {"/".join(keyparts[u]) for u in ordered if u in data}), "pool": pool_name, "retries": int(t["retries"] or 0)}
        if tid in consumed and not ret_none and not (pplan or ppart) and not t.get("_caveat"):
            nm = f"chk_{safe(tid)}_value"
            check_blocks.append(
                f"@dg.asset_check(asset=dg.AssetKey({keyparts[tid]!r}), name='returned_value_present', blocking=False,\n"
                f"                description=\"Structural check generated from the Airflow DAG (not a business check): the task's return value is consumed downstream, so it must not be None; an empty list, dict, string or DataFrame is a warning.\")\n"
                f"def {nm}(value) -> dg.AssetCheckResult:\n"
                "    if value is None:\n"
                "        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.ERROR, metadata={'kind': 'structural', 'why': 'the value is None but a downstream task consumes it'})\n"
                "    size = structural_size(value)\n"
                "    if size == 0:\n"
                "        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.WARN, metadata={'kind': 'structural', 'size': 0, 'why': 'the value is empty'})\n"
                "    return dg.AssetCheckResult(passed=True, metadata={'kind': 'structural', 'type': type(value).__name__, **({'size': size} if size is not None else {})})\n")
            check_list.append({"name": "returned_value_present", "asset": "/".join(keyparts[tid]), "kind": "returned_value"})
        if ty in S3_OPS and not t.get("is_mapped") and not (pplan or ppart):
            dest = {"S3CopyObjectOperator": ("dest_bucket_name", "dest_bucket_key"), "LocalFilesystemToS3Operator": ("dest_bucket", "dest_key"),
                    "S3CreateObjectOperator": ("s3_bucket", "s3_key")}.get(ty)
            if dest:
                tf_ = t["template_fields"]
                b_, k_ = tf_.get(dest[0]), tf_.get(dest[1])
                plain = lambda x: isinstance(x, str) and x and "{{" not in x and "{%" not in x          # noqa: E731
                if plain(k_) and (plain(b_) or k_.startswith("s3://")):
                    if k_.startswith("s3://"):
                        b_, k_ = k_[5:].split("/", 1) if "/" in k_[5:] else (k_[5:], "")
                    conn_ = tf_.get("aws_conn_id") or "aws_default"
                    if k_:
                        check_blocks.append(
                            f"@dg.asset_check(asset=dg.AssetKey({keyparts[tid]!r}), name='s3_object_exists', blocking=False, required_resource_keys={{{conn_!r}}},\n"
                            f"                description='Structural check generated from the Airflow DAG (not a business check): the object the task writes exists.')\n"
                            f"def chk_{safe(tid)}_s3(context) -> dg.AssetCheckResult:\n"
                            f"    client = getattr(context.resources, {conn_!r})\n"
                            "    try:\n"
                            f"        client.head_object(Bucket={b_!r}, Key={k_!r})\n"
                            "    except Exception as e:  # noqa: BLE001\n"
                            f"        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.ERROR, metadata={{'kind': 'structural', 'bucket': {b_!r}, 'key': {k_!r}, 'error': type(e).__name__}})\n"
                            f"    return dg.AssetCheckResult(passed=True, metadata={{'kind': 'structural', 'bucket': {b_!r}, 'key': {k_!r}}})\n")
                        check_list.append({"name": "s3_object_exists", "asset": "/".join(keyparts[tid]), "kind": "s3_object_exists"})

    blocks.extend(check_blocks)
    keys = [keyparts[t["task_id"]] for t in real] + ([[safe(dag_id), "run_check"]] if graph_outs else []) + cosmos_keys
    imports = (["import itertools"] if gplan else []) + ["import dagster as dg"] + (["from dagster_community_components import on_hooks"] if any(k != "on_execute_callback" for v_ in cb_code.values() for k, _ in v_) else []) \
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
    if shims_used & {"Connection", "BaseHook"}:
        notes.append("callables use Connection / BaseHook.get_connection through an environment shim: it reads AIRFLOW_CONN_<CONN_ID> (a URI or JSON, as Airflow's environment backend does) and "
                     "raises AirflowNotFoundException naming the variable when it is not set; the Dagster-native form is a ConfigurableResource fed from EnvVar")
    if "ObjectStoragePath" in shims_used or any("object_storage_path" in json.dumps(t, default=str) for t in tasks.values()):
        notes.append("ObjectStoragePath became a pathlib.Path (file://) or a universal_pathlib UPath (s3://, gs://, ...): install universal-pathlib and the fsspec backend; a connection id in the URI "
                     "reads AIRFLOW_CONN_<ID> and fails loudly if it is not set. A file:// path is the absolute path seen when the IR was extracted: point it where the data lives")
    if "S3Hook" in shims_used:
        notes.append("callables use S3Hook through a compatibility shim (default AWS credentials; the connection id is not applied)")
    if notes:
        mod[1:1] = ["", *[f"NOTE: {n}" for n in notes]]
    out = "\n".join(mod) + "\n\n" + "\n\n".join(blocks) + "\n"
    mapped_keys = [keyparts[x] for x in (pplan["args"] if pplan else [])]
    job_keys = [k for k in keys if k not in mapped_keys] if pplan and pplan["kind"] == "xcom" else keys
    pd_arg = (f", partitions_def={safe(dag_id)}_items" if pplan and pplan["kind"] == "literal" else (f", partitions_def={part_var}" if ppart else ""))
    cosmos_retries = max((c["retries"] for c in cosmos_clusters), default=0)       # the dbt component has no retry-policy field: Cosmos task retries become run retries
    job_tags = {**({"airflow/dag_id": dag_id} if max_runs else {}), **({"dagster/max_runtime": str(run_timeout)} if run_timeout else {}),
                **({"dagster/max_retries": str(cosmos_retries)} if cosmos_retries else {})}
    out += (f"\n{safe(dag_id)}_job = dg.define_asset_job({safe(dag_id)!r}, selection=[dg.AssetKey(k) for k in {job_keys!r}]{pd_arg}"
            + (f", tags={job_tags!r}" if job_tags else "")
            + (f", executor_def=dg.multiprocess_executor.configured({{'max_concurrent': {max_tasks}}})" if max_tasks else "") + ")\n")
    for kind_ in dict.fromkeys(k_ for k_, _ in dag_cb):             # DAG-level callbacks: a run-status sensor on this DAG's job (needs the sensor daemon), one per kind, calling every function of that kind
        status_ = "SUCCESS" if kind_ == "on_success_callback" else "FAILURE"
        nm_ = f"{safe(dag_id)}_{kind_}"
        fns_ = [f_ for k_, f_ in dag_cb if k_ == kind_]
        out += (f"\n\n@dg.run_status_sensor(name={nm_!r}, run_status=dg.DagsterRunStatus.{status_}, monitored_jobs=[{safe(dag_id)}_job], default_status=dg.DefaultSensorStatus.RUNNING)\n"
                f"def {nm_}(context):\n    af_dag_callbacks(context, {dag_id!r}, {('success' if status_ == 'SUCCESS' else 'failed')!r}, [{', '.join(fns_)}])\n")
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
    for k in own_keys | {"/".join(x) for x in cosmos_keys}:
        produced[k] = dag_id
    ext_ops = SENSORS | TRIGGER | ISOLATED | ISOLATED_BRANCH | PROV | S3_OPS | SNOWFLAKE_OPS | COSMOS_STEP
    mat_skip: str | None = None
    if cosmos_clusters or cosmos_steps:
        mat_skip = "the DAG runs dbt (Cosmos) against a warehouse"
    elif resources or sf_resources or prov_conns or any(t["type"] in ext_ops for t in real):
        mat_skip = "the DAG calls external systems (cloud storage, a warehouse, an HTTP or SQL service, a sensor, a container or a triggered DAG): only the load and shape tests apply; run it side by side with Airflow instead"
    elif any(c.get("kind") == "stub" for c in caveats):
        mat_skip = "the DAG contains a loud stub (AIRFLOW-CAVEAT), which fails when it runs by design"
    elif ppart or pplan:
        mat_skip = "the assets are partitioned: a run needs a partition key chosen by a person"
    elif external:
        mat_skip = "the DAG reads assets that no converted DAG produces (sources.py)"
    elif any(is_custom(t) for t in real):
        mat_skip = "the DAG runs a user-defined operator class, which may reach external systems"
    sched_info: Any = {"cron": tt.get("cron"), "timezone": tz_name(tt["timezone"]) if tt.get("cron") else None} if tt["kind"] in ("cron_trigger", "cron_data_interval") and not (ppart or pplan) else None
    if ppart or pplan:
        sched_info = "unchecked"
    test_info = {"dag_id": dag_id, "job": safe(dag_id), "schedule": sched_info, "materialize_skip": mat_skip,
                 "needs_shell": any(t["type"] in BASH | BASH_DEC for t in real),
                 "assets": {"/".join(keyparts[t["task_id"]]): task_info.get(t["task_id"], {}) for t in real},
                 "checks": check_list, "loadable_alone": not cosmos_keys}
    return out, {"assets": len(real), "elided": len(tasks) - len(real), "schedule": tt.get("cron"),
                 "timezone": tz_name(tt["timezone"]) if tt.get("cron") else None, "triggers": triggers,
                 "resources": sorted(resources), "snowflake_resources": sorted(sf_resources), "provider_connections": dict(sorted(prov_conns.items())), "shims": sorted(shims_used), "notes": notes, "caveats": caveats, "catalog_keys": [list(v) for v in rt_outlets.values()], "local_modules": sorted(local_modules(third_party(support), {t["callable"].get("file") for t in real if t.get("callable")})),
                 "requirements": sorted((third_party(support) - local_modules(third_party(support), {t["callable"].get("file") for t in real if t.get("callable")})) | ({"croniter"} if uses_ctx and tt["kind"] == "cron_data_interval" else set()) | ({"babel"} if any("ds_format_locale" in json.dumps(task_templates(t), default=str) for t in tasks.values()) else set()) | ({"universal-pathlib"} if "ObjectStoragePath" in shims_used or any("object_storage_path" in json.dumps(t, default=str) for t in tasks.values()) else set()) | prov_reqs | cosmos_reqs | ({"dagster-community-components"} if any(k != "on_execute_callback" for v_ in cb_code.values() for k, _ in v_) or (ppart and ppart.get("via")) else set())), "external": sorted("/".join(e) for e in external), "keys": keys,
                 "cosmos": cosmos_out, "cosmos_steps": cosmos_steps, "cosmos_files": cosmos_files,
                 "cosmos_tasks": {m: ["/".join(k) for k in c["keys"]] for c in cosmos_clusters for m in c["members"]},
                 "cosmos_sinks": {m: ["/".join(k) for k in c["sink_keys"]] for c in cosmos_clusters for m in c["members"]},
                 "uses_context": uses_ctx, "run_timeout_s": run_timeout, "partitions": (({"role": ppart["role"], **ppart["info"]}) if ppart else None), "mapping": ({"kind": pplan["kind"], "mapped": sorted(pplan["args"]), "producer": pplan.get("producer")} if pplan else
                             ({"kind": "graph", "mapped": sorted(gplan["mapped"])} if gplan else None)), "max_active_runs": max_runs, "max_active_tasks": max_tasks, "pools": pool_limits,
                 "task_keys": {**{t["task_id"]: "/".join(keyparts[t["task_id"]]) for t in real}, **{f: "/".join(keyparts[c]) for f, c in fold_into.items()}, **loop_members_report(dag, keyparts, fold_into)},
                 "loop_members": loop_members_report(dag, keyparts, fold_into),
                 "folded": dict(sorted(fold_into.items())), "glue": sorted(elided), "granularity": ({"mode": "product", "decisions": dict(sorted(gran_decisions.items()))} if GRANULARITY == "product" else None), "test_info": test_info, "asset_checks": check_list}


RUNTIME = '''"""Helpers used by the converted DAGs. Each mirrors what the Airflow operator or hook does."""
from __future__ import annotations

import os
import subprocess
from typing import Any, Optional

import dagster as dg


def run_shell(context: dg.AssetExecutionContext, command: str, env: Optional[dict[str, Any]] = None,
              cwd: Optional[str] = None, timeout: Optional[int] = None, xcom_json: bool = False, skip_on_exit_code: Any = (99,)) -> Any:
    """Run a BashOperator command with `bash -c`, as Airflow does. Returns the last line written to stdout, which is what
    Airflow pushes as the task's XCom (None when the command wrote nothing). An exit code in `skip_on_exit_code` (Airflow's default is 99)
    skips the task instead of failing it."""
    full_env = {**os.environ, **{k: str(v) for k, v in (env or {}).items()}}
    context.log.info(f"$ {command}")
    try:
        proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env=full_env, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as e:           # Airflow raises AirflowTaskTimeout: the task fails
        raise dg.Failure(description=f"execution_timeout of {timeout} s exceeded", metadata={"command": command}) from e
    if proc.stdout:
        context.log.info(proc.stdout)
    skip_codes = [skip_on_exit_code] if isinstance(skip_on_exit_code, int) else list(skip_on_exit_code or [])
    if proc.returncode != 0 and proc.returncode in skip_codes:
        raise AirflowSkipException(f"command exited {proc.returncode}, which skips the task")
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
import re
import types

_MISSING = object()
_AF_CTX: contextvars.ContextVar = contextvars.ContextVar("airflow_context", default=None)


class _TaskInstance:
    """The part of Airflow's TaskInstance converted callables and templates use. Anything else raises."""

    def __init__(self, dag_id, task_id, run_id, try_number, upstream, context=None, keys=None):
        self.dag_id, self.task_id, self.run_id, self.try_number, self._up = dag_id, task_id, run_id, try_number, upstream
        self._dg_run_id = run_id                             # Dagster's run id (event lookups); `run_id` is replaced by the Airflow-style id in af_context
        self._ctx, self._keys = context, keys or {}
        self._rv = _MISSING
        self.state = "running"                               # a callback adapter sets it ('running', 'success', 'failed', 'up_for_retry') before calling the callback
        self.map_index = -1                                  # Airflow's value for a task that is not mapped; a mapped op sets its index (see af_context)

    def xcom_pull(self, task_ids=None, dag_id=None, key="return_value", include_prior_dates=False, map_indexes=None, default=None):
        if task_ids is None:
            raise NotImplementedError("xcom_pull without task_ids is not mirrored: name the task")
        if map_indexes is not None and key == "return_value" and dag_id in (None, self.dag_id) and not include_prior_dates:
            ids = [task_ids] if isinstance(task_ids, str) else list(task_ids)
            idxs = [map_indexes] if isinstance(map_indexes, int) else list(map_indexes)
            out = []
            for i in ids:                                    # Airflow 3: one value per (task id, map index), task-major; a mapped task's collected values are the list over its instances
                seq = self._up.get(i)
                for m in idxs:
                    v = seq[m] if isinstance(seq, (list, tuple)) and -len(seq) <= m < len(seq) else None
                    out.append(default if v is None else v)
            return out[0] if isinstance(task_ids, str) and isinstance(map_indexes, int) else _pulled(out)
        if map_indexes is not None:
            raise NotImplementedError("xcom_pull with map_indexes of another DAG, a prior run or a pushed key is not mirrored")
        if dag_id not in (None, self.dag_id) or include_prior_dates:
            # another DAG's value, or a PRIOR run's: in Dagster that is the latest materialization of the asset that task became. A value pushed under a key is metadata on it;
            # the DAG's own return value cannot be read this way (load that asset as an input instead), so it stays loud.
            ids = [task_ids] if isinstance(task_ids, str) else list(task_ids)
            if key == "return_value":
                raise NotImplementedError("xcom_pull of another DAG's (or a prior run's) return value: declare that asset as an input (deps / AssetIn) or use context.instance to load it; only values pushed under a key (asset metadata) are mirrored")
            vals = [self._pull_latest(dag_id or self.dag_id, i, key, default) for i in ids]
            return vals[0] if isinstance(task_ids, str) else vals
        ids = [task_ids] if isinstance(task_ids, str) else list(task_ids)
        if key != "return_value":                            # a value another task pushed under a key: read from its materialization metadata
            vals = [self._pull_key(i, key, default) for i in ids]
            return vals[0] if isinstance(task_ids, str) else vals
        vals = []
        for i in ids:
            if i not in self._up:          # Airflow returns None for a task id it does not know (often a TaskGroup prefix left out)
                self._warn(i)
            vals.append(self._up.get(i, default))
        return vals[0] if isinstance(task_ids, str) else _pulled(vals)

    def _pull_latest(self, dag_id, task_id, key, default):
        """the metadata `key` on the latest materialization (any run) of the asset (dag_id, task_id) became (default key path: [dag_id, task_id]); None when it was never materialized, as Airflow returns None."""
        asset_key = (self._keys.get(task_id) if dag_id == self.dag_id else None) or [dag_id, task_id]
        rec = self._ctx.instance.get_latest_materialization_event(dg.AssetKey(asset_key)) if self._ctx is not None else None
        mat = rec.asset_materialization if rec is not None else None
        if mat is not None and key in mat.metadata:
            ordered = mat.metadata.get(key + _ORDER_SUFFIX)
            return json.loads(ordered.value) if ordered is not None else mat.metadata[key].value
        return default

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
            if r.event_log_entry.run_id == self._dg_run_id and key in r.asset_materialization.metadata:
                ordered = r.asset_materialization.metadata.get(key + _ORDER_SUFFIX)
                return json.loads(ordered.value) if ordered is not None else r.asset_materialization.metadata[key].value
        return default

    def xcom_push(self, key, value=None, **_):
        """A value pushed under a key becomes metadata on this asset's materialization (a return value is the asset's value)."""
        if key == "return_value":                            # the task's value: kept, and used when the callable itself returns nothing (or, for a sensor, only the done flag)
            self._rv = value
            return
        try:
            json.dumps(value)
        except TypeError as e:
            raise dg.Failure(description=f"xcom_push of {key!r}: the value is not JSON-serializable ({e})") from e
        meta = dg.MetadataValue.json(value) if isinstance(value, (dict, list)) else value
        out = {key: meta}
        if _has_dict(value):                                 # Dagster stores a JSON metadata value with its keys sorted: the pushed order is kept in a text copy that a pull reads back
            out[key + _ORDER_SUFFIX] = dg.MetadataValue.text(json.dumps(value))
        self._ctx.add_output_metadata(out)


NEVER_EXPIRE = _dt.timedelta.max      # Airflow 3.4's retention sentinel for the task / asset state stores: the shim keeps state until it is deleted whatever the retention


_ORDER_SUFFIX = "::order"


def _has_dict(value):
    return isinstance(value, dict) or (isinstance(value, (list, tuple)) and any(_has_dict(x) for x in value))


class _TaskStateStore:
    """Airflow 3.4's `task_state_store` (key/value state of ONE task instance, kept across its retries): a JSON file per (dag, task, run, map_index) under AIRFLOW_TASK_STATE_DIR
    (default: <tempdir>/af_task_state). Values must be JSON. A plain-Python shim: Dagster has no per-step key/value store (asset metadata is the nearest durable record)."""

    def __init__(self, dag_id, task_id, run_id, map_index):
        import tempfile
        d = os.environ.get("AIRFLOW_TASK_STATE_DIR") or os.path.join(tempfile.gettempdir(), "af_task_state")
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{dag_id}__{task_id}__{run_id}__{map_index}")
        self._path = os.path.join(d, name + ".json")

    def _load(self):
        try:
            with open(self._path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save(self, data):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(data, f)

    def get(self, key, default=None):
        return self._load().get(key, default)

    def set(self, key, value, retention=None):
        """`retention` (a timedelta or NEVER_EXPIRE) is accepted and not applied: the file stays until it is deleted (the state of one run id is not read by any other run)."""
        json.dumps(value)                                  # Airflow stores JSON: a value that is not JSON fails there too
        data = self._load()
        data[key] = value
        self._save(data)

    def delete(self, key):
        data = self._load()
        data.pop(key, None)
        self._save(data)

    def clear(self):
        self._save({})

    async def aget(self, key, default=None):               # the async forms of an `async def` task (Airflow 3.4): the same file, no real I/O wait
        return self.get(key, default)

    async def aset(self, key, value, retention=None):
        self.set(key, value, retention)

    async def adelete(self, key):
        self.delete(key)

    async def aclear(self):
        self.clear()


class BaseNotifier:
    """Airflow's `BaseNotifier` (a callable object whose `notify(context)` is called with the callback context), as a plain class: a notifier subclass copied from the DAG file runs unchanged.
    Airflow also renders the notifier's `template_fields` against the context first; that is not mirrored."""
    template_fields = ()

    def __init__(self, *args, **kwargs):
        pass

    def notify(self, context):
        raise NotImplementedError

    def __call__(self, *args):
        self.notify(args[-1])


def af_dag_callbacks(context, dag_id, state, fns):
    """Run a DAG's `on_success_callback` / `on_failure_callback` functions from a Dagster run-status sensor, with the context dict Airflow hands them (the parts a Dagster run can supply:
    dag, dag_id, run_id, dag_run (run_id, dag_id, state, conf, logical_date), logical_date, execution_date, ds, ts, params, conf, reason, exception = the run failure message). Not provided:
    task_instance / ti (a DAG callback has none in Airflow 3), the DAG object's own attributes."""
    run = context.dagster_run
    ld = None
    try:
        tag = (run.tags or {}).get("airflow/logical_date")
        ld = _dt.datetime.fromisoformat(tag) if tag else _dt.datetime.fromtimestamp(context.instance.get_run_record_by_id(run.run_id).create_timestamp.timestamp(), tz=_dt.timezone.utc)
    except Exception:                                    # noqa: BLE001
        ld = _dt.datetime.now(_dt.timezone.utc)
    dag_run = types.SimpleNamespace(run_id=run.run_id, dag_id=dag_id, state=state, conf={}, logical_date=ld, execution_date=ld)
    ctx = {"dag": types.SimpleNamespace(dag_id=dag_id), "dag_id": dag_id, "run_id": run.run_id, "dag_run": dag_run, "logical_date": ld, "execution_date": ld,
           "ds": ld.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d"), "ts": ld.isoformat(), "params": {}, "conf": {},
           "reason": "success" if state == "success" else "task_failure", "exception": None if state == "success" else getattr(context.dagster_event, "message", None)}
    for fn in fns:
        fn(ctx)


class _AssetStateStore:
    """Airflow 3.4's `asset_state_store[asset]` (key/value state kept ON THE ASSET across runs, e.g. a watermark): a JSON file per asset (its URI, else its name) under AIRFLOW_ASSET_STATE_DIR
    (default: <tempdir>/af_asset_state). Same `get`/`set`/`delete`/`clear` as the task store. A plain-Python shim: Dagster has no per-asset key/value store (the nearest durable records are the
    asset's materialization metadata, or a resource you back with a database); point AIRFLOW_ASSET_STATE_DIR at durable storage to keep the state between deployments."""

    def __getitem__(self, asset):
        import tempfile
        d = os.environ.get("AIRFLOW_ASSET_STATE_DIR") or os.path.join(tempfile.gettempdir(), "af_asset_state")
        ref = asset if isinstance(asset, str) else (getattr(asset, "uri", None) or getattr(asset, "name", None) or str(asset))
        store = _TaskStateStore.__new__(_TaskStateStore)
        store._path = os.path.join(d, re.sub(r"[^A-Za-z0-9_.-]", "_", ref) + ".json")
        return store


AF_ISO_DT = False          # set when the runtime is written: Airflow before 2.8 prints datetimes in templates with a T (2026-07-02T05:00:00+00:00)
AF_RT_MAJOR = 2          # the Airflow major version of the converted DAGs (written by convert_ir.py): Airflow 2's xcom_pull of several tasks / indexes returns a lazy sequence, Airflow 3's a list


class LazySelectSequence(list):
    """What Airflow 2's `ti.xcom_pull` returns for several task ids or map indexes: a read-only lazy sequence that prints as `LazySelectSequence([n items])`."""

    def __repr__(self):
        return f"LazySelectSequence([{len(self)} item{'' if len(self) == 1 else 's'}])"

    __str__ = __repr__


def _pulled(vals):
    return LazySelectSequence(vals) if AF_RT_MAJOR < 3 else vals


class _IsoDatetime(_dt.datetime):
    def __str__(self):
        return self.isoformat()


def _iso(x):
    return _IsoDatetime(x.year, x.month, x.day, x.hour, x.minute, x.second, x.microsecond, x.tzinfo) if AF_ISO_DT and isinstance(x, _dt.datetime) else x


class _TaskShim:
    """The `task` of the Airflow context: its ids, and `render_template(content, context)`, which renders a string (or each string in a list, tuple, set or dict) with the runtime's Jinja environment."""

    def __init__(self, task_id, dag_id, params=None):
        self.task_id, self.dag_id = task_id, dag_id
        self.params = dict(params or {})

    def render_template(self, content, context=None, jinja_env=None, seen_oids=None):
        ctx = context if context is not None else (_AF_CTX.get() or {})
        if isinstance(content, str):
            return render(ctx, content) if ("{{" in content or "{%" in content or "{#" in content) else content
        if isinstance(content, (list, tuple, set)):
            return type(content)(self.render_template(x, ctx) for x in content)
        if isinstance(content, dict):
            return {k: self.render_template(v, ctx) for k, v in content.items()}
        return content


def af_context(context, dag_id, task_id, params, upstream, keys=None):
    """Rebuild the Airflow task context from the Dagster run. The logical date comes from run tags that airflow_schedule sets;
    a run without them (a manual run) uses the current time, as Airflow does for a manual trigger. ds is the UTC date of the
    logical date."""
    run = getattr(context, "run", None)
    tags = dict(getattr(run, "tags", None) or {})
    utc = _dt.timezone.utc
    window = None                                        # a time-partitioned run: the verified templating pattern takes ds and the interval from the partition's window
    try:
        if context.has_partition_key:
            window = context.partition_time_window
    except Exception:                                    # noqa: BLE001  not a time partition (static or dynamic keys), or an op context without one
        window = None
    if tags.get("airflow/logical_date"):                 # a scheduled run carries Airflow's own logical date
        ld = _dt.datetime.fromisoformat(tags["airflow/logical_date"])
    elif window is not None:
        ld = window.start
    else:
        ld = None                                        # a manual run: Airflow's logical date is the moment the run started, the same for every task of the run
        try:
            ld = _dt.datetime.fromtimestamp(context.instance.get_run_record_by_id(run.run_id).create_timestamp.timestamp(), tz=utc)
        except Exception:                                # noqa: BLE001  no instance or run record (a bare context)
            ld = _dt.datetime.now(utc)
    if tags.get("airflow/data_interval_end"):
        de = _dt.datetime.fromisoformat(tags["airflow/data_interval_end"])
    else:
        de = window.end if window is not None else ld
    ld, de = ld.astimezone(utc), de.astimezone(utc)
    ds = ld.strftime("%Y-%m-%d")
    nxt = _dt.datetime.fromisoformat(tags["airflow/next_logical_date"]).astimezone(utc) if tags.get("airflow/next_logical_date") else (de if de != ld else ld)
    prv = _dt.datetime.fromisoformat(tags["airflow/prev_logical_date"]).astimezone(utc) if tags.get("airflow/prev_logical_date") else None
    one = _dt.timedelta(days=1)
    conf = json.loads(tags.get("airflow/conf", "{}"))
    run_id = run.run_id if run is not None else context.run_id        # context.run_id is deprecated in current Dagster
    ti = _TaskInstance(dag_id, task_id, run_id, getattr(context, "retry_number", 0) + 1, upstream, context, keys)
    af_run_id = tags.get("airflow/run_id") or f"{'scheduled' if tags.get('airflow/logical_date') else 'manual'}__{ld.isoformat()}"      # Airflow's `<run type>__<logical date>`; Dagster's own id stays the key for event lookups
    ti.run_id = af_run_id
    try:                                                 # a dynamically mapped op: the mapping key is `i<index>` (see the fan-out op), which is Airflow's map_index
        mk = context.get_mapping_key() if hasattr(context, "get_mapping_key") else None
        if mk and re.fullmatch(r"i\d+", mk):
            ti.map_index = int(mk[1:])
    except Exception:                                    # noqa: BLE001  an asset context has no mapping key
        pass
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
            "run_id": af_run_id, "params": {k: v for k, v in dict(params or {}).items() if k != "__macros__"}, "conf": conf, **dict((params or {}).get("__macros__") or {}),
            "next_execution_date": _iso(nxt), "next_ds": nxt.strftime("%Y-%m-%d"), "next_ds_nodash": nxt.strftime("%Y%m%d"),
            "prev_execution_date": _iso(prv), "prev_ds": prv.strftime("%Y-%m-%d") if prv else None, "prev_ds_nodash": prv.strftime("%Y%m%d") if prv else None,
            "tomorrow_ds": (ld + one).strftime("%Y-%m-%d"), "tomorrow_ds_nodash": (ld + one).strftime("%Y%m%d"),
            "yesterday_ds": (ld - one).strftime("%Y-%m-%d"), "yesterday_ds_nodash": (ld - one).strftime("%Y%m%d"),
            "ts_nodash_with_tz": ld.strftime("%Y%m%dT%H%M%S%z"), "task_instance_key_str": f"{dag_id}__{task_id}__{ld.strftime('%Y%m%d')}", "prev_data_interval_start_success": None, "prev_data_interval_end_success": None,
            "dag": types.SimpleNamespace(dag_id=dag_id), "task": _TaskShim(task_id, dag_id, params), "test_mode": False, "ti": ti, "task_instance": ti,
            "dag_run": types.SimpleNamespace(state="running", run_id=af_run_id, conf=conf, logical_date=ld, execution_date=ld, dag_id=dag_id, partition_key=pkey, partition_date=pdate),
            "partition_key": pkey, "outlet_events": _OutletEvents(context), "inlet_events": _InletEvents(context),
            "task_state_store": _TaskStateStore(dag_id, task_id, run_id, ti.map_index), "asset_state_store": _AssetStateStore(),
            "var": _VarNamespace(), "conn": _ConnNamespace()}


def call_with_context(fn, args, kwargs, ctx):
    """PythonOperator's rule: context is merged under op_kwargs; a **kwargs callable gets everything, otherwise only the
    parameters it names (and that op_args did not already fill)."""
    sig = inspect.signature(fn)
    merged = {**ctx, **kwargs}
    if "context" in sig.parameters and "context" not in kwargs:          # a callable that takes the whole context dict (`def f(context)`): the same dict `get_current_context()` returns
        merged["context"] = ctx
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
        rv = getattr(ctx.get("ti"), "_rv", _MISSING)
        if rv is not _MISSING:                              # ti.xcom_push(key="return_value", ...) inside the callable
            if isinstance(result, bool):
                result = PokeReturnValue(result, rv)
            elif result is None:
                result = rv
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


def _ds_format_locale(ds, input_format, output_format, locale=None):
    """Airflow's macros.ds_format_locale: needs `babel` (pip install babel), as Airflow does."""
    from babel.dates import format_date
    return format_date(_dt.datetime.strptime(str(ds), input_format), format=output_format, locale=locale)


def _lazy_macro_modules():
    out = {}
    for name in ("yaml", "dateutil"):                  # macros.yaml / macros.dateutil: present when the package is installed (Airflow ships both)
        try:
            out[name] = __import__(name)
        except ImportError:
            pass
    if "dateutil" in out:
        import dateutil.parser, dateutil.relativedelta, dateutil.tz  # noqa: E401,F401
    return out


class _VarValue:
    def __getattr__(self, key):
        v = os.environ.get("AIRFLOW_VAR_" + key.upper())
        if v is None:
            raise KeyError(f"Variable {key} does not exist")
        return v


class _VarJson:
    def __getattr__(self, key):
        return json.loads(getattr(_VarValue(), key))


class _VarNamespace:
    """`{{ var.value.x }}` and `{{ var.json.x }}`: Airflow Variables from AIRFLOW_VAR_<KEY>, as Variable.get reads them."""
    value = _VarValue()
    json = _VarJson()


def render(ctx, text):
    """Render an Airflow-style Jinja template: sandboxed, strict about undefined names, with the ds/ts filters and ds macros."""
    import jinja2
    from jinja2.sandbox import SandboxedEnvironment
    env = SandboxedEnvironment(undefined=jinja2.StrictUndefined, extensions=["jinja2.ext.do"], cache_size=0)
    env.filters.update({"ds": lambda v: v.strftime("%Y-%m-%d"), "ds_nodash": lambda v: v.strftime("%Y%m%d"),
                        "ts": lambda v: v.isoformat(), "ts_nodash": lambda v: v.strftime("%Y%m%dT%H%M%S")})
    import random as _random
    import time as _time
    import uuid as _uuid
    macros = types.SimpleNamespace(ds_add=_ds_add, ds_format=_ds_format, ds_format_locale=_ds_format_locale, datetime=_dt.datetime, timedelta=_dt.timedelta,
                                   json=json, time=_time, uuid=_uuid, random=_random, **_lazy_macro_modules())
    return env.from_string(text).render(**{"var": _VarNamespace(), **ctx}, macros=macros)


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


class _Branch:
    """What a branch task chose: the task ids (or task group ids) to follow. The value IS what the callable returned (a str or a list), so the asset's value, metadata and the IO manager see exactly Airflow's XCom."""

    @property
    def selected(self):
        return [str(self)] if isinstance(self, str) else list(self)

    def selects(self, task_id):
        return any(task_id == s or task_id.startswith(s + ".") for s in self.selected)


class _BranchStr(_Branch, str):
    pass


class _BranchList(_Branch, list):
    pass


def BranchResult(selected):
    """Wrap a branch callable's return value (a task id, a list of ids, or None for 'follow nothing') in a value that also carries the choice."""
    return _BranchStr(selected) if isinstance(selected, str) else _BranchList([] if selected is None else selected)


import enum


class WeekDay(enum.IntEnum):
    """Airflow's WeekDay: Monday is 1 ... Sunday is 7."""
    MONDAY = 1
    TUESDAY = 2
    WEDNESDAY = 3
    THURSDAY = 4
    FRIDAY = 5
    SATURDAY = 6
    SUNDAY = 7

    @classmethod
    def get_weekday_number(cls, date_time):
        return date_time.isoweekday()


class AirflowException(Exception):
    """Stands in for Airflow's exception so converted callables that raise it need no Airflow import."""


class AirflowSkipException(AirflowException):
    """Raised by a task to skip itself: in the graph form the step becomes a skipped step."""


class AirflowFailException(AirflowException):
    """Raised by a task to fail without retrying."""


class AirflowNotFoundException(AirflowException):
    """Raised for a connection (or variable) that is not defined, as Airflow does."""


def _conn_env_name(conn_id):
    return "AIRFLOW_CONN_" + str(conn_id).upper()


class Connection:
    """Airflow connections from the environment (AIRFLOW_CONN_<CONN_ID>, a URI or Airflow's JSON form), which is what Airflow's own environment backend reads.
    Loud when the variable is missing: nothing is guessed. Dagster's native form is a ConfigurableResource or EnvVar; this keeps `Connection.get(...)` and
    `BaseHook.get_connection(...)` calls working unchanged while you move the secrets there."""

    def __init__(self, conn_id=None, conn_type=None, host=None, login=None, password=None, schema=None, port=None, extra=None, uri=None, description=None):
        self.conn_id, self.conn_type, self.host, self.login, self.password, self.schema, self.port = conn_id, conn_type, host, login, password, schema, port
        self.description = description
        self.extra = json.dumps(extra) if isinstance(extra, dict) else extra
        if uri:
            self._from_uri(uri)

    def _from_uri(self, uri):
        from urllib.parse import unquote, urlsplit, parse_qsl
        parts = urlsplit(uri if "://" in uri else "//" + uri)
        self.conn_type = (parts.scheme or "").replace("-", "_") or self.conn_type
        if self.conn_type == "postgresql":
            self.conn_type = "postgres"
        self.host = unquote(parts.hostname) if parts.hostname else None
        self.login = unquote(parts.username) if parts.username else None
        self.password = unquote(parts.password) if parts.password else None
        self.port = parts.port
        self.schema = unquote(parts.path[1:]) if parts.path else None
        if parts.query:
            q = dict(parse_qsl(parts.query, keep_blank_values=True))
            self.extra = q["__extra__"] if list(q) == ["__extra__"] else json.dumps(q)

    @classmethod
    def from_json(cls, value, conn_id=None):
        d = json.loads(value)
        d.pop("conn_id", None)
        if "conn_type" not in d:
            raise AirflowException(f"The connection JSON for {conn_id} has no conn_type")
        return cls(conn_id=conn_id, **d)

    @classmethod
    def get(cls, conn_id):
        raw = os.environ.get(_conn_env_name(conn_id))
        if raw is None:
            raise AirflowNotFoundException(
                f"The conn_id `{conn_id}` isn't defined: set the environment variable {_conn_env_name(conn_id)} (a URI or JSON, as Airflow's environment backend reads it), "
                "or move the call to a Dagster ConfigurableResource")
        return cls.from_json(raw, conn_id) if raw.lstrip().startswith("{") else cls(conn_id=conn_id, uri=raw)

    get_connection_from_secrets = get

    @property
    def extra_dejson(self):
        return json.loads(self.extra) if self.extra else {}

    def get_uri(self):
        from urllib.parse import quote
        auth = (quote(self.login or "", safe="") + (":" + quote(self.password or "", safe="") if self.password else "") + "@") if self.login else ""
        host = (self.host or "") + (f":{self.port}" if self.port else "")
        return f"{(self.conn_type or '').replace('_', '-')}://{auth}{host}" + (f"/{self.schema}" if self.schema else "") + (f"?__extra__={quote(self.extra, safe='')}" if self.extra else "")

    def __repr__(self):
        return f"Connection(conn_id={self.conn_id!r}, conn_type={self.conn_type!r}, host={self.host!r})"        # never prints the password


class _ConnNamespace:
    """`context["conn"].my_conn_id` / `conn.get("my_conn_id")`: the Connection from the environment (loud when its variable is not set)."""

    def __getattr__(self, conn_id):
        if conn_id.startswith("__"):
            raise AttributeError(conn_id)
        return Connection.get(conn_id)

    def get(self, conn_id, default_conn=None):
        try:
            return Connection.get(conn_id)
        except AirflowNotFoundException:
            return default_conn


class BaseHook:
    """`BaseHook.get_connection(conn_id)` from the environment (see Connection). A provider hook subclass is not mirrored: its methods raise instead of guessing."""

    @classmethod
    def get_connection(cls, conn_id):
        return Connection.get(conn_id)


class _FilePath(type(__import__("pathlib").Path())):
    """A local ObjectStoragePath. Airflow 2's (universal_pathlib 0.2) `rename` / `replace` read a RELATIVE target as a sibling (relative to the parent directory); Airflow 3's read it against the working directory, like pathlib."""

    def rename(self, target):
        if AF_RT_MAJOR < 3 and isinstance(target, (str, os.PathLike)) and not os.path.isabs(target):
            target = self.parent / os.path.normpath(target)
        return super().rename(target)

    def replace(self, target):
        if AF_RT_MAJOR < 3 and isinstance(target, (str, os.PathLike)) and not os.path.isabs(target):
            target = self.parent / os.path.normpath(target)
        return super().replace(target)


def ObjectStoragePath(path, conn_id=None, **storage_options):
    """Airflow's ObjectStoragePath as plain Python: pathlib.Path for file:// and bare paths; universal_pathlib's UPath for other schemes (pip install universal-pathlib plus
    the fsspec backend, e.g. s3fs). A `conn_id@bucket` netloc or conn_id= reads AIRFLOW_CONN_<ID> (key, secret, endpoint) and fails loudly if it is not set; with no
    connection the backend's default credentials apply, as in Airflow."""
    import pathlib
    from urllib.parse import urlsplit
    text = str(path)
    parts = urlsplit(text)
    if not parts.scheme or parts.scheme == "file":
        # `file://include/bears` is the RELATIVE path include/bears (fsspec's local filesystem reads the host part as the first segment), resolved against the task's working directory
        return _FilePath(parts.netloc + parts.path if parts.scheme else text)
    netloc = parts.netloc
    if "@" in netloc:
        conn_id, netloc = netloc.rsplit("@", 1)
    uri = f"{parts.scheme}://{netloc}{parts.path}"
    if conn_id:
        c = Connection.get(conn_id)
        if parts.scheme in ("s3", "s3a"):
            if c.login:
                storage_options.setdefault("key", c.login)
            if c.password:
                storage_options.setdefault("secret", c.password)
            ep = c.extra_dejson.get("endpoint_url") or c.extra_dejson.get("host")
            if ep:
                storage_options.setdefault("endpoint_url", ep)
        elif c.login:
            storage_options.setdefault("username", c.login)
            if c.password:
                storage_options.setdefault("password", c.password)
    try:
        from upath import UPath
    except ImportError as e:
        raise ImportError("ObjectStoragePath for " + parts.scheme + ":// needs `pip install universal-pathlib` and the fsspec backend for the scheme (s3fs, gcsfs, adlfs)") from e
    return UPath(uri, **storage_options)


class _LazyPath:
    """A path object built on first use (`af_lazy_path`): every attribute, operator and conversion goes to the real ObjectStoragePath."""

    def __init__(self, args, kwargs):
        object.__setattr__(self, "_args", (args, kwargs))
        object.__setattr__(self, "_real", None)

    def _get(self):
        if self._real is None:
            a, k = self._args
            object.__setattr__(self, "_real", ObjectStoragePath(*a, **k))
        return self._real

    def __getattr__(self, name):
        return getattr(self._get(), name)

    def __truediv__(self, other):
        return self._get() / other

    def __rtruediv__(self, other):
        return other / self._get()

    def __fspath__(self):
        return os.fspath(self._get())

    def __str__(self):
        return str(self._get())

    def __repr__(self):
        return repr(self._get())

    def __eq__(self, other):
        return self._get() == (other._get() if isinstance(other, _LazyPath) else other)

    def __hash__(self):
        return hash(self._get())


def af_lazy_path(path, conn_id=None, **storage_options):
    return _LazyPath((path,), {"conn_id": conn_id, **storage_options})


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


def af_gate(context, rule, inputs, task_id, setup=None, sc=None):
    """Airflow's decision for one task from its upstream states (TriggerRuleDep): None to run it, or a marker (Skipped, UpstreamFailed)."""
    if any(isinstance(x, Skipped) and x.hard for x in inputs):
        return _skip(context, True)
    for i, x in enumerate(inputs):
        if isinstance(x, _Branch) and not x.selects(task_id):
            return _skip(context, False)
        if sc and sc[i] is not None and not isinstance(x, (Skipped, Failed, UpstreamFailed)) and not x:
            return _skip(context, sc[i])      # a ShortCircuit that returned a falsy value; its value stays the plain bool Airflow records
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
        if python:                                                  # the verified venv pattern: a missing interpreter fails loudly, never the current one
            if not (os.path.exists(python) or shutil.which(python)):
                raise dg.Failure(description=f"the interpreter for this task's virtualenv ({python!r}) is missing; build it and point `python` at it: not falling back to the current interpreter")
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


class AfTaskRef:
    """A task defined in the DAG body that a callable names (`astronauts.operator.task_id`, `skip_roster.task_id`): the Airflow task id, group prefix included."""

    def __init__(self, task_id):
        self.task_id = task_id

    @property
    def operator(self):
        return self

    def __repr__(self):
        return f"AfTaskRef({self.task_id!r})"


class LoopDecision:
    """What an `until` callable of a loop returns in Airflow 3.4: whether to stop, and why."""

    def __init__(self, stop, reason=None):
        self.stop, self.reason = bool(stop), reason


def af_loop_ti(task_id, map_index):
    """The `ti` a loop body's task is handed: its id and (for a mapped task) the item's position, as `ti.map_index`."""
    return types.SimpleNamespace(task_id=task_id, map_index=-1 if map_index is None else map_index, try_number=1)


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
    raise dg.Failure(description=f"AIRFLOW-CAVEAT: {reason}", allow_retries=False)      # a stub fails the same way every time: no retries (they only delay the failure)


def af_run_if(fn, cond, run=True):
    """@task.run_if / @task.skip_if: the condition gets the task context; the task skips itself when run_if is false (skip_if is true)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if bool(cond(_AF_CTX.get() or {})) != run:
            raise AirflowSkipException("the run_if / skip_if condition skipped the task")
        return fn(*a, **kw)
    return wrapper


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
    """An Airflow Asset: a name or URI that maps to a Dagster asset key. A plain class with the attributes callables read (`name`, `uri`, `group`, `extra`), so a callable that is handed one
    (or builds one to return) runs; the key mapping for outlets and inlets is separate (`key`)."""

    def __init__(self, name=None, uri=None, *, group="asset", extra=None, **kwargs):
        self.uri = uri if uri is not None else name              # Airflow: one of the two given, the other defaults to it
        self.name = name if name is not None else uri
        self.group, self.extra = group, (extra if extra is not None else {})
        self.ref = uri or name

    @property
    def key(self):
        return asset_key_of(self.ref)

    def __repr__(self):
        return f"Asset(name={self.name!r}, uri={self.uri!r}, group={self.group!r}, extra={self.extra!r})"

    def __eq__(self, other):
        return isinstance(other, Asset) and (self.name, self.uri) == (other.name, other.uri)

    def __hash__(self):
        return hash((self.name, self.uri))


class Dataset(Asset):
    """Airflow 2's `Dataset(uri, extra=None)`: the same plain class (`uri`, `extra`; `name` is the URI)."""

    def __init__(self, uri=None, extra=None, **kwargs):
        super().__init__(name=uri, uri=uri, extra=extra, **kwargs)
        self.extra = extra

    def __repr__(self):
        return f"Dataset(uri={self.uri!r}, extra={self.extra!r})"


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

    def defer(self, *, trigger, method_name, kwargs=None, timeout=None):
        """A deferrable operator, run to completion in place: the trigger's `run()` (an async generator) is driven until its first event, then `method_name(context, event=<payload>, **kwargs)` runs and its
        result is the task's result. Airflow hands the wait to a triggerer process and frees the worker; here the step simply waits (a trigger that never yields an event never returns)."""
        event = _first_trigger_event(trigger)
        raise _TaskDeferred(getattr(self, method_name)(getattr(self, "_af_ctx", None), event=getattr(event, "payload", event), **(kwargs or {})))

    def _run_start_trigger(self, context):
        """An operator with `start_from_trigger` and no `execute`: Airflow starts the trigger from `start_trigger_args` without a worker, then calls `next_method`."""
        a = self.start_trigger_args
        cls = a.trigger_cls
        if isinstance(cls, str):
            raise RuntimeError(f"start_trigger_args names the trigger class {cls!r} as a string and the converter could not find that class next to the operator: copy the class into the module and pass it")
        event = _first_trigger_event(cls(**(a.trigger_kwargs or {})))
        return getattr(self, a.next_method)(context, event=getattr(event, "payload", event), **(a.next_kwargs or {}))


class _TaskDeferred(Exception):
    def __init__(self, result):
        super().__init__("deferred")
        self.result = result


class BaseTrigger:
    """Airflow's BaseTrigger as a plain class: a subclass's `run()` (an async generator that yields TriggerEvent) is driven by `BaseOperator.defer`."""

    def __init__(self, **kwargs):
        import logging
        self.log = logging.getLogger("airflow.triggers." + type(self).__name__)

    def serialize(self):
        raise NotImplementedError


class TriggerEvent:
    def __init__(self, payload):
        self.payload = payload

    def __repr__(self):
        return f"TriggerEvent({self.payload!r})"


class StartTriggerArgs:
    def __init__(self, trigger_cls, trigger_kwargs=None, next_method=None, next_kwargs=None, timeout=None):
        self.trigger_cls, self.trigger_kwargs, self.next_method, self.next_kwargs, self.timeout = trigger_cls, trigger_kwargs, next_method, next_kwargs, timeout


def _first_trigger_event(trigger):
    import asyncio

    async def first():
        async for event in trigger.run():
            return event
        raise RuntimeError("the trigger finished without yielding an event")
    return asyncio.run(first())


def sync_to_async(fn=None, **_):
    """asgiref's `sync_to_async` without the thread pool or the package: an async wrapper that calls the function in place."""
    import functools

    def deco(f):
        @functools.wraps(f)
        async def run(*a, **k):
            return f(*a, **k)
        return run
    return deco(fn) if fn is not None else deco


class _Conf:
    """`airflow.configuration.conf` read from the environment: `conf.get("section", "key", fallback=...)` is `AIRFLOW__SECTION__KEY`, else the fallback."""

    def get(self, section, key, fallback=None, **_):
        return os.environ.get(f"AIRFLOW__{section}__{key}".upper(), fallback)

    def getboolean(self, section, key, fallback=None, **_):
        v = os.environ.get(f"AIRFLOW__{section}__{key}".upper())
        return fallback if v is None else v.strip().lower() in ("1", "true", "t", "yes", "y")

    def getint(self, section, key, fallback=None, **_):
        v = os.environ.get(f"AIRFLOW__{section}__{key}".upper())
        return fallback if v is None else int(v)

    def getfloat(self, section, key, fallback=None, **_):
        v = os.environ.get(f"AIRFLOW__{section}__{key}".upper())
        return fallback if v is None else float(v)


conf = _Conf()


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
    op._af_ctx = ctx
    try:
        if type(op).execute is BaseOperator.execute and getattr(op, "start_trigger_args", None) is not None:
            return op._run_start_trigger(ctx)
        return op.execute(ctx)
    except _TaskDeferred as d:
        return d.result


def af_data(x):
    """The value a downstream task sees: None for a skipped task, the chosen ids for a branch, the flag for a short-circuit."""
    if isinstance(x, (Skipped, Failed, UpstreamFailed)):
        return None
    if isinstance(x, _BranchStr):
        return str(x)
    if isinstance(x, _BranchList):
        return list(x)
    return x
'''
exec(ITEM_SRC)           # item_key is also used at conversion time, so the converter and the generated runtime share one definition
RUNTIME += ITEM_SRC + GATE_SRC


RUNTIME += P.PROVIDER_RUNTIME
RUNTIME += K.RUNTIME
# depends_on_past: the library's VERIFIED step-event check, embedded from reference/rewrite-patterns/depends_on_past.py (index.json is read now, so a refined library is picked up);
# `previous_run_failed` is the library's own function, the wrapper only turns it into Airflow's wording.
DOP_GUARD = PE.status("depends_on_past", "STEP_FAILURE") == "VERIFIED"      # emitted only while the library still says VERIFIED; otherwise the old degraded caveat
RUNTIME += ("\n\n" + PE.pull("depends_on_past", ["_latest_id", "previous_run_failed"]) if DOP_GUARD else "\n\nprevious_run_failed = None") + '''

def af_depends_on_past(context):
    """depends_on_past=True: Airflow waits for the previous run's instance of this task; here the step fails when its previous run failed (waits become fails)."""
    if previous_run_failed(context):
        raise dg.Failure(description="depends_on_past: the previous run of this task did not succeed")
'''


def main(argv: list[str] | None = None) -> int:
    REG_RESOURCES.clear()
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
    ap.add_argument("--dbt-dir", default="dbt", help="where the dbt projects of converted Cosmos DAGs are expected, relative to the package (default dbt): the converter "
                    "writes components that point at <package>/<dbt-dir>/<project name>; it does not copy the projects")
    ap.add_argument("--granularity", choices=["task", "product"], default="product",
                    help="product (default): an asset is a data product. A step whose value feeds only one downstream task, with no outlets and no restart need of its own, "
                         "is folded into that task's asset as plain Python; EmptyOperators are glue; the report's `granularity` section lists the decision and reason for every task. "
                         "task (fallback): one asset per Airflow task, the faithful form")
    ap.add_argument("--partition-start", default="2025-01-01", help="where time-partitioned assets begin (Airflow's partitions have no start date)")
    ap.add_argument("--return-all", action="store_true", help="return every Python task's value (stored by the IO manager), for parity runs")
    a = ap.parse_args(argv)
    global RETURN_ALL, MAP_MODE, AF_MAJOR, DATASET_KEYS, CATCHUP, CAVEATS, OUTPUT, DBT_DIR, GRANULARITY
    GRANULARITY = a.granularity
    DBT_DIR = a.dbt_dir
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
    root_ = next((p for p in [a.out_dir.resolve(), *a.out_dir.resolve().parents] if (p / "pyproject.toml").exists() or (p / "dg.toml").exists()), a.out_dir.resolve().parent)
    rel_ = os.path.relpath(a.out_dir.resolve().parent, root_)
    K.PACKAGE_REL = "" if rel_ == "." else rel_.replace(os.sep, "/")           # the package directory relative to the Dagster project root
    (a.out_dir / "__init__.py").touch()
    _v = re.match(r"(\d+)\.(\d+)", str(ir.get("airflow_version") or ""))
    iso = bool(_v) and (int(_v.group(1)), int(_v.group(2))) < (2, 8)
    (a.out_dir.parent / "runtime.py").write_text(RUNTIME.replace("AF_ISO_DT = False", f"AF_ISO_DT = {iso}", 1).replace("AF_RT_MAJOR = 2", f"AF_RT_MAJOR = {AF_MAJOR}", 1).replace('DATASET_KEYS_MODE = "path"', f'DATASET_KEYS_MODE = "{DATASET_KEYS}"', 1).replace('DBT_DIR = "dbt"', f'DBT_DIR = {DBT_DIR!r}', 1))
    note = ("Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 "
            "(trigger timetable) and Airflow 2 (data-interval timetable).")
    report: dict[str, Any] = {"airflow_version": ir.get("airflow_version"), "dataset_keys": DATASET_KEYS, "dags": {}}
    produced: dict[str, str] = {}
    resources: set[str] = set()
    sf_resources: set[str] = set()
    prov_conns: dict[str, str] = {}
    external: set[str] = set()
    for dag in ir["dags"]:
        name = safe(dag["dag_id"])
        try:
            code, summary = convert_dag(dag, note, produced)
            compile(code, name, "exec")
            for rel, text in (summary.get("cosmos_files") or {}).items():       # dbt components (defs/<dag>_dbt*/defs.yaml) and generated profiles
                dest = (a.out_dir / rel[len("defs/"):]) if rel.startswith("defs/") else (a.out_dir.parent / rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text)
            summary = {**summary, "cosmos_files": sorted((summary.get("cosmos_files") or {}))}
            comp = component_files(dag, summary, a.out_dir.parent.name) if OUTPUT == "components" and not summary.get("cosmos") and not summary.get("cosmos_steps") else None
            if comp:
                files, over = comp
                for rel, text in files.items():
                    dest = a.out_dir.parent / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(text)
                    if rel.startswith("callables/"):
                        (dest.parent / "__init__.py").touch()
                summary = {"resource_types": [], "registry_resources": [], **summary, **over, "converted_with_component": True, "component_types": sorted({ln.split('dagster_community_components.')[1] for t_ in files.values() for ln in t_.splitlines() if ln.startswith('type: dagster_community_components.')})}
            else:
                (a.out_dir / f"{name}.py").write_text(code)
                summary = {**summary, "output": "python"}
                ti = summary.pop("test_info", None)
                if ti:                                           # a pytest file per converted DAG (see gen_tests.py)
                    pkg_dir = a.out_dir.resolve().parent
                    tests_root = root_ if root_ != pkg_dir else pkg_dir.parent
                    (tests_root / "tests").mkdir(exist_ok=True)
                    tfile = tests_root / "tests" / f"test_{name}.py"
                    tfile.write_text(G.render({**ti, "module": f"{pkg_dir.name}.{a.out_dir.name}.{name}"}))
                    summary = {**summary, "generated_tests": {"file": f"tests/{tfile.name}", "functions": 5 if ti["materialize_skip"] is None else 5, "materializes": ti["materialize_skip"] is None,
                                                             "materialize_skip_reason": ti["materialize_skip"], "needs_shell": ti["needs_shell"]}}
            summary.pop("test_info", None)
            report["dags"][dag["dag_id"]] = {"status": "converted", **summary}
            resources |= set(summary["resources"])
            sf_resources |= set(summary["snowflake_resources"])
            prov_conns.update(summary.get("provider_connections") or {})
            external |= set(summary["external"])
        except Defer as e:
            report["dags"][dag["dag_id"]] = {"status": "deferred", "reason": str(e)}
        except SyntaxError as e:
            report["dags"][dag["dag_id"]] = {"status": "deferred", "reason": f"generated code did not compile: {e.msg}"}
        except Exception as e:                       # a converter bug must show up as a named deferral, never a crash or a silent skip
            report["dags"][dag["dag_id"]] = {"status": "deferred", "reason": f"CONVERTER ERROR {type(e).__name__}: {e}"}
    if REG_RESOURCES:                                # one file for every registry resource, so two DAGs that use one connection do not register its key twice
        d_ = a.out_dir / "registry_resources"
        d_.mkdir(parents=True, exist_ok=True)
        (d_ / "defs.yaml").write_text("\n---\n".join(f"type: dagster_community_components.{typ_}\nattributes:\n  resource_key: {json.dumps(k_)}\n" + "".join(f"  {ak}: {json.dumps(av)}\n" for ak, av in at_.items())
                                                      for k_, (typ_, at_) in sorted(REG_RESOURCES.items())))
    owned = {"/".join(k) for v in report["dags"].values() if v["status"] == "converted" for k in [*v["keys"], *v.get("catalog_keys", [])]}
    ext = sorted(e for e in external if e not in owned)
    lib_res = {c: k for c, k in prov_conns.items() if k in P.RESOURCE_KINDS}
    if resources or sf_resources or lib_res:
        lines = ['"""Airflow connections become resources under the same key (credentials from the environment)."""',
                 "import dagster as dg"]
        if resources:
            lines.append("from dagster_aws.s3 import S3Resource")
        if sf_resources:
            lines.append("from dagster_snowflake import SnowflakeResource")
        lines += sorted({P.RESOURCE_KINDS[k]["import"] for k in lib_res.values()})
        by_conn = {**{c: CN.resource_entry("aws", c) for c in resources}, **{c: CN.resource_entry("snowflake", c) for c in sf_resources},
                   **{c: P.RESOURCE_KINDS[k]["ctor"](c) for c, k in lib_res.items()}}
        entries = [f"{c!r}: {ctor}" for c, ctor in sorted(by_conn.items())]
        (a.out_dir / "resources.py").write_text("\n".join(lines) + "\n\n\n@dg.definitions\ndef resources() -> dg.Definitions:\n    return dg.Definitions(resources={" + ", ".join(entries) + "})\n")
    if resources or sf_resources:                   # the S3 and Snowflake connections' variable names, from the same table connections_to_resources.py uses
        report.setdefault("connections", {})
        for kind_, ids_ in (("aws", resources), ("snowflake", sf_resources)):
            for c_ in sorted(ids_):
                report["connections"][c_] = {"kind": kind_, "environment": CN.env_names(kind_, c_), "read_by": "resource in resources.py"}
    if prov_conns:                                   # what each provider connection needs, by name only: no value is ever copied
        report.setdefault("connections", {}).update({c: {"kind": k, "environment": (P.RESOURCE_KINDS[k]["env"](c) if k in P.RESOURCE_KINDS else P.ENV_KINDS[k](c)),
                                                         "read_by": "resource in resources.py" if k in P.RESOURCE_KINDS else "the step, when it runs"}
                                                     for c, k in sorted(prov_conns.items()) if k in P.RESOURCE_KINDS or k in P.ENV_KINDS})
    if ext:
        (a.out_dir / "sources.py").write_text(
            '"""Assets that converted DAGs read or are triggered by but that no converted DAG produces."""\n'
            "import dagster as dg\n\n\n@dg.definitions\ndef sources() -> dg.Definitions:\n    return dg.Definitions(assets=[\n" +
            "".join(f"        dg.AssetSpec(key={e.split('/')!r}),\n" for e in ext) + "    ])\n")
    conv = {k: v for k, v in report["dags"].items() if v["status"] == "converted"}
    report["generated"] = {"test_files": sum(1 for v in conv.values() if v.get("generated_tests")), "test_functions": sum(v["generated_tests"]["functions"] for v in conv.values() if v.get("generated_tests")),
                           "materialize_tests": sum(1 for v in conv.values() if (v.get("generated_tests") or {}).get("materializes")),
                           "materialize_skipped": sum(1 for v in conv.values() if v.get("generated_tests") and not v["generated_tests"]["materializes"]),
                           "asset_checks": sum(len(v.get("asset_checks") or []) for v in conv.values())}
    dbt_projects: dict[str, dict[str, Any]] = {}
    for dag_id_, v in conv.items():
        for cl in [*(v.get("cosmos") or []), *(v.get("cosmos_steps") or [])]:
            nm = cl["project"]
            e = dbt_projects.setdefault(nm, {"airflow_path": cl.get("airflow_project_path") or cl.get("project_path"), "expected_path": f"{a.out_dir.parent.name}/{DBT_DIR}/{nm}",
                                             "used_unchanged": True, "converter_changes": [], "used_by": [], "environment": []})
            for gen in cl.get("profiles_generated") or []:
                ch = f"generated {gen} (every connection field read with env_var()); the project's own files are not modified"
                if ch not in e["converter_changes"]:
                    e["converter_changes"].append(ch)
            if cl.get("prefix"):
                ch = f"asset keys are prefixed `{cl['prefix']}/` (the component's key_prefix; no model file changes)"
                if ch not in e["converter_changes"]:
                    e["converter_changes"].append(ch)
            e["used_by"].append(f"{dag_id_}:{cl.get('id') or cl.get('task')}")
            e["environment"] = sorted(set(e["environment"]) | set(cl.get("environment") or cl.get("env") or []))
    if dbt_projects:                                   # names only: the profile fields every generated profiles.yml reads, and where each dbt project must be copied to
        report["dbt"] = {"projects": dbt_projects, "dbt_dir": DBT_DIR}
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
    supported = ELIDED | PY_TASKS | BASH | BASH_DEC | S3_OPS | SNOWFLAKE_OPS | PROV | P.SQL_OPS | set(P.ALIASES)
    ops = Counter(t["type"] for d in ir["dags"] for t in d["tasks"])
    report["operators"] = {ty: {"tasks": n, "mapped_by_converter": ty in supported, **(HINTS.get(ty) or ({"registry": [OPERATOR_COMPONENTS[ty]]} if ty in OPERATOR_COMPONENTS else {}))} for ty, n in ops.most_common()}
    a.report.write_text(json.dumps(report, indent=1) + "\n")
    c = Counter(v["status"] for v in report["dags"].values())
    print(f"{c['converted']} converted, {c['deferred']} deferred of {len(report['dags'])} DAGs; resources {sorted(resources | sf_resources)}; external assets {len(ext)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
