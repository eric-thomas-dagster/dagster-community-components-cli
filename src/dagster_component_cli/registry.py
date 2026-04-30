"""Registry client — fetches and caches the community components manifest."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import requests

from . import DEFAULT_REGISTRY_URL


CACHE_DIR = Path.home() / ".cache" / "dagster-community-components"
CACHE_FILE = CACHE_DIR / "manifest.json"
CACHE_TTL_SECONDS = 3600  # 1 hour


class Registry:
    """Lazy, cached client for the community components manifest."""

    def __init__(self, url: Optional[str] = None, *, force_refresh: bool = False):
        self.url = url or os.environ.get("DAGSTER_COMPONENT_REGISTRY_URL", DEFAULT_REGISTRY_URL)
        self._manifest: Optional[dict] = None
        self._force_refresh = force_refresh

    @property
    def manifest(self) -> dict:
        if self._manifest is None:
            self._manifest = self._load()
        return self._manifest

    @property
    def components(self) -> list[dict]:
        return self.manifest.get("components", [])

    def get(self, component_id: str) -> Optional[dict]:
        """Return the manifest entry for `component_id`, or None if not found."""
        for c in self.components:
            if c.get("id") == component_id:
                return c
        return None

    def search(self, query: str, *, category: Optional[str] = None) -> list[dict]:
        """Return components whose id, name, description, or tags contain `query` (case-insensitive)."""
        q = query.lower()
        results = []
        for c in self.components:
            if category and c.get("category") != category:
                continue
            haystack = " ".join(
                str(c.get(field, "")) for field in ("id", "name", "description")
            ).lower()
            haystack += " " + " ".join(c.get("tags", [])).lower()
            if q in haystack:
                results.append(c)
        return results

    def categories(self) -> list[tuple[str, int]]:
        """Return [(category, count), ...] sorted by count desc."""
        counts: dict[str, int] = {}
        for c in self.components:
            cat = c.get("category", "unknown")
            counts[cat] = counts.get(cat, 0) + 1
        return sorted(counts.items(), key=lambda x: -x[1])

    # ------------------------------------------------------------------ internal

    def _load(self) -> dict:
        if not self._force_refresh and self._cache_fresh():
            try:
                return json.loads(CACHE_FILE.read_text())
            except (OSError, json.JSONDecodeError):
                pass  # cache corrupt, fall through to fetch

        return self._fetch()

    def _cache_fresh(self) -> bool:
        if not CACHE_FILE.exists():
            return False
        age = time.time() - CACHE_FILE.stat().st_mtime
        return age < CACHE_TTL_SECONDS

    def _fetch(self) -> dict:
        resp = requests.get(self.url, timeout=30)
        resp.raise_for_status()
        manifest = resp.json()
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(manifest))
        except OSError:
            pass  # cache is best-effort
        return manifest


def fetch_file(url: str) -> bytes:
    """Fetch a file from a URL, returning raw bytes. Raises on HTTP error."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content
