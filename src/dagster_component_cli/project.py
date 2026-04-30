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
    """Decide where to install a component.

    Priority:
      1. Explicit `--target-dir` overrides everything.
      2. If project_root is found, use `<root>/components/<category-dir>/<id>/`.
         (`components/` is a stable, predictable location regardless of how
          the user has organized their `defs/` tree.)
      3. Otherwise, install relative to cwd at `./components/<category-dir>/<id>/`.
    """
    if target_dir:
        return Path(target_dir).resolve()

    component_id = component["id"]
    category = component.get("category", "unknown")
    category_dir = CATEGORY_DIRS.get(category, category)

    base = (project_root or Path.cwd()).resolve()
    return base / "components" / category_dir / component_id
