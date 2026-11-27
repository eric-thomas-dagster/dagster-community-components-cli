"""Shared helpers for the evaluation harness (`eval_*.py`): where things live in a work directory, reading the corpus manifest, the DAG's IR and spec, local dependencies of a DAG file.

Layout of a work directory (everything outside the repository):
    <work>/repos/<repo>/                 shallow clones of the public repositories
    <work>/irs/<repo>.<2|3>.json         IR per repository and Airflow major
    <work>/conv/<repo>.<2|3>/            the converter's output (and `.ra`: converted with --return-all, for value comparison)
    <work>/corpus/manifest.json          the corpus table
    <work>/<run>/<dag>/                  what a COLD WORKER gets, one folder per DAG (kit)
    <work>/<run>-private/                specs with converter data, results, behaviour verdicts: workers never get this
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))


def safe(n: str) -> str:
    s = re.sub(r"\W", "_", n)
    return f"_{s}" if s[:1].isdigit() else s


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def row_for(manifest: dict[str, Any], key: str) -> dict[str, Any]:
    for r in manifest["dags"]:
        if r["key"] == key:
            return r
    raise KeyError(key)


def dir_names(entries: list[dict[str, Any]]) -> dict[str, str]:
    """key -> folder name: the dag id, with the repository added when two sampled DAGs share an id."""
    ids = [e["dag_id"] for e in entries]
    return {e["key"]: (e["dag_id"] if ids.count(e["dag_id"]) == 1 else f"{e['dag_id']}__{e['repo']}") for e in entries}


def pick_ir_dag(dags: list[dict[str, Any]], dag_id: str, file: str | None = None) -> dict[str, Any] | None:
    """The IR entry for a dag id. Two files can define the same dag id (an `exercises/` copy and its `solutions/` copy): the one whose `file` is the manifest row's wins, so the DAG measured in Airflow is the DAG converted and the one whose spec is shown."""
    same = [d for d in dags if d["dag_id"] == dag_id]
    for d in same:
        if file and d.get("file") == file:
            return d
    return same[0] if same else None


def dedupe_ir(ir: dict[str, Any], files: dict[str, str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """The IR with one entry per dag id (the entry in `files[dag_id]`, else the first), plus the dag ids that had several. Unchanged (same object) when no id repeats."""
    ids = [d["dag_id"] for d in ir.get("dags", [])]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if not dup:
        return ir, []
    keep = {i: pick_ir_dag(ir["dags"], i, (files or {}).get(i)) for i in dup}
    return {**ir, "dags": [d for d in ir["dags"] if d["dag_id"] not in keep or d is keep[d["dag_id"]]]}, dup


def ir_dag(work: Path, row: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    ir = json.loads((work / "irs" / f"{row['repo']}.{row['airflow']}.json").read_text())
    d = pick_ir_dag(ir["dags"], row["dag_id"], row.get("file"))
    if d is None:
        raise KeyError(row["key"])
    return d, ir.get("airflow_version")


def scrub(text: str, work: Path) -> str:
    """Environment paths (the venv interpreter an ExternalPythonOperator names, this machine's work directory and home) are not facts about the DAG."""
    text = re.sub(re.escape(str(work)) + r"/venvs/[\w.-]+/bin/python", "<the DAG's Python environment>", text)
    return text.replace(str(work), "<work>").replace(os.path.expanduser("~"), "~")


def make_spec(work: Path, row: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The facts-only spec for one DAG with NO converter data and NO Airflow baseline (what a cold worker may see)."""
    import dag_spec
    d, ver = ir_dag(work, row)
    spec = dag_spec.build(d, None, None, None, ver)
    text = scrub(json.dumps(spec, sort_keys=True, default=str), work)
    return json.loads(text), scrub(dag_spec.render_md(spec), work)


def dag_file(work: Path, manifest: dict[str, Any], row: dict[str, Any]) -> Path:
    root = work / "repos" / row["repo"]
    for folder in manifest["repos"][row["repo"]]["dag_folders"]:
        p = root / folder / row["file"]
        if p.exists():
            return p
    p = root / row["file"]
    if p.exists():
        return p
    raise FileNotFoundError(f"{row['key']}: {row['file']}")


def local_deps(root: Path, dag_path: Path) -> list[Path]:
    """Repository files the DAG file needs: modules it imports (resolved against the DAG's folder, parents up to the repo root, and the root) and data files named by a string literal, transitively."""
    root_given = root
    root, dag_path = root.resolve(), dag_path.resolve()                        # a work directory's `repos` is often a symlink: `resolve()` below must not take a file out from under the root
    seen: dict[Path, None] = {}
    todo = [dag_path]
    bases = [dag_path.parent, *[p for p in dag_path.parents if root in p.parents or p == root]]
    while todo:
        f = todo.pop()
        try:
            tree = ast.parse(f.read_text())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level:
                base = f.parent
                for _ in range(node.level - 1):
                    base = base.parent
                rel = (node.module or "").replace(".", "/")
                cands = [base / rel] if rel else [base]
                cands += [base / rel / a.name for a in node.names] if rel else [base / a.name for a in node.names]
                for c in cands:
                    for p in (c.with_suffix(".py"), c / "__init__.py"):
                        if p.exists() and p not in seen and p != dag_path:
                            seen[p] = None
                            todo.append(p)
                continue
            for m in mods:
                rel = m.replace(".", "/")
                for b in bases:
                    for p in (b / f"{rel}.py", b / rel / "__init__.py"):
                        if p.exists() and p not in seen and p != dag_path and root in p.parents:
                            seen[p] = None
                            todo.append(p)
                            for init in p.parents:
                                if init == root:
                                    break
                                ip = init / "__init__.py"
                                if ip.exists() and ip not in seen:
                                    seen[ip] = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and 3 < len(node.value) < 200 and re.search(r"\.(csv|json|ya?ml|txt|sql|tsv|parquet|md|xml|html|jinja|j2|sh)$", node.value) and "{" not in node.value:
                for b in bases:
                    p = (b / node.value.lstrip("/")).resolve()
                    if p.is_file() and root in p.parents and p.stat().st_size < 1_000_000 and ".git" not in p.parts and p not in seen and p != dag_path:
                        seen[p] = None
                        break
    return sorted(root_given / p.relative_to(root) for p in seen)                    # in terms of the root the caller gave


def copy_tree_file(root: Path, f: Path, dest_root: Path) -> Path:
    dest = dest_root / f.relative_to(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(f, dest)
    return dest


def python_for(a: Any, major: str) -> str:
    return (a.venv_2 if major == "2" else a.venv_3)


def run_py(cmd: list[str], timeout: int, env: dict[str, str] | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})}, cwd=cwd)
