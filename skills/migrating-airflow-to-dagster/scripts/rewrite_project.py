"""How the gates and the harness look at a REWRITE PROJECT: through the project's own definitions loader, not through one module's variables.

A rewrite is a Dagster project made by `create-dagster`. Its definitions are whatever `src/<pkg>/definitions.py` loads (`load_from_defs_folder`), which covers both shapes a worker may choose:

* **module style**: `src/<pkg>/defs/<name>.py` with `@dg.asset` functions as module variables;
* **component style**: a folder under `src/<pkg>/defs/<id>/` with a `defs.yaml` that names a registry (or official) component, optionally with sibling `.py` files for the job and the schedule.

Both are accepted on equal terms: the gates find assets, asset checks, the job and the schedule by KEY or NAME in the loaded `Definitions` object, never by variable name or module path. The header
accounting (one line per Airflow task) may be the module docstring of any `.py` under `defs/`, or the `#` comment block of a `defs.yaml`.

Everything here is pure file reading except `load_defs` (imports the project) and `defs_summary` (runs `load_defs` in a subprocess so the gate process stays clean).
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

YAML_NAMES = ("defs.yaml", "defs.yml")


def package_of(project: Path, hint: str | None = None) -> str | None:
    """The project's Python package under `src/` (the one with a `defs/` folder). `hint` (for instance from a kit index) is preferred when it matches, case-insensitively: `uv` and `create-dagster` normalise the
    package name (`rw_load_file_to_S3` becomes `rw_load_file_to_s3`), so a name computed from a DAG id is not reliable; the folder on disk is."""
    src = Path(project) / "src"
    if not src.is_dir():
        return None
    cands = [p.name for p in sorted(src.iterdir()) if p.is_dir() and (p / "defs").is_dir() and not p.name.endswith(".egg-info")]
    if hint:
        for c in cands:
            if c == hint:
                return c
        for c in cands:
            if c.lower() == hint.lower():
                return c
    return cands[0] if cands else None


def defs_root(project: Path, pkg: str | None = None) -> Path | None:
    pkg = pkg or package_of(project)
    return Path(project) / "src" / pkg / "defs" if pkg else None


def py_files(project: Path, pkg: str | None = None) -> list[Path]:
    """every Python module under defs/ except `__init__.py` (a module-style rewrite, or the job/schedule file next to a component)."""
    root = defs_root(project, pkg)
    return sorted(p for p in root.rglob("*.py") if p.name != "__init__.py" and "__pycache__" not in p.parts) if root and root.exists() else []


def yaml_files(project: Path, pkg: str | None = None) -> list[Path]:
    root = defs_root(project, pkg)
    return sorted(p for p in root.rglob("*") if p.name in YAML_NAMES and p.is_file()) if root and root.exists() else []


def component_dirs(project: Path, pkg: str | None = None) -> list[Path]:
    """folders under defs/ that hold a defs.yaml (component instances)."""
    return sorted({p.parent for p in yaml_files(project, pkg)})


def yaml_header(text: str) -> str:
    """The `#` comment lines of a `defs.yaml` as plain text (one leading space removed): the header accounting a component folder may carry. The `# yaml-language-server:` modeline is not header text."""
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#") and not s.startswith("# yaml-language-server"):
            out.append(s[1:][1:] if s[1:2] == " " else s[1:])
    return "\n".join(out)


def doc_of_source(path: str | Path, src: str) -> str:
    """the header text of one file: a Python module's docstring, or a defs.yaml's comment block."""
    if str(path).endswith((".yaml", ".yml")):
        return yaml_header(src)
    try:
        return ast.get_docstring(ast.parse(src)) or ""
    except SyntaxError:
        return ""


def sources(project: Path, pkg: str | None = None) -> dict[str, str]:
    """{path: text} of every file that may hold the header: Python modules and defs.yaml files under defs/."""
    return {str(p): p.read_text() for p in py_files(project, pkg) + yaml_files(project, pkg)}


def header_text(project: Path, pkg: str | None = None) -> str:
    """The header accounting of the whole project: every module docstring and defs.yaml comment block under defs/, joined. A worker may put the accounting in one file or split it."""
    return "\n\n".join(d for d in (doc_of_source(p, s) for p, s in sources(project, pkg).items()) if d)


def definitions_module(project: Path, pkg: str) -> str:
    """The module `dg` loads: `[tool.dg.project] code_location_target_module` or `[tool.dagster] module_name` of pyproject.toml, else `<pkg>.definitions`."""
    f = Path(project) / "pyproject.toml"
    if f.exists():
        text = f.read_text()
        for key in ("code_location_target_module", "module_name"):
            m = re.search(rf'^\s*{key}\s*=\s*"([\w.]+)"', text, re.M)
            if m:
                return m.group(1)
    return f"{pkg}.definitions"


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def component_usage(project: Path, pkg: str | None = None) -> dict[str, Any]:
    """Which components the project's defs.yaml files use: `registry` (ids of components installed from the community registry: the `.dg-community.json` marker `dagster-component add` writes next to the vendored source,
    or a `dagster_community_components.<Class>` type from the package), `other` (official integrations and anything else) and `types` (every `type:` seen)."""
    pkg = pkg or package_of(project)
    registry: list[str] = []
    other: list[str] = []
    types: list[str] = []
    for y in yaml_files(project, pkg):
        for t in re.findall(r"^type:\s*([\w.]+)\s*$", y.read_text(), re.M):
            types.append(t)
            rid = None
            m = re.search(r"\.components\.(\w+)\.", t)
            if m:
                for marker in Path(project).glob(f"src/*/components/{m.group(1)}/.dg-community.json"):
                    try:
                        rid = json.loads(marker.read_text()).get("id") or m.group(1)
                    except ValueError:
                        rid = m.group(1)
            if rid is None and t.startswith("dagster_community_components."):
                rid = snake(t.rsplit(".", 1)[-1]).removesuffix("_component")
            (registry if rid else other).append(rid or t)
    return {"registry": sorted(set(registry)), "other": sorted(set(other)), "types": types, "component_folders": len(component_dirs(project, pkg))}


# ---- loading the project's definitions ---------------------------------------------------------------------------------------------------------
def load_defs(definitions_module: str, fallback_module: str | None = None):
    """The project's Definitions the way `dg` loads them: import its definitions module (`@definitions def defs()` / `defs = ...`), which for a create-dagster project is `load_from_defs_folder` over `defs/`
    (module-style assets and component folders alike). Only a project WITHOUT a definitions module falls back to the module-level objects of `fallback_module`.
    This function's source is copied into every generated test, so it must stay self-contained."""
    import importlib

    import dagster as dg
    try:
        m = importlib.import_module(definitions_module)
    except ModuleNotFoundError as e:
        if fallback_module is None or e.name not in (definitions_module, definitions_module.rsplit(".", 1)[0], definitions_module.split(".")[0]):
            raise
        m = None
    if m is not None:
        d = getattr(m, "defs", None)
        if d is None:
            d = next((v for v in vars(m).values() if isinstance(v, dg.Definitions)), None)
        if d is not None and not isinstance(d, dg.Definitions) and callable(d):
            d = d()
        if d is None and hasattr(m, "definitions"):
            d = m.definitions()
        if isinstance(d, dg.Definitions):
            return d
    mod = importlib.import_module(fallback_module or definitions_module)
    vals = list(vars(mod).values())
    return dg.Definitions(assets=[v for v in vals if isinstance(v, dg.AssetsDefinition) and not isinstance(v, dg.AssetChecksDefinition)], asset_checks=[v for v in vals if isinstance(v, dg.AssetChecksDefinition)],
                          jobs=[v for v in vals if type(v).__name__ in ("UnresolvedAssetJobDefinition", "JobDefinition")], schedules=[v for v in vals if isinstance(v, dg.ScheduleDefinition)],
                          sensors=[v for v in vals if isinstance(v, dg.SensorDefinition)])


LOAD_DEFS_SOURCE = inspect.getsource(load_defs)

SUMMARY_CODE = '''
import json, sys, warnings
warnings.filterwarnings("ignore")
sys.path[:0] = {paths!r}
import dagster as dg
{loader}
defs = load_defs({defs_module!r}, {fallback!r})
out = {{"assets": {{}}, "checks": [], "jobs": [], "schedules": [], "sensors": []}}
try:
    graph = defs.resolve_asset_graph()
    keys = [k.to_user_string() for k in graph.get_all_asset_keys()]
    out["checks"] = sorted(f"{{k.asset_key.to_user_string()}}:{{k.name}}" for k in graph.asset_check_keys)
except Exception:
    keys = []
    for a in defs.assets or []:
        if isinstance(a, dg.AssetsDefinition):
            keys += [k.to_user_string() for k in a.keys]
            if hasattr(a, "check_keys"):
                out["checks"] += [f"{{k.asset_key.to_user_string()}}:{{k.name}}" for k in a.check_keys]
        elif hasattr(a, "key"):
            keys.append(a.key.to_user_string())
node = {{}}
for a in defs.assets or []:
    if isinstance(a, dg.AssetsDefinition):
        for k in a.keys:
            try:
                node[k.to_user_string()] = a.node_def.name
            except Exception:
                pass
out["assets"] = {{k: node.get(k) for k in sorted(set(keys))}}
out["jobs"] = sorted(j.name for j in (defs.jobs or []))
out["schedules"] = [{{"name": s.name, "cron": s.cron_schedule if isinstance(s.cron_schedule, str) else list(s.cron_schedule), "timezone": s.execution_timezone or "UTC"}} for s in (defs.schedules or []) if isinstance(s, dg.ScheduleDefinition)]
out["sensors"] = sorted(s.name for s in (defs.sensors or []))
print("SUMMARY:" + json.dumps(out))
'''


def defs_summary(project: Path, pkg: str | None = None, python: str | None = None, timeout: int = 180) -> dict[str, Any]:
    """{assets: {key: node name}, checks: ['asset:check'], jobs: [name], schedules: [{name, cron, timezone}], sensors: [name]} of the LOADED definitions, from a subprocess (so a failing import is reported, not raised).
    On failure: {'error': '...'}."""
    project = Path(project).resolve()
    pkg = pkg or package_of(project)
    if not pkg:
        return {"error": "no package with a defs/ folder under src/"}
    code = SUMMARY_CODE.format(paths=[str(project / "src"), str(project)], loader=LOAD_DEFS_SOURCE, defs_module=definitions_module(project, pkg), fallback=None)
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    try:
        r = subprocess.run([python or sys.executable, "-W", "ignore", "-c", code], cwd=project, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"loading the definitions timed out after {timeout}s"}
    for ln in r.stdout.splitlines():
        if ln.startswith("SUMMARY:"):
            return json.loads(ln[len("SUMMARY:"):])
    return {"error": (r.stderr or r.stdout).strip()[-500:] or "the definitions could not be loaded"}


def dg_binary(project: Path | None = None) -> str | None:
    """The `dg` to use: the project's own virtualenv first (it matches the project, so no VIRTUAL_ENV warning), then the one beside this interpreter, then PATH."""
    import shutil
    cands = []
    if project:
        cands.append(Path(project) / ".venv" / "bin" / "dg")
    cands.append(Path(sys.executable).parent / "dg")
    for c in cands:
        if c.exists():
            return str(c)
    return shutil.which("dg")


def clean_env(project: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for running `dg`, pytest or python against the project: an outer virtualenv is not 'active' (`VIRTUAL_ENV` unset, so `dg` does not warn about a mismatch), the project's source is importable."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["PYTHONPATH"] = f"{Path(project) / 'src'}:{project}"
    env.update(extra or {})
    return env


def dg_list_defs(project: Path) -> dict[str, Any] | None:
    """`dg list defs --json` of the project (asset keys, checks, jobs, schedules), or None when `dg` is unavailable or fails."""
    dg_bin = dg_binary(project)
    if not dg_bin:
        return None
    r = subprocess.run([dg_bin, "list", "defs", "--json"], cwd=project, capture_output=True, text=True, env=clean_env(project), timeout=180)
    if r.returncode != 0:
        return None
    start = r.stdout.find("{")
    try:
        return json.loads(r.stdout[start:]) if start >= 0 else None
    except ValueError:
        return None
