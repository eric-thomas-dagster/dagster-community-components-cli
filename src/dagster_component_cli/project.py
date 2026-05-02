"""Project detection and component install-path resolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


# Component category → directory name in the user's project.
CATEGORY_DIRS: dict[str, str] = {
    "resource": "resources",
    "io_manager": "io_managers",
    "sensor": "sensors",
    "observation": "observations",
    "external": "external_assets",
    "integration": "integrations",
    "check": "asset_checks",
    "transformation": "assets/transforms",
    "ingestion": "assets/ingestion",
    "ai": "assets/ai",
    "analytics": "assets/analytics",
    "infrastructure": "assets/infrastructure",
    "source": "assets/sources",
    "sink": "assets/sinks",
    "dbt": "assets/dbt",
}


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from `start` looking for a Dagster project marker.

    Returns the directory containing `pyproject.toml`, `dg.toml`, or a `defs/`
    folder. Returns None if no project root is found.
    """
    cwd = (start or Path.cwd()).resolve()
    for parent in (cwd, *cwd.parents):
        if any((parent / m).exists() for m in ("dg.toml", "pyproject.toml")):
            return parent
        if (parent / "defs").is_dir():
            return parent
    return None


def detect_canonical_layout(project_root: Path) -> Optional[str]:
    """If project_root looks like a `create-dagster` project, return the package name.

    Recognizes the layout produced by `uvx create-dagster project ...`:

        <root>/
        ├── pyproject.toml      # contains [tool.dg.project] root_module = "<pkg>"
        └── src/
            └── <pkg>/
                └── defs/

    Returns the package name (so callers can build `<root>/src/<pkg>/defs/<id>/`),
    or None if the layout doesn't match.
    """
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        return None

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None

    try:
        data = tomllib.loads(pyproject.read_text())
    except (OSError, ValueError):
        return None

    pkg = (
        data.get("tool", {})
            .get("dg", {})
            .get("project", {})
            .get("root_module")
    )
    if not pkg:
        return None
    if not (project_root / "src" / pkg / "defs").is_dir():
        return None
    return pkg


def installed_components(project_root: Path) -> list[dict]:
    """Scan a project for components installed by this CLI.

    Looks for any directory containing both `component.py` and a marker file
    `.dg-community.json` written at install time.
    """
    out: list[dict] = []
    if not project_root.exists():
        return out
    for marker in project_root.rglob(".dg-community.json"):
        try:
            data = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data["_path"] = str(marker.parent.relative_to(project_root))
        out.append(data)
    return out


def resolve_install_dir(
    project_root: Optional[Path],
    component: dict,
    *,
    target_dir: Optional[str] = None,
) -> Path:
    """Decide where the component CLASS files (component.py, schema.json,
    README.md, requirements.txt, marker) should land.

    Priority:
      1. Explicit `--target-dir` overrides everything.
      2. Canonical `create-dagster` layout — install class files into
         `<root>/src/<pkg>/components/<id>/` (the `[tool.dg.project]
         registry_modules` location). The instance YAML lives separately
         at `<root>/src/<pkg>/defs/<id>/defs.yaml` so `dg` autoloads it.
      3. Other project — install to `<root>/components/<category-dir>/<id>/`.
      4. No project root — install relative to cwd at the same path.
    """
    if target_dir:
        return Path(target_dir).resolve()

    component_id = component["id"]
    base = (project_root or Path.cwd()).resolve()

    if project_root is not None:
        pkg = detect_canonical_layout(project_root)
        if pkg:
            return base / "src" / pkg / "components" / component_id

    category = component.get("category", "unknown")
    category_dir = CATEGORY_DIRS.get(category, category)
    return base / "components" / category_dir / component_id


def resolve_defs_dir(project_root: Path, pkg: str, component_id: str) -> Path:
    """Where the instance defs.yaml should land in canonical layout."""
    return project_root.resolve() / "src" / pkg / "defs" / component_id
