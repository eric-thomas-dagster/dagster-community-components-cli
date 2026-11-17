#!/usr/bin/env python3
"""Static, read-only inventory of an Airflow project -> migration manifest (JSON).

Never imports or runs DAG code: it parses Python with `ast` (and dag-factory YAML with
PyYAML if installed). Every DAG and task becomes a unit with a *suggested* classification
(MECH / JUDG / REDESIGN) that the migrating agent must confirm. The scanner enumerates;
the agent decides.

    python3 inventory.py <airflow_repo> --out manifest.json
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "site-packages", "build", "dist"}

CRON_PRESETS = {
    "@hourly": "0 * * * *", "@daily": "0 0 * * *", "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
}

# operator/decorator -> (classification, note). First match wins; order matters.
OPERATOR_RULES: list[tuple[str, str, str]] = [
    ("Branch", "REDESIGN", "branching: model as conditional asset logic, asset checks or an op with dynamic outputs"),
    ("ShortCircuit", "REDESIGN", "short-circuit: model as a skip inside the asset or an automation condition"),
    ("SubDag", "REDESIGN", "SubDAG: flatten into the parent job/asset graph"),
    ("TriggerDagRun", "JUDG", "cross-DAG trigger: usually an asset dependency or automation condition"),
    ("ExternalTaskSensor", "JUDG", "cross-DAG wait: usually an asset dependency"),
    ("Sensor", "JUDG", "sensor: Dagster sensor or declarative automation (eager/on_cron); check the registry for the service"),
    ("DbtCloud", "JUDG", "dbt Cloud: dagster-dbt cloud component (state-backed)"),
    ("Dbt", "MECH", "dbt via Cosmos: dagster-dbt (@dbt_assets / DbtProjectComponent)"),
    ("KubernetesPod", "JUDG", "pod execution: dagster-k8s Pipes client or run launcher"),
    ("Bash", "MECH", "shell command: asset running the command (Pipes subprocess or component)"),
    ("Empty", "MECH", "no-op: becomes a dependency join only, usually no asset"),
    ("Dummy", "MECH", "no-op: becomes a dependency join only, usually no asset"),
    ("Python", "MECH", "python callable: asset or op running the same function"),
    ("Sql", "JUDG", "SQL operator: check registry/official integration for the warehouse"),
    ("Snowflake", "JUDG", "provider operator: official dagster-snowflake or a registry component"),
    ("BigQuery", "JUDG", "provider operator: official dagster-gcp or a registry component"),
    ("S3", "JUDG", "provider operator: dagster-aws or a registry component"),
    ("Spark", "JUDG", "provider operator: dagster-spark/databricks or a registry component"),
    ("Databricks", "JUDG", "provider operator: dagster-databricks"),
    ("Email", "JUDG", "notification: alerts or a notification component"),
]
OPERATOR_FALLBACK = ("JUDG", "no mapping row for this operator: record the finding, then search the registry (`dagster-component search --json`) before hand-writing")

TASK_DECORATORS = {"task": "python", "task.python": "python", "task.bash": "bash", "task.branch": "branch",
                   "task.short_circuit": "short_circuit", "task.sensor": "sensor", "task.virtualenv": "python",
                   "task.docker": "docker", "task.kubernetes": "kubernetes", "task.external_python": "python"}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _src(node: ast.AST | None) -> str:
    return ast.unparse(node) if node is not None else ""


def _const(node: ast.AST | None) -> Any:
    try:
        return ast.literal_eval(node) if node is not None else None
    except Exception:
        return None


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def classify_operator(cls: str) -> tuple[str, str]:
    for needle, klass, note in OPERATOR_RULES:
        if needle.lower() in cls.lower():
            return klass, note
    return OPERATOR_FALLBACK


def parse_schedule(call: ast.Call) -> dict[str, Any]:
    node = _kw(call, "schedule")
    key = "schedule"
    if node is None:
        node, key = _kw(call, "schedule_interval"), "schedule_interval"
    if node is None:
        node, key = _kw(call, "timetable"), "timetable"
    if node is None:
        return {"kind": "unspecified", "raw": ""}
    val = _const(node)
    if key == "timetable":
        return {"kind": "timetable", "raw": _src(node)}
    if val is None and isinstance(node, ast.Constant):
        return {"kind": "manual", "raw": "None"}
    if isinstance(val, str):
        if val in CRON_PRESETS:
            return {"kind": "cron", "raw": val, "cron": CRON_PRESETS[val]}
        if val == "@once":
            return {"kind": "once", "raw": val}
        if val == "@continuous":
            return {"kind": "continuous", "raw": val}
        return {"kind": "cron", "raw": val, "cron": val}
    if isinstance(node, ast.List):
        return {"kind": "dataset", "raw": _src(node)}
    if isinstance(node, ast.Call) and _dotted(node.func).split(".")[-1] in ("timedelta", "relativedelta"):
        return {"kind": "interval", "raw": _src(node)}
    if isinstance(node, ast.Call) and _dotted(node.func).split(".")[-1] in ("Dataset", "Asset"):
        return {"kind": "dataset", "raw": _src(node)}
    return {"kind": "expression", "raw": _src(node)}


WRAP_ATTRS = {"override", "expand", "partial", "expand_kwargs", "loop"}
TASK_LIKE_DECOS = {"setup", "teardown"}


def _unwrap(call: ast.Call) -> tuple[ast.AST, list[ast.Call], list[str]]:
    """Peel `X.override(...)(...)`, `X.partial(...).expand(...)` into (base node, every call in the chain, attrs)."""
    calls, attrs, node = [call], [], call.func
    while True:
        if isinstance(node, ast.Attribute) and node.attr in WRAP_ATTRS:
            attrs.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            calls.append(node)
            node = node.func
        else:
            break
    return node, calls, attrs


class FileScan(ast.NodeVisitor):
    """Collect DAGs, tasks, edges and migration-relevant signals from one Python file.

    Tasks are created where Airflow creates them: at the call site. TaskGroup contexts prefix task ids
    (`group.task`); repeated calls of one @task or group get Airflow's `__1`, `__2` suffixes.
    """

    def __init__(self, path: str, source: str) -> None:
        self.path = path
        self.source = source
        self.dags: dict[str, dict[str, Any]] = {}
        self.tasks: list[dict[str, Any]] = []
        self.taskflow: dict[str, dict[str, Any]] = {}      # @task function name -> info
        self.groupfuncs: dict[str, ast.FunctionDef] = {}   # @task_group function name -> node
        self.var_to_tasks: dict[str, list[str]] = {}
        self.edges: list[tuple[str, str, str | None]] = []
        self.signals: dict[str, int] = {}
        self.named_dicts: dict[str, ast.Dict] = {}
        self.dag_vars: dict[str, str] = {}              # variable name -> latest unique DAG key
        self._loop_depth = 0
        self._mapped_group = 0
        self._dag_stack: list[str] = []
        self._group_stack: list[str] = []
        self._task_body = 0
        self._seen: set[int] = set()
        self._call_ids: dict[int, list[str]] = {}
        self._counts: dict[tuple[str | None, str], int] = {}

    # -- helpers -------------------------------------------------------------------------------------------------
    def _sig(self, name: str) -> None:
        self.signals[name] = self.signals.get(name, 0) + 1

    def _current_dag(self) -> str | None:
        return self._dag_stack[-1] if self._dag_stack else None

    def _prefix(self) -> str:
        return ".".join(g for g in self._group_stack if g)

    def _unique(self, dag: str | None, tid: str) -> str:
        n = self._counts.get((dag, tid), 0)
        self._counts[(dag, tid)] = n + 1
        return tid if n == 0 else f"{tid}__{n}"

    def _register_dag(self, key: str, call: ast.Call, line: int, dag_id: str | None = None) -> str:
        node = _kw(call, "dag_id")
        did, dynamic = dag_id or _const(node), False
        if did is None and node is None and call.args:
            did = _const(call.args[0])
        if did is None and node is not None:
            if "__file__" in _src(node):
                did = Path(self.path).stem          # the common `dag_id=Path(__file__).stem` idiom
            else:
                did, dynamic = _src(node), True
        info = {"dag_id": str(did or key), "line": line, "schedule": parse_schedule(call), "dynamic_dag_id": dynamic,
                "catchup": _const(_kw(call, "catchup")), "max_active_runs": _const(_kw(call, "max_active_runs")),
                "tags": _const(_kw(call, "tags")), "default_args": {}, "kind": "dag"}
        da = _kw(call, "default_args")
        if isinstance(da, ast.Name):
            da = self.named_dicts.get(da.id)
        if isinstance(da, ast.Dict):
            for k, v in zip(da.keys, da.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    info["default_args"][k.value] = _const(v) if _const(v) is not None else _src(v)
        if _kw(call, "outlets") is not None or _kw(call, "inlets") is not None:
            info["dag_level_outlets"] = True   # outlets/inlets are task arguments; Airflow's DAG rejects them
            self._sig("dag_level_outlets")
        ukey = f"{key}@{line}"
        self.dag_vars[key] = ukey
        self.dags[ukey] = info
        return ukey

    def _add_task(self, **t: Any) -> None:
        t.setdefault("pool", None); t.setdefault("retries", None); t.setdefault("trigger_rule", None)
        t.setdefault("mapped", False); t.setdefault("dynamic_id", False); t.setdefault("implicit_xcom", False)
        t.setdefault("outlets", ""); t.setdefault("inlets", "")
        t["in_loop"] = self._loop_depth > 0
        self.tasks.append(t)

    # -- DAG and group discovery ---------------------------------------------------------------------------------
    def visit_With(self, node: ast.With) -> None:
        dags = groups = 0
        for item in node.items:
            ctx = item.context_expr
            if not isinstance(ctx, ast.Call):
                continue
            last = _dotted(ctx.func).split(".")[-1]
            if last == "DAG":
                var = item.optional_vars.id if isinstance(item.optional_vars, ast.Name) else f"dag@{node.lineno}"
                self._dag_stack.append(self._register_dag(var, ctx, node.lineno))
                dags += 1
            elif last == "TaskGroup":
                self._sig("task_group")
                gid = _const(_kw(ctx, "group_id")) or (_const(ctx.args[0]) if ctx.args else None) or _src(_kw(ctx, "group_id"))
                no_prefix = _const(_kw(ctx, "prefix_group_id")) is False
                self._group_stack.append("" if no_prefix else self._unique(self._current_dag(), f"group:{gid}").replace("group:", "", 1))
                groups += 1
        self.generic_visit(node)
        for _ in range(dags):
            self._dag_stack.pop()
        for _ in range(groups):
            self._group_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind, deco_call = None, None
        for d in node.decorator_list:
            name = _dotted(d.func if isinstance(d, ast.Call) else d)
            last = name.split(".")[-1]
            call = d if isinstance(d, ast.Call) else None
            if name in ("dag", "airflow.decorators.dag", "airflow.sdk.dag"):
                kind, deco_call = "dag", call or ast.Call(func=ast.Name("dag"), args=[], keywords=[])
            elif last == "task_group":
                kind, deco_call = "group", call
            elif last == "asset" and "airflow" in self.source:
                kind, deco_call = "asset", call or ast.Call(func=ast.Name("asset"), args=[], keywords=[])
            elif name in TASK_DECORATORS or name.endswith(".task") or last in TASK_LIKE_DECOS:
                kind, deco_call = f"task:{name}", call
        if kind == "dag":
            key = self._register_dag(node.name, deco_call, node.lineno, dag_id=_const(_kw(deco_call, "dag_id")) or node.name)
            self._dag_stack.append(key)
            self.generic_visit(node)
            self._dag_stack.pop()
        elif kind == "group":
            self.groupfuncs[node.name] = node          # expanded at each call site, like Airflow
            self._group_id = _const(_kw(deco_call, "group_id")) if deco_call else None
            node._gid = self._group_id or node.name    # type: ignore[attr-defined]
        elif kind == "asset":
            did = _const(_kw(deco_call, "name")) or node.name
            key = self._register_dag(f"asset:{node.name}", deco_call, node.lineno, dag_id=did)
            self._add_task(task_id=did, line=node.lineno, operator="@asset", style="asset", dag_hint=key, var=None)
        elif kind and kind.startswith("task:"):
            name = kind[5:]
            self.taskflow[node.name] = {"base_id": (_const(_kw(deco_call, "task_id")) if deco_call else None) or node.name,
                                        "operator": f"@{name}", "style": TASK_DECORATORS.get(name, "python"), "call": deco_call}
            self._task_body += 1
            self.generic_visit(node)                   # signals only: calls inside a task body are not tasks
            self._task_body -= 1
        else:
            self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    # -- task instantiation --------------------------------------------------------------------------------------
    def _instantiate(self, call: ast.Call) -> list[str]:
        if id(call) in self._seen:
            return self._call_ids.get(id(call), [])
        self._seen.add(id(call))
        ids = self._make(call)
        self._call_ids[id(call)] = ids
        return ids

    def _make(self, call: ast.Call) -> list[str]:
        if self._task_body:
            return []
        base, chain, attrs = _unwrap(call)
        for c in chain:
            self._seen.add(id(c))
        name = _dotted(base)
        last = name.split(".")[-1]

        tf0 = self.taskflow.get(name)
        if tf0 is not None and tf0["call"] is not None:
            chain = [*chain, tf0["call"]]   # decorator keywords (retries, pool, outlets, ...) apply to every instance

        def kw(n: str) -> ast.AST | None:
            for c in chain:
                v = _kw(c, n)
                if v is not None:
                    return v
            return None

        if name in self.groupfuncs and set(attrs) <= WRAP_ATTRS:
            fn = self.groupfuncs[name]
            gid = self._unique(self._current_dag(), f"group:{fn._gid}").replace("group:", "", 1)  # type: ignore[attr-defined]
            mapped_group = any(a in ("expand", "expand_kwargs") for a in attrs)
            looped = "loop" in attrs
            if looped:
                self._sig("task_loop")     # Airflow 3.x group.loop(...): iteration count is decided at run time
            body_ids = {id(n) for stmt in fn.body for n in ast.walk(stmt)}   # the same AST is expanded once per call
            self._seen -= body_ids
            for k in body_ids:
                self._call_ids.pop(k, None)
            self._group_stack.append(gid)
            self._mapped_group += mapped_group
            self._loop_depth += looped
            for stmt in fn.body:
                self.visit(stmt)
            self._loop_depth -= looped
            self._mapped_group -= mapped_group
            self._group_stack.pop()
            return []

        tf = self.taskflow.get(name)
        dag_var = kw("dag")
        dag_key = self.dag_vars.get(dag_var.id, dag_var.id) if isinstance(dag_var, ast.Name) else self._current_dag()
        if tf is not None:
            if dag_key is None:
                return []
            node, base_id, operator, style = kw("task_id"), None, tf["operator"], tf["style"]
            base_id = _const(node) if node is not None else tf["base_id"]
            dynamic = node is not None and base_id is None
            base_id = _src(node) if dynamic else base_id
        elif kw("task_id") is not None and (self._current_dag() or dag_var is not None or last.endswith(("Operator", "Sensor", "Trigger", "Marker"))):
            node = kw("task_id")
            base_id, dynamic = _const(node), False
            if base_id is None:
                base_id, dynamic = _src(node), True
            operator, style = last, "operator"
        else:
            return []
        if last in ("DbtDag", "DbtTaskGroup"):
            self._sig("cosmos")
            return []
        mapped = any(a in ("expand", "expand_kwargs", "partial") for a in attrs) or self._mapped_group > 0
        if mapped:
            self._sig("dynamic_task_mapping")
        tid = self._unique(dag_key, f"{self._prefix()}.{base_id}" if self._prefix() else str(base_id))
        implicit = False
        for c in chain:
            for a in [*c.args, *[k.value for k in c.keywords]]:
                ups = self._arg_ids(a)
                for u in ups:
                    self.edges.append((u, tid, dag_key))
                if ups and tf is not None:
                    implicit = True
        outlets, inlets = kw("outlets"), kw("inlets")
        if outlets is not None or inlets is not None:
            self._sig("dataset_lineage")
        self._add_task(task_id=tid, line=call.lineno, operator=operator, style=style, pool=_const(kw("pool")),
                       retries=_const(kw("retries")), trigger_rule=_const(kw("trigger_rule")), mapped=mapped,
                       dynamic_id=dynamic, implicit_xcom=implicit, dag_hint=dag_key, var=None,
                       outlets=_src(outlets) if outlets is not None else "", inlets=_src(inlets) if inlets is not None else "")
        return [tid]

    def _arg_ids(self, node: ast.AST) -> list[str]:
        """Task ids that an argument expression depends on (variables, nested task calls, .output, subscripts)."""
        if isinstance(node, ast.Name):
            return list(self.var_to_tasks.get(node.id, []))
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return [i for e in node.elts for i in self._arg_ids(e)]
        if isinstance(node, ast.Dict):
            return [i for v in node.values for i in self._arg_ids(v)]
        if isinstance(node, ast.Starred):
            return self._arg_ids(node.value)
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            return self._arg_ids(node.value)
        if isinstance(node, ast.Call):
            ids = self._instantiate(node)
            return ids
        return []

    # -- statements ----------------------------------------------------------------------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        var = node.targets[0].id if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None
        v = node.value
        if var and isinstance(v, ast.Dict):
            self.named_dicts[var] = v
        ids: list[str] = []
        if isinstance(v, ast.Call) and _dotted(v.func).split(".")[-1] in ("DAG", "DbtDag"):
            self._seen.add(id(v))
            if _dotted(v.func).endswith("DbtDag"):
                self._sig("cosmos")
            key = self._register_dag(var or f"dag@{node.lineno}", v, node.lineno)
            if _dotted(v.func).endswith("DbtDag"):
                self.dags[key]["kind"] = "cosmos"
        elif isinstance(v, ast.Call):
            ids = self._instantiate(v)
        elif isinstance(v, (ast.List, ast.Tuple)):
            ids = [i for e in v.elts if isinstance(e, ast.Call) for i in self._instantiate(e)]
        elif isinstance(v, (ast.ListComp, ast.GeneratorExp, ast.SetComp)) and isinstance(v.elt, ast.Call):
            ids = self._instantiate(v.elt)
        if var and ids:
            self.var_to_tasks[var] = ids
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        v = node.value
        if isinstance(v, ast.Call):
            self._instantiate(v)
            fn = _dotted(v.func)
            last = fn.split(".")[-1]
            if last in ("set_downstream", "set_upstream") and isinstance(v.func, ast.Attribute) and v.args:
                a, b = self._names(v.func.value), self._names(v.args[0])
                self._link(a, b) if last == "set_downstream" else self._link(b, a)
            if last == "chain":
                groups = [self._names(a) for a in v.args]
                for x, y in zip(groups, groups[1:]):
                    self._link(x, y)
        elif isinstance(v, ast.BinOp):
            self._shift(v)
        self.generic_visit(node)

    def _names(self, node: ast.AST) -> list[str]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("as_setup", "as_teardown"):
            return self._names(node.func.value)
        if isinstance(node, ast.BinOp):
            return self._names(node.right if isinstance(node.op, ast.RShift) else node.left)
        return self._arg_ids(node)

    def _link(self, ups: list[str], downs: list[str]) -> None:
        for u in ups:
            for d in downs:
                self.edges.append((u, d, self._current_dag()))

    def _shift(self, node: ast.BinOp) -> list[str]:
        if not isinstance(node.op, (ast.RShift, ast.LShift)):
            return []
        left = self._shift(node.left) if isinstance(node.left, ast.BinOp) and isinstance(node.left.op, (ast.RShift, ast.LShift)) else self._names(node.left)
        right = self._names(node.right)
        if isinstance(node.op, ast.RShift):
            self._link(left, right)
            return right
        self._link(right, left)
        return right

    def _loop(self, node: ast.AST) -> None:
        self._loop_depth += 1
        self.generic_visit(node)
        self._loop_depth -= 1

    visit_For = visit_While = visit_ListComp = visit_SetComp = visit_GeneratorExp = visit_DictComp = _loop  # type: ignore[assignment]

    # -- signals and stray task calls ----------------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        if id(node) not in self._seen:
            self._instantiate(node)
        fn = _dotted(node.func)
        last = fn.split(".")[-1]
        if fn.endswith("Variable.get") or fn.endswith("Variable.set") or fn == "Variable.get":
            self._sig("airflow_variable")
        if last in ("get_connection", "get_hook"):
            self._sig("airflow_connection")
        if last in ("xcom_pull", "xcom_push"):
            self._sig("xcom")
        if last in ("Dataset", "Asset") and node.args and isinstance(_const(node.args[0]), str):
            self._sig("dataset_or_asset")
        if _kw(node, "conn_id") is not None or any(k.arg and k.arg.endswith("_conn_id") for k in node.keywords):
            self._sig("connection_id")
        if _kw(node, "depends_on_past") is not None and _const(_kw(node, "depends_on_past")):
            self._sig("depends_on_past")
        if _kw(node, "trigger_rule") is not None:
            self._sig("trigger_rule")
        if _kw(node, "sla") is not None:
            self._sig("sla")
        for cb in ("on_failure_callback", "on_success_callback", "on_retry_callback", "sla_miss_callback"):
            if _kw(node, cb) is not None:
                self._sig("callback")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "output" and isinstance(node.value, ast.Name):
            self._sig("xcom")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and "{{" in node.value and "}}" in node.value:
            self._sig("jinja_template")
        if isinstance(node.value, str) and "{{" in node.value and "macros." in node.value:
            self._sig("airflow_macros")


def scan_python(path: Path, rel: str) -> list[dict[str, Any]]:
    source = path.read_text(errors="replace")
    if "airflow" not in source.lower() and "DbtDag" not in source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [{"kind": "unparsed", "file": rel, "line": e.lineno or 0, "reason": f"syntax error: {e.msg}"}]
    fs = FileScan(rel, source)
    fs.visit(tree)
    if not fs.dags and not fs.tasks:
        return []

    single = next(iter(fs.dags)) if len(fs.dags) == 1 else None
    out: list[dict[str, Any]] = []
    for key, d in fs.dags.items():
        d_tasks = [t for t in fs.tasks if t["dag_hint"] == key or (single == key and (t["dag_hint"] is None or t["dag_hint"] not in fs.dags))]
        ids = {t["task_id"] for t in d_tasks}
        d_edges = sorted({(u, v) for u, v, _ in fs.edges if u in ids and v in ids})
        out.append({"kind": d["kind"], "dag_id": d["dag_id"], "file": rel, "line": d["line"], "schedule": d["schedule"],
                    "catchup": d["catchup"], "max_active_runs": d["max_active_runs"], "tags": d["tags"],
                    "default_args": d["default_args"], "edges": [list(e) for e in d_edges], "dynamic_dag_id": d["dynamic_dag_id"],
                    "signals": fs.signals, "task_ids": sorted(ids)})
        for t in d_tasks:
            out.append({"kind": "task", "dag_id": d["dag_id"], "task_id": t["task_id"], "file": rel, "line": t["line"],
                        "operator": t["operator"], "style": t["style"], "pool": t["pool"], "retries": t["retries"],
                        "trigger_rule": t["trigger_rule"], "mapped": t["mapped"], "dynamic_id": t.get("dynamic_id", False), "in_loop": t.get("in_loop", False),
                        "implicit_xcom": t.get("implicit_xcom", False),
                        "upstream": sorted({u for u, v in d_edges if v == t["task_id"]}),
                        "outlets": t.get("outlets", ""), "inlets": t.get("inlets", ""), "signals": fs.signals})
    return out


def scan_dag_factory(path: Path, rel: str) -> list[dict[str, Any]]:
    try:
        import yaml
    except ImportError:
        return []
    try:
        data = yaml.safe_load(path.read_text())
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out: list[dict[str, Any]] = []
    for dag_id, cfg in data.items():
        if not (isinstance(cfg, dict) and isinstance(cfg.get("tasks"), dict)):
            continue
        if not any(isinstance(t, dict) and "operator" in t for t in cfg["tasks"].values()):
            continue
        sched = cfg.get("schedule") or cfg.get("schedule_interval")
        out.append({"kind": "dag_factory", "dag_id": str(dag_id), "file": rel, "line": 1,
                    "schedule": {"kind": "cron" if isinstance(sched, str) else "unspecified", "raw": str(sched),
                                 "cron": CRON_PRESETS.get(sched, sched) if isinstance(sched, str) else None},
                    "catchup": cfg.get("catchup"), "max_active_runs": cfg.get("max_active_runs"), "tags": cfg.get("tags"),
                    "default_args": cfg.get("default_args") or {}, "edges": [], "signals": {}, "task_ids": sorted(cfg["tasks"])})
        for tid, t in cfg["tasks"].items():
            out.append({"kind": "task", "dag_id": str(dag_id), "task_id": str(tid), "file": rel, "line": 1,
                        "operator": str(t.get("operator", "")).split(".")[-1], "style": "dag_factory",
                        "pool": t.get("pool"), "retries": t.get("retries"), "trigger_rule": t.get("trigger_rule"),
                        "mapped": False, "upstream": list(t.get("dependencies") or []), "outlets": "", "inlets": "", "signals": {}})
    return out


def suggest(unit: dict[str, Any]) -> tuple[str, str]:
    """Suggested classification and reason for one unit."""
    if unit["kind"] == "unparsed":
        return "JUDG", unit["reason"]
    if unit["kind"] == "task":
        klass, note = ("MECH", "TaskFlow task: asset or op running the same function") if unit["operator"].startswith("@") \
            else classify_operator(unit["operator"])
        if unit["operator"].startswith("@task.branch") or unit["operator"].startswith("@task.short"):
            klass, note = "REDESIGN", "branching/short-circuit: redesign"
        if unit["operator"] == "@task.sensor":
            klass, note = "JUDG", "sensor task: Dagster sensor or declarative automation"
        extra = []
        if unit.get("dynamic_id"):
            klass = "JUDG" if klass == "MECH" else klass
            extra.append("task_id is computed at parse time (factory or loop): one source task may become many tasks; confirm the expansion against a parsed DAG")
        if unit.get("in_loop") and not unit.get("dynamic_id"):
            klass = "JUDG" if klass == "MECH" else klass
            extra.append("created inside a loop or comprehension: the number of copies is only known when Airflow parses the file")
        if unit["mapped"]:
            klass = "REDESIGN"
            extra.append("dynamic task mapping: dynamic partitions or a multi-asset/graph")
        if unit["trigger_rule"] and unit["trigger_rule"] != "all_success":
            klass = "JUDG" if klass == "MECH" else klass
            extra.append(f"trigger_rule={unit['trigger_rule']}: no direct equivalent; model with checks, hooks or automation conditions")
        if unit.get("implicit_xcom") and klass == "MECH":
            klass = "JUDG"
            extra.append("receives an upstream task's return value (implicit XCom): decide per edge between fusing, explicit storage or an IO manager")
        if unit["signals"].get("xcom") and klass == "MECH":
            klass = "JUDG"
            extra.append("XCom in this file: decide per edge between fusing, explicit storage or an IO manager")
        return klass, "; ".join([note, *extra])
    # dag-level
    reasons, klass = [], "MECH"
    sk = unit["schedule"]["kind"]
    if sk in ("timetable", "expression", "continuous"):
        klass = "JUDG"
        reasons.append(f"schedule {unit['schedule']['raw']!r} needs a custom schedule or sensor")
    if sk == "dataset":
        klass = "JUDG"
        reasons.append("dataset-triggered schedule: asset dependencies and an automation condition")
    if sk == "interval":
        klass = "JUDG"
        reasons.append("timedelta schedule: not a cron; use a custom schedule or fixed cron approximation")
    if unit["catchup"] is True:
        klass = "JUDG"
        reasons.append("catchup=True: partitions plus backfill, not a plain schedule")
    if unit["signals"].get("depends_on_past"):
        klass = "JUDG"
        reasons.append("depends_on_past: partition dependencies or a custom condition")
    if unit["signals"].get("sla"):
        klass = "JUDG"
        reasons.append("SLA: freshness policy or alert")
    if unit["signals"].get("airflow_variable") or unit["signals"].get("airflow_connection") or unit["signals"].get("connection_id"):
        reasons.append("Airflow Variables/Connections: map to env vars, secrets and resources (Phase 5)")
    if unit["signals"].get("dag_level_outlets"):
        klass = "JUDG"
        reasons.append("outlets/inlets on the DAG itself: Airflow rejects this (they are task arguments); confirm which task produces the dataset")
    if unit["kind"] == "cosmos":
        klass = "JUDG"
        reasons.append("Cosmos DbtDag: dagster-dbt")
    return klass, "; ".join(reasons) or "plain DAG: job plus schedule"


def build_manifest(root: Path, exclude: set[str]) -> dict[str, Any]:
    raw: list[dict[str, Any]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS or part in exclude for part in p.relative_to(root).parts):
            continue
        rel = str(p.relative_to(root))
        if p.suffix == ".py":
            raw.extend(scan_python(p, rel))
        elif p.suffix in (".yml", ".yaml"):
            raw.extend(scan_dag_factory(p, rel))
    by_dag: dict[str, dict[str, int]] = {}
    for u in raw:
        if u["kind"] == "task":
            k, _ = suggest(u)
            by_dag.setdefault(u["dag_id"], {}).setdefault(k, 0)
            by_dag[u["dag_id"]][k] += 1
    units = []
    for n, u in enumerate(raw, 1):
        klass, why = suggest(u)
        if u["kind"] in ("dag", "cosmos", "dag_factory"):
            counts = by_dag.get(u["dag_id"], {})
            u["task_classes"] = counts
            if counts.get("REDESIGN") or counts.get("JUDG"):
                klass = "JUDG" if klass == "MECH" else klass
                why = "; ".join(filter(None, [why if why != "plain DAG: job plus schedule" else "", "tasks: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))]))
        units.append({"id": f"A{n:03d}", **u, "classification": klass, "classification_reason": why,
                      "status": "pending", "evidence": "", "reason": "", "target": {}})
    return {"version": 1, "root": str(root), "units": units}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", type=Path)
    ap.add_argument("--out", type=Path, default=Path("manifest.json"))
    ap.add_argument("--exclude", action="append", default=[], help="directory name to skip (repeatable)")
    args = ap.parse_args(argv)
    if not args.repo.is_dir():
        print(f"not a directory: {args.repo}", file=sys.stderr)
        return 2
    m = build_manifest(args.repo, set(args.exclude))
    args.out.write_text(json.dumps(m, indent=2, default=str) + "\n")
    counts: dict[str, int] = {}
    for u in m["units"]:
        counts[u["classification"]] = counts.get(u["classification"], 0) + 1
    dags = sum(1 for u in m["units"] if u["kind"] in ("dag", "cosmos", "dag_factory"))
    tasks = sum(1 for u in m["units"] if u["kind"] == "task")
    print(f"wrote {args.out}: {len(m['units'])} units ({dags} DAGs, {tasks} tasks); " +
          ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
