#!/usr/bin/env python3
"""Extract an intermediate representation (IR) of real Airflow DAGs by loading them with Airflow's own parser.

    python3 airflow_ir.py <dags_folder> [<dags_folder> ...] --out ir.json [--pythonpath <project root>]

WARNING: this EXECUTES the DAG files, exactly as Airflow's parser does. Run it only on code you trust, in a
sandbox, in an environment with the same Airflow version and providers the project uses. AIRFLOW_HOME points at a
temp directory; no real metadata database is touched.

Because Airflow has already expanded loops, factories, dag-factory YAML and Blueprints, the IR contains the DAGs as
they really are: every task, resolved arguments, dependencies, schedule, retries, pools, groups, callable source.
`convert_ir.py` turns this IR into Dagster code and reports what it cannot convert.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import tempfile
import textwrap
import warnings
from pathlib import Path
from typing import Any

# A clean sandbox BEFORE anything imports Airflow: importing it reads AIRFLOW_HOME and may open a metadata database, and an existing
# ~/airflow created by a different release makes older Airflow fail with an alembic revision error.
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp(prefix="af_ir_")
os.environ.setdefault("AIRFLOW__CORE__LOAD_EXAMPLES", "False")


def _jsonable(v: Any, depth: int = 0) -> Any:
    """Best-effort JSON form of an operator argument; anything exotic becomes {"repr": ...} so nothing is silently lost."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if depth > 4:
        return {"repr": repr(v)[:200]}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x, depth + 1) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x, depth + 1) for k, x in v.items()}
    import datetime as _d
    import enum as _e
    if type(v).__name__ == "ArgNotSet":
        return None
    if isinstance(v, _d.datetime):
        return {"datetime": v.isoformat()}
    if isinstance(v, _d.time):
        return {"time": v.isoformat()}
    if isinstance(v, _e.Enum):
        return str(v.name)
    name = type(v).__name__
    if name in ("XComArg", "PlainXComArg", "MappedArgument") or hasattr(v, "operator") and hasattr(v, "key"):
        op = getattr(v, "operator", None)
        return {"xcom_from": getattr(op, "task_id", None), "key": getattr(v, "key", None)}
    if hasattr(v, "total_seconds"):
        return {"seconds": v.total_seconds()}
    return {"repr": repr(v)[:200]}


def _callable_info(fn: Any) -> dict[str, Any] | None:
    if fn is None or not callable(fn):
        return None
    info: dict[str, Any] = {"name": getattr(fn, "__name__", "?"), "module": getattr(fn, "__module__", None)}
    try:
        info["source"] = textwrap.dedent(inspect.getsource(fn))
        info["file"] = inspect.getsourcefile(fn)
        info["params"] = list(inspect.signature(fn).parameters)
    except (OSError, TypeError, ValueError) as e:
        info["source_error"] = f"{type(e).__name__}: {e}"
    return info


def _asset_leaves(cond: Any) -> tuple[list[str], str]:
    """Leaf assets of an Airflow asset/dataset schedule condition, and its logic: single, all or any."""
    if cond is None:
        return [], "single"
    objs = getattr(cond, "objects", None)
    if objs is None:
        return [str(getattr(cond, "uri", None) or getattr(cond, "name", None) or cond)], "single"
    name = type(cond).__name__
    logic = "all" if ("All" in name or "And" in name) else "any"
    leaves: list[str] = []
    for o in objs:
        sub, sublogic = _asset_leaves(o)
        leaves += sub
        if sublogic != "single" and sublogic != logic:
            logic = "mixed"
    return leaves, logic


def _timetable(dag: Any) -> dict[str, Any]:
    tt = getattr(dag, "timetable", None)
    cls = type(tt).__name__ if tt is not None else "None"
    out: dict[str, Any] = {"class": cls}
    for attr in ("expression", "_expression", "cron"):
        if getattr(tt, attr, None) and isinstance(getattr(tt, attr), str):
            out["cron"] = getattr(tt, attr)
    tz = getattr(tt, "timezone", None) or getattr(tt, "_timezone", None) or getattr(dag, "timezone", None)
    out["timezone"] = str(tz) if tz is not None else None
    interval = getattr(tt, "interval", None) or getattr(tt, "_interval", None) or getattr(tt, "_delta", None) or getattr(tt, "delta", None)
    if interval is not None and hasattr(interval, "total_seconds"):
        out["interval_seconds"] = interval.total_seconds()
    out["summary"] = str(getattr(tt, "summary", ""))[:200]
    cond = getattr(tt, "asset_condition", None) or getattr(tt, "dataset_condition", None)
    if cond is not None:
        out["assets"], out["asset_logic"] = _asset_leaves(cond)
    else:                                                   # Airflow 2.4 to 2.8: a plain list of datasets, all required
        listed = getattr(tt, "datasets", None) or getattr(dag, "dataset_triggers", None)
        if listed is not None and hasattr(listed, "objects"):               # 2.9: a DatasetAll / DatasetAny condition
            out["assets"], out["asset_logic"] = _asset_leaves(listed)
        elif isinstance(listed, (list, tuple)) and listed:
            out["assets"], out["asset_logic"] = [str(getattr(x, "uri", None) or x) for x in listed], ("all" if len(listed) > 1 else "single")
    if cls in ("NullTimetable",):
        out["kind"] = "manual"
    elif cls in ("OnceTimetable",):
        out["kind"] = "once"
    elif cls == "CronTriggerTimetable" and "cron" in out:
        out["kind"] = "cron_trigger"          # Airflow 3 default for cron strings: logical date = the tick
    elif cls == "CronDataIntervalTimetable" and "cron" in out:
        out["kind"] = "cron_data_interval"    # Airflow 2 default: the run covers the interval that just ended
    elif "Asset" in cls or "Dataset" in cls:
        out["kind"] = "asset_triggered"
    elif cls == "DeltaTriggerTimetable" or cls == "DeltaDataIntervalTimetable":
        out["kind"] = "delta"
    else:
        out["kind"] = "other"
    if cls in ("CronPartitionTimetable", "PartitionedAssetTimetable", "PartitionedAtRuntime"):       # Airflow 3.2+: partitioned runs
        try:
            from airflow.serialization.encoders import encode_timetable      # Airflow's own structured form of the timetable and its mappers
            out["partition"] = json.loads(json.dumps(encode_timetable(tt), default=str))
        except Exception as e:  # noqa: BLE001
            out["partition_error"] = repr(e)[:200]
        out["kind"] = {"CronPartitionTimetable": "cron_partition", "PartitionedAssetTimetable": "partitioned_asset", "PartitionedAtRuntime": "partitioned_at_runtime"}[cls]
    return out


def _overridden_hooks(task: Any) -> list[str]:
    """pre_execute / post_execute are no-op methods on every operator; report them only when something replaced them
    (an instance hook such as `@task.run_if` / `skip_if` / `pre_execute=`, or a subclass override)."""
    try:
        from airflow.sdk import BaseOperator
    except ImportError:
        from airflow.models.baseoperator import BaseOperator
    out = []
    for h in ("pre_execute", "post_execute"):
        # `pre_execute=` and `@task.run_if` / `skip_if` store the hook in a private attribute; a subclass may override the method
        if getattr(task, f"_{h}_hook", None) or h in vars(task) or getattr(type(task), h, None) is not getattr(BaseOperator, h, None):
            out.append(h)
    return out


def _expand_ir(task: Any) -> dict[str, Any] | None:
    """What a mapped task iterates over: literal values, an upstream task's output, or something this extractor cannot name."""
    if not (any(c.__name__ == "MappedOperator" for c in type(task).__mro__) or getattr(task, "is_mapped", False)):
        return None
    try:
        ei = task._get_specified_expand_input() if hasattr(task, "_get_specified_expand_input") else task.expand_input
    except AttributeError:
        if not hasattr(task, "mapped_kwargs"):
            return {"form": "unknown", "error": "no expand input found"}
        import types as _t                                                # Airflow 2.3: the expanded arguments are `mapped_kwargs`
        ei = type("DictOfListsExpandInput", (), {})()
        ei.value = dict(getattr(task, "mapped_op_kwargs", None) or task.mapped_kwargs)       # a decorated task keeps its expanded arguments in mapped_op_kwargs
    except Exception as e:  # noqa: BLE001
        return {"form": "unknown", "error": repr(e)[:120]}
    val = getattr(ei, "value", None)

    def one(v: Any) -> dict[str, Any]:
        if isinstance(v, (list, tuple)):
            try:
                return {"kind": "literal", "value": json.loads(json.dumps(list(v), default=str))}
            except (TypeError, ValueError):
                return {"kind": "other", "repr": repr(v)[:120]}
        if type(v).__name__.endswith("XComArg"):
            op = getattr(v, "operator", None)
            return {"kind": "xcom", "wrapper": type(v).__name__, "task_id": getattr(op, "task_id", None), "key": getattr(v, "key", None)}
        return {"kind": "other", "repr": repr(v)[:120]}

    if type(ei).__name__ == "DictOfListsExpandInput" and isinstance(val, dict):
        pk = getattr(task, "partial_kwargs", None) or {}
        fixed: dict[str, Any] = {}
        bad: list[str] = []
        for k, v in (pk.get("op_kwargs") or {}).items():
            try:
                fixed[k] = json.loads(json.dumps(v))
            except (TypeError, ValueError):
                bad.append(k)
        partial: dict[str, Any] = {}
        for k in ("bash_command", "env", "cwd"):               # BashOperator settings that are not expanded stay fixed
            if k in pk and k not in val:
                try:
                    partial[k] = json.loads(json.dumps(pk[k]))
                except (TypeError, ValueError):
                    bad.append(k)
        return {"form": "expand", "args": {k: one(v) for k, v in val.items()}, "fixed": fixed, "fixed_unserializable": bad,
                "partial": partial, "op_args": bool(pk.get("op_args"))}
    if type(ei).__name__ == "ListOfDictsExpandInput":
        return {"form": "expand_kwargs", "args": {"*": one(val)}}
    return {"form": type(ei).__name__, "args": {}}


def _base_params() -> set[str]:
    try:
        from airflow.sdk import BaseOperator
    except ImportError:
        from airflow.models.baseoperator import BaseOperator
    return set(inspect.signature(BaseOperator.__init__).parameters)


_BASE_PARAMS = _base_params()


def _sensor_ir(task: Any) -> dict[str, Any] | None:
    """The polling settings every sensor has (and a TimeSensor's resolved target)."""
    try:
        from airflow.sdk.bases.sensor import BaseSensorOperator
    except ImportError:
        from airflow.sensors.base import BaseSensorOperator
    if not isinstance(task, BaseSensorOperator) and not (hasattr(task, "poke_interval") and hasattr(task, "timeout")):
        return None

    def secs(v: Any) -> Any:
        return v.total_seconds() if hasattr(v, "total_seconds") else v
    out = {"poke_interval": secs(getattr(task, "poke_interval", 60.0)), "timeout": secs(getattr(task, "timeout", 604800.0)), "mode": getattr(task, "mode", "poke"),
           "soft_fail": bool(getattr(task, "soft_fail", False)), "exponential_backoff": bool(getattr(task, "exponential_backoff", False)),
           "silent_fail": bool(getattr(task, "silent_fail", False)), "never_fail": bool(getattr(task, "never_fail", False))}
    td = getattr(task, "target_datetime", None)
    if td is not None:
        out["target_datetime"] = td.isoformat()
    return out


def _task_ir(task: Any) -> dict[str, Any]:
    t: dict[str, Any] = {
        "task_id": task.task_id, "type": task.task_type, "module": type(task).__module__,
        "group": getattr(getattr(task, "task_group", None), "group_id", None),
        "upstream": sorted(task.upstream_task_ids), "downstream": sorted(task.downstream_task_ids),
        "retries": task.retries, "retry_delay_s": task.retry_delay.total_seconds() if task.retry_delay else None,
        "pool": task.pool if task.pool and task.pool != "default_pool" else None,
        "do_xcom_push": bool(getattr(task, "do_xcom_push", True)),
        "sensor": _sensor_ir(task),
        "trigger_rule": str(getattr(task.trigger_rule, "value", task.trigger_rule)),
        "owner": task.owner, "queue": task.queue if getattr(task, "queue", None) != "default" else None,
        "execution_timeout_s": task.execution_timeout.total_seconds() if getattr(task, "execution_timeout", None) else None,
        # Airflow 2 decorated mapped tasks are DecoratedMappedOperator, a SUBCLASS of MappedOperator: test the whole MRO
        "expand": _expand_ir(task),
        "is_mapped": any(c.__name__ == "MappedOperator" for c in type(task).__mro__) or bool(getattr(task, "is_mapped", False)),
        "is_setup": bool(getattr(task, "is_setup", False)), "is_teardown": bool(getattr(task, "is_teardown", False)),
        "on_failure_fail_dagrun": bool(getattr(task, "on_failure_fail_dagrun", False)),
        "short_circuit_hard": bool(getattr(task, "ignore_downstream_trigger_rules", True)),
        # settings that change behavior: the converter maps them or defers the DAG, never ignores them
        "depends_on_past": bool(getattr(task, "depends_on_past", False)), "wait_for_downstream": bool(getattr(task, "wait_for_downstream", False)),
        "max_active_tis_per_dag": getattr(task, "max_active_tis_per_dag", None), "max_active_tis_per_dagrun": getattr(task, "max_active_tis_per_dagrun", None),
        "pool_slots": getattr(task, "pool_slots", 1), "retry_exponential_backoff": bool(getattr(task, "retry_exponential_backoff", False)),
        "max_retry_delay_s": task.max_retry_delay.total_seconds() if getattr(task, "max_retry_delay", None) else None,
        "sla_s": task.sla.total_seconds() if getattr(task, "sla", None) else None,
        "callback_fns": {a: [_callable_info(f) for f in (getattr(task, a, None) if isinstance(getattr(task, a, None), (list, tuple)) else [getattr(task, a, None)])
                              if callable(f)] for a in ("on_failure_callback", "on_success_callback", "on_retry_callback") if getattr(task, a, None)},
        "callbacks": [a for a in ("on_failure_callback", "on_success_callback", "on_retry_callback", "on_skipped_callback", "on_execute_callback")
                      if getattr(task, a, None)] + _overridden_hooks(task),
        "outlets": [str(getattr(o, "uri", None) or getattr(o, "name", None) or o) for o in (getattr(task, "outlets", None) or [])],
        "inlets": [str(getattr(o, "uri", None) or getattr(o, "name", None) or o) for o in (getattr(task, "inlets", None) or [])],
        "template_fields": {},
    }
    for f in getattr(task, "template_fields", ()) or ():
        if hasattr(task, f):
            t["template_fields"][f] = _jsonable(getattr(task, f, None))
    # constructor arguments (many are not template fields, e.g. a Snowflake operator's stage and table)
    try:
        own = set(inspect.signature(type(task).__init__).parameters) - _BASE_PARAMS - {"self", "args", "kwargs"}
        t["args"] = {n: _jsonable(getattr(task, n)) for n in sorted(own) if hasattr(task, n) and not callable(getattr(task, n, None))}
    except (TypeError, ValueError):
        t["args"] = {}
    # SQL (or other) template files named by a templated field: read them now, from the DAG's template search path
    files: dict[str, str] = {}
    exts = tuple(getattr(task, "template_ext", ()) or ())
    roots = [*(getattr(getattr(task, "dag", None), "template_searchpath", None) or []), os.path.dirname(getattr(getattr(task, "dag", None), "fileloc", "") or "")]
    for f, val in t["template_fields"].items():
        if isinstance(val, str) and exts and val.endswith(exts):
            for r in roots:
                cand = os.path.join(r, val)
                if r and os.path.isfile(cand):
                    files[f] = Path(cand).read_text()
                    break
    if files:
        t["template_files"] = files
    fn = getattr(task, "python_callable", None)
    if fn is not None:
        t["callable"] = _callable_info(fn)
        t["op_args"] = _jsonable(list(getattr(task, "op_args", None) or []))
        t["op_kwargs"] = _jsonable(dict(getattr(task, "op_kwargs", None) or {}))
    return t


def _dag_ir(dag: Any, folder: Path) -> dict[str, Any]:
    da = getattr(dag, "default_args", None) or {}
    return {
        "dag_id": dag.dag_id, "file": os.path.relpath(dag.fileloc, folder), "description": dag.description,
        "tags": sorted(dag.tags or []), "catchup": dag.catchup, "max_active_runs": dag.max_active_runs,
        "start_date": str(getattr(dag, "start_date", None) or da.get("start_date") or "") or None,
        "is_subdag": bool(getattr(dag, "is_subdag", False)), "parent_dag_id": getattr(getattr(dag, "parent_dag", None), "dag_id", None),
        "timetable": _timetable(dag), "default_args": {k: _jsonable(v) for k, v in da.items() if k != "start_date"},
        "params": {k: _jsonable(getattr(v, "value", v)) for k, v in (getattr(dag, "params", None) or {}).items()} if getattr(dag, "params", None) else {},
        "dag_settings": {k: _jsonable(getattr(dag, k, None)) for k in ("fail_stop", "max_active_tasks", "max_active_runs", "dagrun_timeout",
                         "max_consecutive_failed_dag_runs", "render_template_as_native_obj", "is_paused_upon_creation", "concurrency")
                         if getattr(dag, k, None) is not None},
        "has_callbacks": any(getattr(dag, a, None) for a in ("on_failure_callback", "on_success_callback", "sla_miss_callback")),
        "tasks": [_task_ir(t) for t in dag.tasks],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("ir.json"))
    ap.add_argument("--pythonpath", type=Path, action="append", default=[], help="extra import root (e.g. the project root)")
    a = ap.parse_args(argv)
    warnings.filterwarnings("ignore")
    os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp(prefix="af_ir_")
    os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
    for p in a.pythonpath:
        sys.path.insert(0, str(p.resolve()))
    try:
        try:
            from airflow.dag_processing.dagbag import DagBag      # Airflow 3
        except ImportError:
            from airflow.models.dagbag import DagBag              # Airflow 2
    except ImportError as e:
        print(f"Airflow is not importable here: {e}", file=sys.stderr)
        return 2
    import airflow
    if airflow.__version__.startswith("2."):
        from airflow.utils.db import initdb
        initdb()
    dags: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for folder in [f.resolve() for f in a.folders]:
        try:
            bag = DagBag(dag_folder=str(folder), include_examples=False, safe_mode=False)
        except TypeError:
            bag = DagBag(dag_folder=str(folder), safe_mode=False)
        for path, err in bag.import_errors.items():
            errors[os.path.relpath(path, folder)] = err.strip().splitlines()[-1][:300]
        for dag in bag.dags.values():
            try:
                dags.append(_dag_ir(dag, folder))
            except Exception as e:                      # never lose a DAG silently
                errors[f"{dag.dag_id} (IR extraction)"] = f"{type(e).__name__}: {e}"[:300]
    a.out.write_text(json.dumps({"airflow_version": airflow.__version__, "dags": dags, "import_errors": errors}, indent=1, default=str) + "\n")
    print(f"IR: {len(dags)} DAGs, {sum(len(d['tasks']) for d in dags)} tasks, {len(errors)} problem(s); Airflow {airflow.__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
