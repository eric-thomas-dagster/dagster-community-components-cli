"""Component installation — fetch files, write to disk, install requirements."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .registry import fetch_file


# Files that may be present in a component directory in the registry.
# We attempt each — missing files are skipped silently.
COMPONENT_FILES = (
    "component.py",
    "io_manager.py",        # split-architecture IO managers
    "__init__.py",          # split-architecture re-exports
    "README.md",
    "schema.json",
    "example.yaml",
    "requirements.txt",
)


class InstallError(Exception):
    pass


def file_url_for(component: dict, filename: str) -> str:
    """Build the raw-content URL for a file inside a component directory.

    The manifest has `<file>_url` for component.py, README.md, etc. — but we
    can't rely on every file being listed. Instead, derive the URL from the
    `component_url` (which we know exists) by swapping the trailing filename.
    """
    base = component.get("component_url")
    if not base:
        raise InstallError(f"Component '{component.get('id')}' has no component_url in manifest")
    # base ends in `.../component.py` — strip it, append the requested filename
    prefix = base.rsplit("/", 1)[0]
    return f"{prefix}/{filename}"


def fetch_component_files(component: dict) -> dict[str, bytes]:
    """Download every known file for a component. Returns {filename: bytes}."""
    out: dict[str, bytes] = {}
    for filename in COMPONENT_FILES:
        url = file_url_for(component, filename)
        try:
            out[filename] = fetch_file(url)
        except Exception:
            # File doesn't exist for this component — skip
            continue
    if "component.py" not in out:
        raise InstallError(
            f"Could not fetch component.py for '{component['id']}' — registry may be stale"
        )
    return out


def write_files(target_dir: Path, files: dict[str, bytes], *, force: bool = False) -> list[Path]:
    """Write fetched files into target_dir. Returns list of paths written."""
    if target_dir.exists() and any(target_dir.iterdir()) and not force:
        raise InstallError(
            f"Target directory is not empty: {target_dir}\n"
            f"Use --force to overwrite, or --target-dir to choose a different location."
        )
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name, content in files.items():
        path = target_dir / name
        path.write_bytes(content)
        written.append(path)
    return written


def write_marker(target_dir: Path, component: dict) -> Path:
    """Write a marker file recording install metadata.

    Used by `dagster-component list` to detect components installed by us.
    """
    marker = target_dir / ".dg-community.json"
    payload = {
        "id": component.get("id"),
        "name": component.get("name"),
        "category": component.get("category"),
        "version": component.get("version", "1.0.0"),
        "registry_url": component.get("component_url"),
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    marker.write_text(json.dumps(payload, indent=2) + "\n")
    return marker


def parse_requirements(target_dir: Path) -> list[str]:
    """Return non-comment, non-empty lines from the component's requirements.txt."""
    req_file = target_dir / "requirements.txt"
    if not req_file.exists():
        return []
    out: list[str] = []
    for line in req_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def install_requirements(packages: list[str], *, manager: str = "auto") -> int:
    """Install pip packages. Returns the subprocess exit code (0 = success)."""
    if not packages:
        return 0

    if manager == "auto":
        manager = _detect_package_manager()

    if manager == "uv":
        cmd = ["uv", "pip", "install", *packages]
    else:
        cmd = [sys.executable, "-m", "pip", "install", *packages]

    proc = subprocess.run(cmd, check=False)
    return proc.returncode


def _detect_package_manager() -> str:
    """Return 'uv' if uv is on PATH, else 'pip'."""
    return "uv" if shutil.which("uv") else "pip"


def remove_component(target_dir: Path) -> None:
    """Remove a previously-installed component directory."""
    if not target_dir.exists():
        raise InstallError(f"Not found: {target_dir}")
    marker = target_dir / ".dg-community.json"
    if not marker.exists():
        raise InstallError(
            f"Not a community component (no .dg-community.json marker): {target_dir}\n"
            f"Refusing to remove to avoid clobbering hand-written code."
        )
    shutil.rmtree(target_dir)
