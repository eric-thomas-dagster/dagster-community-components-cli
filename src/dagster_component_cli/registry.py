"""Registry client — fetches, caches, and searches the community components manifest."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

from . import DEFAULT_REGISTRY_URL

CACHE_DIR = Path.home() / ".cache" / "dagster-community-components"
CACHE_FILE = CACHE_DIR / "manifest.json"
CACHE_TTL_SECONDS = 3600  # 1 hour


# ─── Search ranking ──────────────────────────────────────────────────────────
#
# Field-weight table. Higher score = more decisive match. Chosen so that:
#   - An exact ID match beats any other field: 100.
#   - `keywords` (curated aliases) ~ `name` — a well-authored keyword like
#     "sql server" for mssql_resource should outrank a description mention.
#   - `agent_hints.when_to_use` and `example_prompts` outrank `description`
#     because agent_hints are more intentional / signal-dense.
#   - `description` and generic agent_hints fields sit at the bottom.
#
# Term-in-multiple-fields is rewarded by simply summing per-field scores
# (a term that hits both name AND description scores name+desc = 40),
# which naturally captures relevance beyond single-field noise.

FIELD_WEIGHTS = {
    "id_exact": 100,
    "id_substring": 50,
    "name": 30,
    "keywords": 40,
    "tags": 25,
    "hint_primary": 20,   # when_to_use, example_prompts, use_cases
    "description": 10,
    "hint_secondary": 5,  # everything else in agent_hints
    "consumes": 5,
    "provides": 5,
    "pip_dep": 3,
}

HINT_PRIMARY_KEYS = {"when_to_use", "example_prompts", "use_cases"}


def _tokenize(query: str) -> list[str]:
    """Split query into search terms. Case-insensitive, keeps alphanumerics
    and hyphens/underscores/dots joined (so `mcp_call` stays one token, but
    "sql server" splits into ["sql", "server"])."""
    return [t for t in re.findall(r"[a-z0-9_\-\.]+", query.lower()) if t]


def _term_match(term: str, haystack: str) -> bool:
    """Whole-word or substring match. `postgres` matches `postgresql`
    (substring); `sql` in `mssql_resource` also matches (substring). This is
    intentionally permissive — precision comes from the AND-of-terms + scoring."""
    return term in haystack


def _score_component(component: dict, terms: list[str]) -> tuple[int, dict]:
    """Return (score, matched_fields_dict).

    All terms must match SOMEWHERE across the searchable fields (AND).
    If any term matches nothing, score is 0 and the component is filtered out."""
    # Pre-lower every searchable field once for the loop.
    id_lower = str(component.get("id", "")).lower()
    name_lower = str(component.get("name", "")).lower()
    desc_lower = str(component.get("description", "")).lower()
    tags_str = " ".join(str(t).lower() for t in (component.get("tags") or []))
    keywords_str = " ".join(str(k).lower() for k in (component.get("keywords") or []))
    consumes_str = " ".join(str(c).lower() for c in (component.get("consumes") or []))
    provides_str = " ".join(
        str(p).lower() for p in (component.get("x-dagster-provides") or [])
    )
    pip_deps_str = " ".join(
        str(p).lower() for p in ((component.get("dependencies") or {}).get("pip") or [])
    )

    # Flatten agent_hints into two haystacks: "primary" (when_to_use +
    # example_prompts + use_cases) and "secondary" (everything else).
    ah = component.get("agent_hints") or {}
    hint_primary_bits: list[str] = []
    hint_secondary_bits: list[str] = []
    for k, v in ah.items():
        # Values can be str / list / dict / nested. Flatten to a string blob.
        blob = json.dumps(v, default=str) if not isinstance(v, str) else v
        (hint_primary_bits if k in HINT_PRIMARY_KEYS else hint_secondary_bits).append(
            blob.lower()
        )
    hint_primary_str = " ".join(hint_primary_bits)
    hint_secondary_str = " ".join(hint_secondary_bits)

    score = 0
    matched_fields: dict[str, list[str]] = {}
    all_terms_matched = True

    for term in terms:
        term_score = 0
        term_hits: list[str] = []

        # id: exact match is jackpot; substring is strong.
        if id_lower == term:
            term_score += FIELD_WEIGHTS["id_exact"]
            term_hits.append("id")
        elif _term_match(term, id_lower):
            term_score += FIELD_WEIGHTS["id_substring"]
            term_hits.append("id")

        if _term_match(term, name_lower):
            term_score += FIELD_WEIGHTS["name"]
            term_hits.append("name")
        if _term_match(term, keywords_str):
            term_score += FIELD_WEIGHTS["keywords"]
            term_hits.append("keywords")
        if _term_match(term, tags_str):
            term_score += FIELD_WEIGHTS["tags"]
            term_hits.append("tags")
        if _term_match(term, hint_primary_str):
            term_score += FIELD_WEIGHTS["hint_primary"]
            term_hits.append("agent_hints.primary")
        if _term_match(term, desc_lower):
            term_score += FIELD_WEIGHTS["description"]
            term_hits.append("description")
        if _term_match(term, hint_secondary_str):
            term_score += FIELD_WEIGHTS["hint_secondary"]
            term_hits.append("agent_hints.other")
        if _term_match(term, consumes_str):
            term_score += FIELD_WEIGHTS["consumes"]
            term_hits.append("consumes")
        if _term_match(term, provides_str):
            term_score += FIELD_WEIGHTS["provides"]
            term_hits.append("x-dagster-provides")
        if _term_match(term, pip_deps_str):
            term_score += FIELD_WEIGHTS["pip_dep"]
            term_hits.append("dependencies.pip")

        if term_score == 0:
            all_terms_matched = False
            break

        score += term_score
        for h in term_hits:
            matched_fields.setdefault(h, []).append(term)

    if not all_terms_matched:
        return (0, {})

    return (score, matched_fields)


class Registry:
    """Lazy, cached client for the community components manifest."""

    def __init__(self, url: str | None = None, *, force_refresh: bool = False):
        self.url = url or os.environ.get("DAGSTER_COMPONENT_REGISTRY_URL", DEFAULT_REGISTRY_URL)
        self._manifest: dict | None = None
        self._force_refresh = force_refresh

    @property
    def manifest(self) -> dict:
        if self._manifest is None:
            self._manifest = self._load()
        return self._manifest

    @property
    def components(self) -> list[dict]:
        return self.manifest.get("components", [])

    def get(self, component_id: str) -> dict | None:
        """Return the manifest entry for `component_id`, or None if not found."""
        for c in self.components:
            if c.get("id") == component_id:
                return c
        return None

    def search(
        self,
        query: str,
        *,
        category: str | None = None,
        produces: str | None = None,
        with_scores: bool = False,
    ) -> list[dict] | list[tuple[dict, int, dict]]:
        """Rank components against `query` using a multi-term AND, field-weighted score.

        Search corpus per component:
          id, name, description, tags, keywords, agent_hints (with primary /
          secondary weight), consumes, x-dagster-provides, dependencies.pip.

        Filters:
          category:  restrict to a component family (see `categories()`).
          produces:  restrict to components emitting a Dagster primitive
                     (asset | multi_asset | asset_check | job | schedule | sensor
                      | resource | io_manager | partitions_def | other).

        Returns:
          By default, a list of manifest entries ranked by score descending.
          With `with_scores=True`, a list of `(entry, score, matched_fields)`
          tuples — used by the CLI's `--json` output.

        Empty query returns every component that passes `category` / `produces`
        filters (unranked — original manifest order preserved), matching the
        legacy behavior of `search("")` for browsing.
        """
        terms = _tokenize(query)

        # Filter phase.
        filtered = []
        for c in self.components:
            if category and c.get("category") != category:
                continue
            if produces and produces not in (c.get("produces") or []):
                continue
            filtered.append(c)

        # No terms → return filtered set unranked, all scored 0.
        if not terms:
            if with_scores:
                return [(c, 0, {}) for c in filtered]
            return filtered

        # Score + rank.
        scored: list[tuple[dict, int, dict]] = []
        for c in filtered:
            score, matched = _score_component(c, terms)
            if score > 0:
                scored.append((c, score, matched))
        scored.sort(key=lambda t: (-t[1], t[0].get("id", "")))

        if with_scores:
            return scored
        return [c for c, _s, _m in scored]

    def categories(self) -> list[tuple[str, int]]:
        """Return [(category, count), ...] sorted by count desc."""
        counts: dict[str, int] = {}
        for c in self.components:
            cat = c.get("category", "unknown")
            counts[cat] = counts.get(cat, 0) + 1
        return sorted(counts.items(), key=lambda x: -x[1])

    def produces_index(self) -> list[tuple[str, int]]:
        """Return [(primitive, count), ...] over the `produces` field, sorted by count desc."""
        counts: dict[str, int] = {}
        for c in self.components:
            for p in c.get("produces") or []:
                counts[p] = counts.get(p, 0) + 1
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
