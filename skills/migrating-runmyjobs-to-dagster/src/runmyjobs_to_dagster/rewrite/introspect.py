"""Load a Dagster project's definitions in a subprocess and report what they contain, as JSON: assets (key, parents, pool, retries, kinds), asset checks, jobs (with the
assets each selects), schedules (cron, time zone, job), sensors. Shared by the acceptance gates and the equivalence check, so both routes are read the same way.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from .spec import norm_command

SCRIPT = r'''
import json, sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
import dagster as dg
pkg = sys.argv[1]
defs = dg.load_from_defs_folder(path_within_project=Path("src") / pkg)
g = defs.resolve_asset_graph()
pools, retries = {}, {}
for a in defs.assets or []:
    if not isinstance(a, dg.AssetsDefinition):
        continue
    ops = list(a.node_def.iterate_op_defs()) if hasattr(a.node_def, "iterate_op_defs") else [a.node_def]
    pool = sorted({o.pool for o in ops if getattr(o, "pool", None)})
    rp = [o.retry_policy for o in ops if getattr(o, "retry_policy", None)]
    for k in a.keys:
        pools[k.to_user_string()] = pool
        retries[k.to_user_string()] = max((r.max_retries for r in rp), default=0)
assets = []
for k in sorted(g.get_all_asset_keys(), key=lambda x: x.to_user_string()):
    n = g.get(k)
    s = k.to_user_string()
    assets.append({"key": s, "parents": sorted(p.to_user_string() for p in n.parent_keys), "pool": pools.get(s, []), "retries": retries.get(s, 0),
                   "kinds": sorted(n.kinds or []), "group": n.group_name, "executable": bool(n.is_executable)})
jobs = []
for j in defs.jobs or []:
    name = j.name
    try:
        rj = defs.resolve_job_def(name)
        keys = sorted(x.to_user_string() for x in rj.asset_layer.selected_asset_keys)
    except Exception as e:
        keys = ["<could not resolve: %s>" % type(e).__name__]
    jobs.append({"name": name, "assets": keys})
scheds = [{"name": s.name, "cron": getattr(s, "cron_schedule", None), "timezone": getattr(s, "execution_timezone", None), "job": s.job_name,
           "status": str(getattr(s, "default_status", ""))} for s in defs.schedules or []]
def _jn(s):
    try:
        return s.job_name
    except Exception:
        return None
sens = [{"name": s.name, "status": str(getattr(s, "default_status", "")), "job": _jn(s)} for s in defs.sensors or []]
checks = []
for c in defs.asset_checks or []:
    for ck in getattr(c, "check_keys", []):
        checks.append({"asset": ck.asset_key.to_user_string(), "name": ck.name})
for a in defs.assets or []:
    for ck in getattr(a, "check_keys", []) or []:
        checks.append({"asset": ck.asset_key.to_user_string(), "name": ck.name})
print("RMJ_INTROSPECT=" + json.dumps({"assets": assets, "jobs": jobs, "schedules": scheds, "sensors": sens, "checks": checks}))
'''


def package_of(project: Path) -> str:
    src = project / "src"
    for p in sorted(src.iterdir()) if src.is_dir() else []:
        if (p / "defs").is_dir():
            return p.name
    raise FileNotFoundError(f"no src/<package>/defs in {project}")


def env_for(project: Path, extra: dict | None = None) -> dict:
    e = dict(os.environ)
    e["PYTHONPATH"] = os.pathsep.join([str(project / "src"), str(project), e.get("PYTHONPATH", "")]).strip(os.pathsep)
    e.update(extra or {})
    return e


def introspect(project: Path, python: str = sys.executable, pkg: str | None = None) -> tuple[dict | None, str]:
    pkg = pkg or package_of(project)
    p = subprocess.run([python, "-c", SCRIPT, pkg], cwd=project, capture_output=True, text=True, env=env_for(project))
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("RMJ_INTROSPECT=")), None)
    if p.returncode != 0 or line is None:
        err = [ln for ln in (p.stderr or p.stdout).splitlines() if ln.strip() and "Warning" not in ln and "warnings.warn" not in ln and "class " not in ln[:7]]
        return None, "\n".join(err[-12:])[-1500:]
    return json.loads(line[len("RMJ_INTROSPECT="):]), ""


def dg_command(python: str) -> list[str] | None:
    cand = Path(python).parent / "dg"
    if cand.exists():
        return [str(cand)]
    import shutil
    w = shutil.which("dg")
    return [w] if w else None


# ---------------------------------------------------------------- source corpus


def _str_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name):
                out.append("{" + v.value.id + "}")
            else:
                out.append("{?}")
        return "".join(out)
    return None


def _yaml_strings(x: Any):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from _yaml_strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _yaml_strings(v)


def corpus(files: list[Path]) -> list[str]:
    """Normalised text of everything a set of source files says: each file's raw text, every string literal in Python, every string value in YAML."""
    out: list[str] = []
    for f in files:
        try:
            text = f.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        out.append(norm_command(text))
        if f.suffix == ".py":
            try:
                for node in ast.walk(ast.parse(text)):
                    s = _str_of(node)
                    if s:
                        out.append(norm_command(s))
            except SyntaxError:
                pass
        elif f.suffix in (".yaml", ".yml"):
            try:
                for doc in yaml.safe_load_all(text):
                    out.extend(norm_command(s) for s in _yaml_strings(doc))
            except yaml.YAMLError:
                pass
    return [c for c in out if c]


def command_present(cmd: dict, texts: list[str]) -> bool:
    forms = [f for f in (cmd.get("normalized"), cmd.get("rendered")) if f]
    return any(f in t for f in forms for t in texts)


def unit_files(project: Path, pkg: str, unit_id: str) -> list[Path]:
    root = project / "src" / pkg / "defs"
    files: list[Path] = []
    d = root / unit_id
    if d.is_dir():
        files += [p for p in sorted(d.rglob("*")) if p.is_file() and p.suffix in (".py", ".yaml", ".yml") and "__pycache__" not in p.parts]
    f = root / f"{unit_id}.py"
    if f.exists():
        files.append(f)
    return files


# ---------------------------------------------------------------- header docstring accounting

VERBS = r"(replaced by|folded into|merged into|dropped|reimplemented)"


def docstrings(files: list[Path]) -> str:
    out = []
    for f in files:
        if f.suffix == ".py":
            try:
                d = ast.get_docstring(ast.parse(f.read_text()))
            except SyntaxError:
                d = None
            if d:
                out.append(d)
    return "\n".join(out)


def parse_accounting(doc: str, ids: list[str]) -> dict[str, dict]:
    """For each object id: the verb and the asset keys (backticked) on the header line that accounts for it. A missing id is absent from the result."""
    res: dict[str, dict] = {}
    lines = doc.splitlines()
    for oid in ids:
        pat = re.compile(r"^\s*[-*]?\s*`?" + re.escape(oid) + r"`?\s*(?:\([^)]*\))?\s*[:\-]\s*(.*)$")
        for i, ln in enumerate(lines):
            m = pat.match(ln)
            if not m:
                continue
            rest = m.group(1)
            j = i + 1
            while j < len(lines) and lines[j].startswith((" ", "\t")) and lines[j].strip() and not pat.match(lines[j]):      # a wrapped continuation line
                rest += " " + lines[j].strip()
                j += 1
            v = re.search(VERBS, rest)
            if not v:
                continue
            assets = re.findall(r"`([^`]+)`", rest[v.end():])
            res[oid] = {"verb": v.group(1), "assets": assets, "text": rest, "reimplemented": "reimplemented" in rest}
            break
    return res
