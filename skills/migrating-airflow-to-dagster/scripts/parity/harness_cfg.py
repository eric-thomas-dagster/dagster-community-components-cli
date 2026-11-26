"""The sandbox configuration the behaviour harness gives every execution (the original DAG, the converter, a rewrite, and the worker-side `compare_behavior.py`): one definition, so a worker's own run is set up exactly like the measurement."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def conn_uri(cid: str, root: Path) -> str:
    c = cid.lower()
    if "sqlite" in c:
        return f"sqlite:///{root / 'data' / 'app.db'}"
    if "http" in c:
        return "http://eval-http.test"
    if "smtp" in c or "email" in c:
        return "smtp://eval-smtp.test:25"
    if c.startswith("aws") or "s3" in c:
        return "aws://"
    return f"generic://eval-host.test/{cid}"


def sandbox_cfg(root: Path, spec: dict[str, Any], cap: float | None = None, seed: dict[str, str] | None = None) -> dict[str, Any]:
    return {"root": str(root), "connections": {c: conn_uri(c, root) for c in spec.get("connections", [])}, "variables": {v: "eval-variable" for v in spec.get("variables", [])}, "sleep_cap": cap, "seed": seed or {}}
