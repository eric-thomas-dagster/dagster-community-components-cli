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
import re
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
    if name == "LiteralValue":                               # airflow.sdk.literal(): the string is not rendered as a template
        return "{% raw %}" + str(getattr(v, "value", "")) + "{% endraw %}"
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
    if info["name"] == "<lambda>":                           # rewrite the lambda as a function: `lambda x: x + 1` -> `def _lambda(x): return x + 1`
        try:
            import ast
            path = inspect.getsourcefile(fn)
            line = fn.__code__.co_firstlineno
            found = [n for n in ast.walk(ast.parse(Path(path).read_text())) if isinstance(n, ast.Lambda) and n.lineno == line
                     and len(n.args.args) == fn.__code__.co_argcount]
            if len(found) == 1:
                lam = found[0]
                info.update(name="_lambda", source=f"def _lambda({ast.unparse(lam.args)}):\n    return {ast.unparse(lam.body)}\n", file=path,
                            params=list(inspect.signature(fn).parameters))
                return info
        except Exception:                                    # noqa: BLE001
            pass
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


def _asset_tree(cond: Any) -> Any:
    """The asset condition as a tree: a name for a leaf, {"op": "and" | "or", "items": [...]} for a combination."""
    objs = getattr(cond, "objects", None)
    if objs is None:
        return str(getattr(cond, "uri", None) or getattr(cond, "name", None) or cond)
    name = type(cond).__name__
    return {"op": "and" if ("All" in name or "And" in name) else "or", "items": [_asset_tree(o) for o in objs]}


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
        out["asset_tree"] = _asset_tree(cond)
    else:                                                   # Airflow 2.4 to 2.8: a plain list of datasets, all required
        listed = getattr(tt, "datasets", None) or getattr(dag, "dataset_triggers", None)
        if listed is not None and hasattr(listed, "objects"):               # 2.9: a DatasetAll / DatasetAny condition
            out["assets"], out["asset_logic"] = _asset_leaves(listed)
            out["asset_tree"] = _asset_tree(listed)
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
            w = type(v).__name__
            if w == "MapXComArg":                                  # `.map(f)`: the inner source with each item passed through f
                return {"kind": "xcom", "wrapper": w, "inner": one(v.arg), "callables": [_callable_info(f) for f in v.callables]}
            if w in ("ConcatXComArg", "ZipXComArg"):             # `a.concat(b)` / `a.zip(b)`
                out = {"kind": "xcom", "wrapper": w, "parts": [one(x) for x in v.args]}
                if w == "ZipXComArg":
                    out["fillvalue"] = None if type(getattr(v, "fillvalue", None)).__name__ == "ArgNotSet" else getattr(v, "fillvalue", None)
                return out
            op = getattr(v, "operator", None)
            return {"kind": "xcom", "wrapper": w, "task_id": getattr(op, "task_id", None), "key": getattr(v, "key", None)}
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


def _class_info(cls: type) -> dict[str, Any] | None:
    """Source of a user-defined operator class, so the converter can copy it."""
    try:
        return {"name": cls.__name__, "module": cls.__module__, "file": inspect.getsourcefile(cls), "source": textwrap.dedent(inspect.getsource(cls)),
                "template_fields": list(getattr(cls, "template_fields", ()) or ())}
    except (OSError, TypeError) as e:
        return {"name": cls.__name__, "module": cls.__module__, "source_error": repr(e)[:120]}


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


def _loop_ir(task: Any) -> dict[str, Any] | None:
    """The gate of an Airflow 3.4 task-group loop (`group.loop(max_iterations=, until=)`): the group, the iteration limit and the `until` callable."""
    if type(task).__name__ != "LoopGateOperator":
        return None
    grp = getattr(task, "task_group", None)
    until = getattr(task, "until", None)
    return {"group": getattr(grp, "group_id", None), "max_iterations": getattr(grp, "max_iterations", None) or getattr(task, "max_iterations", None),
            "until": _callable_info(until) if callable(until) else None}


_SECRET_NAME = re.compile(r"pass|secret|token|key|credential|private|auth", re.I)
_COSMOS_OP = re.compile(r"^Dbt(RunOperation|Run|Test|Seed|Build|Snapshot|Ls|Source|Compile|Clone|Docs|Deps)(.*?)Operator$")


def _redact(v: Any) -> Any:
    """A dict of settings with the value of every secret-looking key replaced: nothing that looks like a credential reaches the IR."""
    if isinstance(v, dict):
        return {str(k): ("<redacted>" if _SECRET_NAME.search(str(k)) else _redact(x)) for k, x in v.items()}
    return _jsonable(v)


def _cosmos_profile(pc: Any) -> dict[str, Any] | None:
    """Cosmos ProfileConfig: names and structure only. A profile mapping lists the dbt fields it fills from an Airflow connection and which are
    secret; its `profile_args` are kept with secret-looking keys redacted. The profiles.yml file's contents are never read."""
    if pc is None:
        return None
    out: dict[str, Any] = {"profile_name": getattr(pc, "profile_name", None), "target_name": getattr(pc, "target_name", None),
                           "profiles_yml_filepath": str(pc.profiles_yml_filepath) if getattr(pc, "profiles_yml_filepath", None) else None, "mapping": None}
    pm = getattr(pc, "profile_mapping", None)
    if pm is not None:
        out["mapping"] = {"class": type(pm).__name__, "conn_id": getattr(pm, "conn_id", None), "dbt_profile_type": getattr(pm, "dbt_profile_type", None),
                          "dbt_fields": sorted(getattr(pm, "airflow_param_mapping", {}) or {}), "secret_fields": sorted(getattr(pm, "secret_fields", []) or []),
                          "profile_args": _redact(getattr(pm, "profile_args", None) or {})}
    return out


def _cosmos_node(extra: Any) -> dict[str, Any] | None:
    n = (extra or {}).get("dbt_node_config") if isinstance(extra, dict) else None
    if not n:
        return None
    return {"unique_id": n.get("unique_id"), "resource_type": n.get("resource_type"), "depends_on": list(n.get("depends_on") or []), "tags": list(n.get("tags") or []),
            "fqn": n.get("fqn")}


def _cosmos_group_root(task: Any) -> str | None:
    """The DbtTaskGroup this task was rendered into (outermost one), "" when the whole DAG is a DbtDag, None for a standalone Cosmos operator."""
    root = None
    g = getattr(task, "task_group", None)
    while g is not None and getattr(g, "group_id", None):
        if g.__class__.__name__ == "DbtTaskGroup":
            root = g.group_id
        g = getattr(g, "parent_group", None)
    if root is None and getattr(task, "dag", None) is not None and task.dag.__class__.__name__ == "DbtDag":
        return ""
    return root


_CONTAINER_SETTINGS = ("image", "namespace", "secrets", "resources", "volumes", "volume_mounts", "env_vars", "node_selector", "tolerations", "affinity", "service_account_name",
                       "labels", "image_pull_secrets", "pod_template_file", "full_pod_spec", "config_file", "kubernetes_conn_id", "cmds", "arguments", "entrypoint",
                       "docker_url", "network_mode", "mounts", "cluster_name", "region", "cpu", "memory", "container_name", "task_definition", "launch_type", "overrides",
                       "project_id", "job_name", "resource_group", "subscription_id")


def _container_ir(task: Any) -> dict[str, Any]:
    """Container execution settings of a Cosmos Kubernetes/Docker/ECS/... operator: the NAMES of the settings that are set (values can hold secrets and are not
    recorded), and the command the container was told to run when the DAG overrides it (`cmds` / `entrypoint`)."""
    names = sorted(n for n in _CONTAINER_SETTINGS if getattr(task, n, None) not in (None, "", [], {}, ()))
    override = None
    for n in ("entrypoint", "cmds"):
        v = getattr(task, n, None)
        if v:
            override = [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
    return {"settings": names, "command_override": override}


def _cosmos_task_ir(task: Any) -> dict[str, Any] | None:
    """The settings of an Astronomer Cosmos operator (DbtRunLocalOperator, DbtBuildKubernetesOperator, ...), or None for any other task."""
    cls = type(task)
    if not cls.__module__.startswith("cosmos"):
        return None
    m = _COSMOS_OP.match(cls.__name__)
    if not m:
        return {"kind": None, "mode": None, "class": cls.__name__, "group_root": _cosmos_group_root(task)}
    g = lambda n, d=None: getattr(task, n, d)          # noqa: E731
    inv = g("invocation_mode")
    return {
        "class": cls.__name__, "kind": m.group(1).lower() if m.group(1) != "RunOperation" else "run_operation", "mode": (m.group(2) or "Local").lower(),
        "group_root": _cosmos_group_root(task), "project_dir": str(g("project_dir")) if g("project_dir") else None, "profile": _cosmos_profile(g("profile_config")),
        "select": g("select"), "exclude": g("exclude"), "selector": g("selector"), "models": g("models"), "vars": _redact(g("vars")) if g("vars") else None,
        "full_refresh": bool(g("full_refresh", False)), "dbt_cmd_flags": _jsonable(list(g("dbt_cmd_flags") or [])),
        "dbt_cmd_global_flags": _jsonable(list(g("dbt_cmd_global_flags") or [])), "env_names": sorted((g("env") or {}).keys()) if isinstance(g("env"), dict) else [],
        "install_deps": bool(g("install_deps", False)), "invocation_mode": getattr(inv, "value", inv), "dbt_executable_path": str(g("dbt_executable_path")) if g("dbt_executable_path") else None,
        "emit_datasets": bool(g("emit_datasets", False)), "warn_error": bool(g("warn_error", False)), "quiet": bool(g("quiet", False)), "fail_fast": bool(g("fail_fast", False)),
        "no_version_check": bool(g("no_version_check", False)), "indirect_selection": getattr(g("indirect_selection"), "value", g("indirect_selection")),
        "has_callback": bool(g("callback")), "py_requirements": _jsonable(list(g("py_requirements") or [])) or None, "image": g("image"), "container": _container_ir(task),
        "node": _cosmos_node(g("extra_context")),
    }


def _cosmos_groups(dag: Any) -> list[dict[str, Any]]:
    """Each DbtDag / DbtTaskGroup in the DAG with the Cosmos configuration it was built from and the dbt nodes it rendered."""
    graphs: list[tuple[str, Any]] = []
    if dag.__class__.__name__ == "DbtDag" and getattr(dag, "dbt_graph", None) is not None:
        graphs.append(("", dag.dbt_graph))
    for gid, tg in (getattr(dag, "task_group_dict", None) or {}).items():
        if tg.__class__.__name__ == "DbtTaskGroup" and getattr(tg, "dbt_graph", None) is not None:
            par = getattr(tg, "parent_group", None)
            if not (par is not None and par.__class__.__name__ == "DbtTaskGroup"):
                graphs.append((gid, tg.dbt_graph))
    out = []
    for gid, gr in graphs:
        rc, ec, pr = getattr(gr, "render_config", None), getattr(gr, "execution_config", None), getattr(gr, "project", None)
        enum = lambda v: getattr(v, "value", v)         # noqa: E731
        env = getattr(pr, "env_vars", None) or {}
        out.append({
            "group": gid, "cosmos_type": (getattr(gr, "airflow_metadata", None) or {}).get("cosmos_type"),
            "project_dir": str(getattr(gr, "project_path", None) or getattr(pr, "dbt_project_path", None) or "") or None,
            "project_name": getattr(pr, "project_name", None), "profile": _cosmos_profile(getattr(gr, "profile_config", None)),
            "render": {"select": list(getattr(rc, "select", []) or []), "exclude": list(getattr(rc, "exclude", []) or []), "selector": getattr(rc, "selector", None),
                       "test_behavior": enum(getattr(rc, "test_behavior", None)), "load_method": enum(getattr(rc, "load_method", None)),
                       "emit_datasets": bool(getattr(rc, "emit_datasets", False)), "source_rendering_behavior": enum(getattr(rc, "source_rendering_behavior", None)),
                       "seed_rendering_behavior": enum(getattr(rc, "seed_rendering_behavior", None)), "node_converters": bool(getattr(rc, "node_converters", None)),
                       "normalize_task_id": bool(getattr(rc, "normalize_task_id", None)), "group_nodes_by_folder": bool(getattr(rc, "group_nodes_by_folder", False))},
            "execution": {"mode": enum(getattr(ec, "execution_mode", None)), "invocation_mode": enum(getattr(ec, "invocation_mode", None)),
                          "dbt_executable_path": str(getattr(ec, "dbt_executable_path", "") or "") or None,
                          "test_indirect_selection": enum(getattr(ec, "test_indirect_selection", None))},
            "dbt_vars": _redact(getattr(gr, "dbt_vars", None) or {}), "env_names": sorted(env), "operator_args": _redact({k: v for k, v in (getattr(gr, "operator_args", None) or {}).items()
                                                                                                                      if not callable(v)}),
            "nodes": [{"unique_id": n.unique_id, "resource_type": enum(n.resource_type), "depends_on": list(n.depends_on or []), "tags": list(n.tags or [])}
                      for n in (getattr(gr, "filtered_nodes", None) or {}).values()],
        })
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
        "operator_class": (_class_info(type(task)) if not type(task).__module__.startswith(("airflow", "cosmos")) and hasattr(type(task), "execute") else None),     # Cosmos operators are mapped by cosmos_ops.py, not copied
        "trigger_rule": str(getattr(task.trigger_rule, "value", task.trigger_rule)),
        "owner": task.owner, "queue": task.queue if getattr(task, "queue", None) != "default" else None,
        "execution_timeout_s": task.execution_timeout.total_seconds() if getattr(task, "execution_timeout", None) else None,
        # Airflow 2 decorated mapped tasks are DecoratedMappedOperator, a SUBCLASS of MappedOperator: test the whole MRO
        "expand": _expand_ir(task),
        "is_mapped": any(c.__name__ == "MappedOperator" for c in type(task).__mro__) or bool(getattr(task, "is_mapped", False)),
        "loop": _loop_ir(task),
        "cosmos": _cosmos_task_ir(task),
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
        "outlet_assets": [{"uri": str(getattr(o, "uri", None) or ""), "name": str(getattr(o, "name", None) or "")} for o in (getattr(task, "outlets", None) or []) if getattr(o, "uri", None) and getattr(o, "name", None)],
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
        # constructor arguments that hold a function (response_check=, response_filter=, a SQL handler, ...): `args` leaves them out, so name them here
        # and the converter can refuse an operator whose behavior depends on one instead of silently dropping it
        t["callable_args"] = {n: f"{getattr(getattr(task, n), '__module__', '')}.{getattr(getattr(task, n), '__qualname__', type(getattr(task, n)).__name__)}"
                              for n in sorted(own) if getattr(task, n, None) is not None and callable(getattr(task, n, None))}
        # the source of those functions too (a sensor's `response_check`, an HTTP `response_filter`, ...): the converter refuses such an operator, but the rewrite route needs the logic
        t["callable_arg_info"] = {n: info for n in sorted(own) if getattr(task, n, None) is not None and callable(getattr(task, n, None))
                                  for info in [_callable_info(getattr(task, n))] if "site-packages" not in (info or {}).get("file", "")}      # the user's functions, not the library's defaults
    except (TypeError, ValueError):
        t["args"] = {}
        t["callable_args"] = {}
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
        "cosmos_groups": _cosmos_groups(dag),
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
