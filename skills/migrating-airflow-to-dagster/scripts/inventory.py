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
    ("BranchPython", "REDESIGN", "branching: model as conditional asset logic, asset checks or an op with dynamic outputs"),
    ("ShortCircuit", "REDESIGN", "short-circuit: model as a skip inside the asset or an automation condition"),
    ("BranchDateTime", "REDESIGN", "branching on time: schedule or conditional logic"),
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


class FileScan(ast.NodeVisitor):
    """Collect DAGs, tasks, edges and migration-relevant signals from one Python file."""

    def __init__(self, path: str, source: str) -> None:
        self.path = path
        self.dags: dict[str, dict[str, Any]] = {}       # dag var-or-id -> info
        self.tasks: list[dict[str, Any]] = []
        self.taskflow_funcs: dict[str, dict[str, Any]] = {}
        self.var_to_task: dict[str, str] = {}
        self.edges: list[tuple[str, str, str | None]] = []   # (up_task_id, down_task_id, dag_hint)
        self.signals: dict[str, int] = {}
        self.named_dicts: dict[str, ast.Dict] = {}
        self._dag_stack: list[str] = []
        self.source = source

    # -- helpers -------------------------------------------------------------------------------------------------
    def _sig(self, name: str) -> None:
        self.signals[name] = self.signals.get(name, 0) + 1

    def _current_dag(self) -> str | None:
        return self._dag_stack[-1] if self._dag_stack else None

    def _register_dag(self, key: str, call: ast.Call, line: int, dag_id: str | None = None) -> str:
        did = dag_id or _const(_kw(call, "dag_id")) or (_const(call.args[0]) if call.args else None) or key
        info = {"dag_id": str(did), "line": line, "schedule": parse_schedule(call),
                "catchup": _const(_kw(call, "catchup")), "max_active_runs": _const(_kw(call, "max_active_runs")),
                "tags": _const(_kw(call, "tags")), "has_default_args": _kw(call, "default_args") is not None,
                "default_args": {}, "kind": "dag"}
        da = _kw(call, "default_args")
        if isinstance(da, ast.Name):
            da = self.named_dicts.get(da.id)
        if isinstance(da, ast.Dict):
            for k, v in zip(da.keys, da.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    info["default_args"][k.value] = _const(v) if _const(v) is not None else _src(v)
        self.dags[key] = info
        return key

    # -- DAG discovery -------------------------------------------------------------------------------------------
    def visit_With(self, node: ast.With) -> None:
        pushed = 0
        for item in node.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Call) and _dotted(ctx.func).split(".")[-1] == "DAG":
                var = item.optional_vars.id if isinstance(item.optional_vars, ast.Name) else f"dag@{node.lineno}"
                self._dag_stack.append(self._register_dag(var, ctx, node.lineno))
                pushed += 1
            elif isinstance(ctx, ast.Call) and _dotted(ctx.func).split(".")[-1] == "TaskGroup":
                self._sig("task_group")
        self.generic_visit(node)
        for _ in range(pushed):
            self._dag_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        dag_deco = None
        task_deco = None
        for d in node.decorator_list:
            name = _dotted(d.func if isinstance(d, ast.Call) else d)
            if name in ("dag", "airflow.decorators.dag", "airflow.sdk.dag"):
                dag_deco = d
            elif name in TASK_DECORATORS or name.endswith(".task"):
                task_deco = (name, d)
        if dag_deco is not None:
            call = dag_deco if isinstance(dag_deco, ast.Call) else ast.Call(func=ast.Name("dag"), args=[], keywords=[])
            key = self._register_dag(node.name, call, node.lineno, dag_id=_const(_kw(call, "dag_id")) or node.name)
            self._dag_stack.append(key)
            self.generic_visit(node)
            self._dag_stack.pop()
            return
        if task_deco is not None:
            name, d = task_deco
            call = d if isinstance(d, ast.Call) else None
            task_id = (_const(_kw(call, "task_id")) if call else None) or node.name
            info = {"task_id": task_id, "line": node.lineno, "operator": f"@{name}", "style": TASK_DECORATORS.get(name, "python"),
                    "func": node.name, "pool": _const(_kw(call, "pool")) if call else None,
                    "retries": _const(_kw(call, "retries")) if call else None,
                    "trigger_rule": _const(_kw(call, "trigger_rule")) if call else None,
                    "mapped": False, "dag_hint": self._current_dag(), "var": None}
            self.taskflow_funcs[node.name] = info
            self.tasks.append(info)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    # -- tasks ---------------------------------------------------------------------------------------------------
    def _operator_task(self, call: ast.Call, var: str | None, line: int) -> None:
        cls = _dotted(call.func)
        last = cls.split(".")[-1]
        if last == "DAG":
            if var:
                self._register_dag(var, call, line)
            return
        if last in ("DbtDag", "DbtTaskGroup"):
            self._sig("cosmos")
            if last == "DbtDag":
                key = self._register_dag(var or f"cosmos@{line}", call, line)
                self.dags[key]["kind"] = "cosmos"
            return
        mapped = False
        chain = [call]
        # x = Operator.partial(...).expand(...): keyword arguments live on every call in the chain
        if isinstance(call.func, ast.Attribute) and call.func.attr in ("expand", "expand_kwargs", "partial"):
            mapped = True
            inner = call.func.value
            while isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr in ("partial", "expand", "expand_kwargs"):
                chain.append(inner)
                inner = inner.func.value
            if isinstance(inner, ast.Call):
                chain.append(inner)
                inner = inner.func
            cls = _dotted(inner)
            last = cls.split(".")[-1]
            self._sig("dynamic_task_mapping")

        def kw(name: str) -> ast.AST | None:
            for c in chain:
                v = _kw(c, name)
                if v is not None:
                    return v
            return None

        if not (last.endswith("Operator") or last.endswith("Sensor") or last.endswith("Trigger") or "Operator" in last):
            return
        task_id = _const(kw("task_id"))
        if task_id is None:
            return
        dag_var = kw("dag")
        info = {"task_id": task_id, "line": line, "operator": last, "style": "operator",
                "pool": _const(kw("pool")), "retries": _const(kw("retries")),
                "trigger_rule": _const(kw("trigger_rule")), "mapped": mapped,
                "dag_hint": (dag_var.id if isinstance(dag_var, ast.Name) else None) or self._current_dag(),
                "var": var, "outlets": _src(kw("outlets")) if kw("outlets") else "",
                "inlets": _src(kw("inlets")) if kw("inlets") else ""}
        if info["outlets"] or info["inlets"]:
            self._sig("dataset_lineage")
        self.tasks.append(info)
        if var:
            self.var_to_task[var] = task_id

    def visit_Assign(self, node: ast.Assign) -> None:
        var = node.targets[0].id if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None
        if var and isinstance(node.value, ast.Dict):
            self.named_dicts[var] = node.value
        if isinstance(node.value, ast.Call):
            self._operator_task(node.value, var, node.lineno)
            fn = _dotted(node.value.func)
            if fn in self.taskflow_funcs and var:
                self.var_to_task[var] = self.taskflow_funcs[fn]["task_id"]
                self._taskflow_edges(node.value)
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        v = node.value
        if isinstance(v, ast.Call):
            self._operator_task(v, None, node.lineno)
            fn = _dotted(v.func)
            if fn in self.taskflow_funcs:
                self._taskflow_edges(v)
            last = fn.split(".")[-1]
            if last in ("set_downstream", "set_upstream") and isinstance(v.func, ast.Attribute) and v.args:
                a, b = self._names(v.func.value), self._names(v.args[0])
                self._link(a, b) if last == "set_downstream" else self._link(b, a)
            if fn.split(".")[-1] == "chain" or fn == "chain":
                groups = [self._names(a) for a in v.args]
                for x, y in zip(groups, groups[1:]):
                    self._link(x, y)
        elif isinstance(v, ast.BinOp):
            self._shift(v)
        self.generic_visit(node)

    def _taskflow_edges(self, call: ast.Call) -> None:
        down = self.taskflow_funcs.get(_dotted(call.func))
        if not down:
            return
        for a in [*call.args, *[k.value for k in call.keywords]]:
            for up in self._names(a):
                self.edges.append((up, down["task_id"], self._current_dag()))

    def _names(self, node: ast.AST) -> list[str]:
        if isinstance(node, ast.Name):
            return [self.var_to_task.get(node.id, node.id)]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [n for e in node.elts for n in self._names(e)]
        if isinstance(node, ast.Call):
            fn = _dotted(node.func)
            if fn in self.taskflow_funcs:
                return [self.taskflow_funcs[fn]["task_id"]]
        if isinstance(node, ast.BinOp):
            return self._names(node.right if isinstance(node.op, ast.RShift) else node.left)
        return []

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

    # -- signals -------------------------------------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
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
        d_tasks = [t for t in fs.tasks if (t["dag_hint"] == key) or (t["dag_hint"] is None and single == key)
                   or (t["dag_hint"] is not None and t["dag_hint"] not in fs.dags and single == key)]
        ids = {t["task_id"] for t in d_tasks}
        d_edges = sorted({(u, v) for u, v, _ in fs.edges if u in ids and v in ids})
        out.append({"kind": d["kind"], "dag_id": d["dag_id"], "file": rel, "line": d["line"], "schedule": d["schedule"],
                    "catchup": d["catchup"], "max_active_runs": d["max_active_runs"], "tags": d["tags"],
                    "default_args": d["default_args"], "edges": [list(e) for e in d_edges],
                    "signals": fs.signals, "task_ids": sorted(ids)})
        for t in d_tasks:
            out.append({"kind": "task", "dag_id": d["dag_id"], "task_id": t["task_id"], "file": rel, "line": t["line"],
                        "operator": t["operator"], "style": t["style"], "pool": t["pool"], "retries": t["retries"],
                        "trigger_rule": t["trigger_rule"], "mapped": t["mapped"],
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
        if unit["mapped"]:
            klass = "REDESIGN"
            extra.append("dynamic task mapping: dynamic partitions or a multi-asset/graph")
        if unit["trigger_rule"] and unit["trigger_rule"] != "all_success":
            klass = "JUDG" if klass == "MECH" else klass
            extra.append(f"trigger_rule={unit['trigger_rule']}: no direct equivalent; model with checks, hooks or automation conditions")
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
    units = []
    for n, u in enumerate(raw, 1):
        klass, why = suggest(u)
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
    args.out.write_text(json.dumps(m, indent=2) + "\n")
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
