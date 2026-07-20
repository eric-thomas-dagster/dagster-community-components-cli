"""Automation-analyzer worker script.

Runs inside the target Dagster project's venv (via `uv run --project <dir>`).
Introspects the project's Definitions object and dumps a JSON payload the
outer analyzer parses.

Kept intentionally standalone (no imports from dagster_component_cli itself)
so it can be shipped-across / executed in an environment that only has the
customer's Dagster install.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any


def _find_definitions(project_dir: str = ".") -> Any:
    """Find the project's Definitions object.

    Modern (`create-dagster`) layout: the project's package has a
    `definitions.py` with a `@definitions`-decorated factory named `defs`.

    Older / custom projects: try common entry points.
    """
    from dagster import Definitions, load_from_defs_folder  # noqa: F401
    import importlib
    from pathlib import Path

    # ── Path 1: PROJECT_ROOT/src/<pkg>/definitions.py — canonical create-dagster shape
    cwd = Path(project_dir).resolve()
    src_dir = cwd / "src"
    if src_dir.is_dir():
        for pkg_dir in src_dir.iterdir():
            if not pkg_dir.is_dir():
                continue
            def_file = pkg_dir / "definitions.py"
            if def_file.exists():
                # Ensure src/ is on sys.path
                sys.path.insert(0, str(src_dir))
                mod = importlib.import_module(f"{pkg_dir.name}.definitions")
                # Try common attribute names.
                for attr in ("defs", "definitions"):
                    obj = getattr(mod, attr, None)
                    if obj is None:
                        continue
                    if callable(obj):
                        # @definitions-decorated factory — call it to materialize.
                        obj = obj()
                    if isinstance(obj, Definitions):
                        return obj
                # Fallback: scan for any Definitions attribute
                for attr in dir(mod):
                    val = getattr(mod, attr)
                    if isinstance(val, Definitions):
                        return val

    # ── Path 2: PROJECT_ROOT/definitions.py directly
    def_file = cwd / "definitions.py"
    if def_file.exists():
        sys.path.insert(0, str(cwd))
        mod = importlib.import_module("definitions")
        for attr in ("defs", "definitions"):
            obj = getattr(mod, attr, None)
            if obj is None:
                continue
            if callable(obj):
                obj = obj()
            if isinstance(obj, Definitions):
                return obj

    raise RuntimeError(
        f"Could not find Definitions object. Looked at src/*/definitions.py "
        f"and ./definitions.py under {cwd}. Provide a project with a canonical "
        f"create-dagster layout (definitions.py with `defs` = @definitions factory)."
    )


def _serialize_asset_selection(sel, resolved_all_assets: list) -> list[str]:
    """Best-effort resolution of an AssetSelection to concrete asset keys.

    Uses `.resolve()` if available; otherwise falls back to str().
    """
    if sel is None:
        return []
    try:
        keys = sel.resolve(resolved_all_assets)
        return [k.to_user_string() for k in keys]
    except Exception:
        try:
            return [str(sel)]
        except Exception:
            return []


def introspect(project_dir: str = ".") -> dict:
    defs = _find_definitions(project_dir)

    # ── Assets ──────────────────────────────────────────────────────────
    assets = []
    all_specs = list(defs.resolve_all_asset_specs())
    for spec in all_specs:
        try:
            key_str = spec.key.to_user_string()
            deps = [dep.asset_key.to_user_string() for dep in (spec.deps or [])]
            has_condition = spec.automation_condition is not None
            assets.append({
                "key": key_str,
                "group": spec.group_name or "",
                "tags": dict(spec.tags or {}),
                "deps": deps,
                "kinds": list(spec.kinds or []),
                "has_automation_condition": has_condition,
                "automation_condition_class": type(spec.automation_condition).__name__ if has_condition else None,
            })
        except Exception as e:
            assets.append({"key": "<unresolved>", "_error": str(e)})

    # ── Schedules ───────────────────────────────────────────────────────
    schedules = []
    try:
        # dagster.Definitions exposes `.schedules` as a Sequence
        schedules_iter = getattr(defs, "schedules", None) or []
        for s in schedules_iter:
            job_name = getattr(s, "job_name", None) or ""
            cron = getattr(s, "cron_schedule", None) or ""
            status = getattr(s, "default_status", None)
            schedules.append({
                "name": s.name,
                "cron": cron,
                "job_name": job_name,
                "default_status": str(status) if status else None,
            })
    except Exception as e:
        schedules.append({"_error": f"schedules introspection failed: {e}"})

    # ── Jobs ────────────────────────────────────────────────────────────
    jobs = []
    try:
        jobs_iter = getattr(defs, "jobs", None) or []
        # Resolve once — reuse for all jobs
        try:
            ag = defs.resolve_asset_graph()
        except Exception:
            ag = None

        for j in jobs_iter:
            name = j.name
            # UnresolvedAssetJobDefinition uses `.selection`; AssetJobDefinition
            # uses `.asset_selection`. Handle both.
            sel = getattr(j, "selection", None) or getattr(j, "asset_selection", None)
            resolved_keys = []
            if sel and ag is not None:
                try:
                    resolved = sel.resolve(ag)
                    resolved_keys = [k.to_user_string() for k in resolved]
                except Exception:
                    pass
            jobs.append({
                "name": name,
                "asset_selection_str": str(sel) if sel else None,
                "asset_keys": resolved_keys,
            })
    except Exception as e:
        jobs.append({"_error": f"jobs introspection failed: {e}"})

    # ── Sensors ─────────────────────────────────────────────────────────
    sensors = []
    try:
        sensors_iter = getattr(defs, "sensors", None) or []
        for s in sensors_iter:
            job_names = []
            single_job = getattr(s, "job_name", None)
            if single_job:
                job_names.append(single_job)
            multi_jobs = getattr(s, "job_names", None) or []
            job_names.extend(multi_jobs)
            sensors.append({
                "name": s.name,
                "job_names": list(set(job_names)),
                "asset_selection_str": str(getattr(s, "asset_selection", None) or ""),
            })
    except Exception as e:
        sensors.append({"_error": f"sensors introspection failed: {e}"})

    return {
        "assets": assets,
        "schedules": schedules,
        "jobs": jobs,
        "sensors": sensors,
    }


if __name__ == "__main__":
    project_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    try:
        payload = introspect(project_dir)
        sys.stdout.write(json.dumps(payload, indent=2))
    except Exception as e:
        err = {"error": str(e), "traceback": traceback.format_exc()}
        sys.stdout.write(json.dumps(err, indent=2))
        sys.exit(1)
