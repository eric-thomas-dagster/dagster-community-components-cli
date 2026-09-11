#!/usr/bin/env bash
# cached_asset — @cached decorator: skip expensive Python compute when nothing changed.
#
# Fully offline (no API keys). Demonstrates:
#   1. cache MISS (first run) — expensive_report sleeps 3s + writes parquet cache
#   2. cache HIT (second run) — same code_version, same key → skips compute, loads parquet
#   3. cache INVALIDATED (third run) — code_version bumped → key changes → miss again
#
# Watch for `cached_asset_status=hit|miss` observation tags in the run log after each launch.
#
# Shape:
#     expensive_report (@dg.asset + @cached decorator)
#          │
#          └── cache_dir: $PROJECT_ABS/.cache/expensive_report/
#              └── <cache_key>.parquet  (auto-generated per code_version + partition_key)
#
# 100% offline — nothing external, no API keys.

set -eo pipefail

PROJECT_DIR="${1:-cached-asset-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi

# --- 1. Fresh project scaffold --------------------------------------------
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

# --- 2. Env ---------------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout: $DCC_LOCAL_PATH)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"
CACHE_DIR="$PROJECT_ABS/.cache/expensive_report"
mkdir -p "$CACHE_DIR"

# --- 3. Install deps ------------------------------------------------------
uv add -q "$DCC_SRC" pandas pyarrow faker

PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# ═══ SHAPE 1: PYTHON DECORATOR ═══════════════════════════════════════════
# @cached on a plain @dg.asset. Best when wrapping existing Python code.

cat > "$DEFS/py_expensive_report.py" <<PY
"""SHAPE 1 — @cached decorator on @dg.asset (v1.0)."""
import time
import pandas as pd
import dagster as dg
from dagster_community_components import cached

CACHE_DIR = "$CACHE_DIR/py_shape"

@dg.asset(code_version="1.0", group_name="python_decorator")
@cached(cache_dir=CACHE_DIR, code_version="1.0", ttl_seconds=3600)
def py_expensive_report(context) -> pd.DataFrame:
    context.log.info("[py_expensive_report] MISS — running compute (sleeping 3s)")
    time.sleep(3)
    return pd.DataFrame({
        "metric": ["revenue_usd", "order_count", "avg_order_value"],
        "value":  [125_430.75, 4_218, 29.73],
    })
PY

# ═══ SHAPE 2: YAML COMPOSABILITY  ← the money shot ══════════════════════
# CachedAssetComponent WRAPS another DCC component. The outer cache adds
# content-addressable caching around the inner component's compute — zero
# Python for this asset. First run invokes the inner data-gen (1000 rows);
# subsequent runs load the parquet cache and skip the inner entirely.

CACHE_YAML_DIR="$CACHE_DIR/yaml_shape"
mkdir -p "$DEFS/yaml_expensive_report" "$CACHE_YAML_DIR"
cat > "$DEFS/yaml_expensive_report/defs.yaml" <<YAML
type: dagster_community_components.CachedAssetComponent
attributes:
  cache_dir: $CACHE_YAML_DIR
  code_version: v1
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_expensive_report
      schema_type: customers
      row_count: 1000
      random_state: 42
      group_name: yaml_component
YAML

# --- 5. dg check defs -----------------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

_run() {
  local n="$1"; local asset="$2"; local expect="$3"
  echo ""
  echo ">>> RUN $n  ($asset) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  uv run dg launch --assets "$asset" >"$LOG" 2>&1
  { grep -E '\[cached|\[py_expensive_report|Generated DataFrame|STEP_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@cached on @dg.asset) ═══"
_run 1 py_expensive_report "MISS  ~3s (writes parquet cache)"
_run 2 py_expensive_report "HIT   near-zero (loads parquet, skips compute)"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (CachedAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 3 yaml_expensive_report "MISS — inner SyntheticDataGen runs (1000 rows), writes parquet cache"
_run 4 yaml_expensive_report "HIT — inner data-gen NOT invoked (no 'Generated DataFrame'), parquet loaded"

# --- 9. Query the event log for cache observation events ----------------
# Every @cached materialization emits an AssetObservation with cache_status tag
# + cache_key/cache_path metadata — perfect shape for cache-hit-rate dashboards.
echo ""
echo ">>> Cache observation events from the event log (both shapes emit the same events):"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<24}  {'status':<5}  {'cache_key':<26}  path (last 40 chars)")
    print(f"    {'-----':<24}  {'-----':<5}  {'---------':<26}  --------------------")
    for asset_name in ("py_expensive_report", "yaml_expensive_report"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        for r in recs:
            obs = r.asset_observation
            if not obs:
                continue
            tags = dict(obs.tags or {})
            meta = {k: v.value for k, v in (obs.metadata or {}).items()}
            status = tags.get("cached_asset_status", "-")
            ck = (meta.get("cache_key") or "-")[:24]
            cp = str(meta.get("cache_path", "-"))[-40:]
            print(f"    {asset_name:<24}  {status:<5}  {ck:<26}  ...{cp}")
PY

# --- Explainer -----------------------------------------------------------
cat <<DONE

✓ cached_asset demo done.

Two shapes, same event-log-backed cache primitive:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_expensive_report.py
      @dg.asset(code_version="1.0") + @cached(cache_dir=..., code_version="1.0")

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_expensive_report/defs.yaml
      CachedAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } }
      Zero Python. Cache wraps another component's compute — on hit, inner
      is NOT invoked; on miss, inner runs + returned DataFrame is cached.

What just happened:
  RUN 1 (py)   → MISS  ran compute (3s), wrote parquet
  RUN 2 (py)   → HIT   loaded parquet (<1s), skipped compute
  RUN 3 (yaml) → MISS  inner SyntheticDataGen runs (1000 rows), wrote parquet
  RUN 4 (yaml) → HIT   inner data-gen NOT invoked, parquet loaded

Both shapes cache to $CACHE_DIR/ and both emit AssetObservation events
with cached_asset_status tag + cache_key/cache_path metadata.

Browse in the UI:
  export DAGSTER_HOME=$DAGSTER_HOME
  cd $PROJECT_DIR
  uv run dg dev
  # → http://localhost:3000 → observations panel

Try:
  - Bump code_version on either shape → next run is a MISS (new cache key)
  - rm -rf $CACHE_DIR → next run is a MISS on both
  - Wrap a real query component: CachedAssetComponent { wraps: SnowflakeQueryComponent }

Cleanup: rm -rf $PROJECT_ABS
DONE
