"""Reuse of the verified rewrite-pattern library by the deterministic converter, so the two routes cannot drift.

    index()                          -> reference/rewrite-patterns/index.json (read at emit time: refinements of the library are picked up on the next conversion)
    status(pattern_id, text)         -> the status ("VERIFIED", "PARTIAL", "FAILED") of the claim whose text contains `text`
    pull(pattern_id, names)          -> the source of the named top-level functions / assignments of the pattern's .py, exactly as the library writes them
    ensure_verified(pattern_id, text)-> raises when the claim the converter relies on is no longer VERIFIED in the index (the converter then must not emit that form)

The converter embeds `pull(...)` output in the generated runtime instead of writing a second copy of the logic.
"""
from __future__ import annotations

import ast
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DIRS = [HERE.parent / "reference" / "rewrite-patterns", HERE / "rewrite-patterns"]


def library_dir() -> Path:
    for d in DIRS:
        if (d / "index.json").exists():
            return d
    raise FileNotFoundError("reference/rewrite-patterns/index.json not found next to the converter")


@lru_cache(maxsize=1)
def index() -> dict[str, Any]:
    return json.loads((library_dir() / "index.json").read_text())


def entry(pattern_id: str) -> dict[str, Any]:
    for p in index()["patterns"]:
        if p["id"] == pattern_id:
            return p
    raise KeyError(pattern_id)


def status(pattern_id: str, text: str) -> str | None:
    for c in entry(pattern_id)["claims"]:
        if text.lower() in c["claim"].lower():
            return c["status"]
    return None


def ensure_verified(pattern_id: str, text: str) -> None:
    st = status(pattern_id, text)
    if st != "VERIFIED":
        raise RuntimeError(f"library claim {text!r} of pattern {pattern_id!r} is {st}, not VERIFIED: the converter must not emit this form")


def pull(pattern_id: str, names: list[str]) -> str:
    """Source of the named top-level defs / assignments from the pattern's code files, in the order asked."""
    found: dict[str, str] = {}
    for fname in entry(pattern_id)["code"]:
        src = (library_dir() / fname).read_text()
        for node in ast.parse(src).body:
            targets = [node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else [t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)]
            for n in targets:
                if n in names:
                    seg = ast.get_source_segment(src, node)
                    if isinstance(node, ast.FunctionDef) and node.decorator_list:      # get_source_segment skips decorators
                        seg = "\n".join(src.splitlines()[node.decorator_list[0].lineno - 1: node.end_lineno])
                    found[n] = seg
    missing = [n for n in names if n not in found]
    if missing:
        raise KeyError(f"{missing} not found in the {pattern_id!r} pattern's code")
    return "\n\n\n".join(found[n] for n in names) + "\n"
