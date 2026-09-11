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
uv add -q "$DCC_SRC" pandas pyarrow

PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# --- 4. The decorated asset — Python, not YAML ----------------------------
# NOTE: decorators are Python primitives — walkthroughs demo them with a
# real @dg.asset file (not a YAML wrapper). The decorator IS the point.

cat > "$DEFS/expensive_report.py" <<PY
"""Cached expensive compute — demoed via @cached decorator (v1.0)."""
import time
import pandas as pd
import dagster as dg
from dagster_community_components import cached

CACHE_DIR = "$CACHE_DIR"

@dg.asset(code_version="1.0", group_name="cache_demo")
@cached(cache_dir=CACHE_DIR, code_version="1.0", ttl_seconds=3600)
def expensive_report(context) -> pd.DataFrame:
    context.log.info("[expensive_report] MISS — running compute (sleeping 3s to simulate a heavy query)")
    time.sleep(3)
    return pd.DataFrame({
        "metric": ["revenue_usd", "order_count", "avg_order_value"],
        "value":  [125_430.75, 4_218, 29.73],
    })
PY

# --- 5. dg check defs -----------------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

# _step_duration <log-file>: extract STEP_SUCCESS duration for the expensive_report step.
# Robust under `set -eo pipefail` — grep never returns non-zero because we `|| true`
# and the outer command substitution won't fail even if nothing matches.
_step_duration() {
  { grep -oE 'expensive_report - STEP_SUCCESS - Finished execution of step "expensive_report" in [0-9.]+s' "$1" || true; } \
    | { grep -oE '[0-9.]+s' || true; } | tail -1
}

# --- 6. RUN 1 — cache MISS (fresh, no parquet yet). Expect ~3s compute. ---
echo ""
echo ">>> RUN 1  — expected: MISS + cached_asset_status=miss observation + ~3s COMPUTE"
LOG1="$PROJECT_ABS/.run1.log"
uv run dg launch --assets expensive_report >"$LOG1" 2>&1
{ grep -E '\[cached\]|\[expensive_report\]|STEP_SUCCESS' "$LOG1" || true; } | sed 's/^/    /'
echo "    ⏱  compute (step_success): $(_step_duration "$LOG1")"
echo ""
echo "    cache dir contents:"
ls -la "$CACHE_DIR" 2>/dev/null | grep -v '^total\|^d' | sed 's/^/      /' || echo "      (empty)"

# --- 7. RUN 2 — cache HIT. Same code_version + same key. --------------------
echo ""
echo ">>> RUN 2  — expected: HIT + cached_asset_status=hit observation + <1s COMPUTE (parquet load)"
LOG2="$PROJECT_ABS/.run2.log"
uv run dg launch --assets expensive_report >"$LOG2" 2>&1
{ grep -E '\[cached\]|\[expensive_report\]|STEP_SUCCESS' "$LOG2" || true; } | sed 's/^/    /'
echo "    ⏱  compute (step_success): $(_step_duration "$LOG2")"

# --- 8. Bump code_version → invalidate cache → RUN 3 should MISS again ----
echo ""
echo ">>> Bumping code_version 1.0 → 1.1 (this invalidates the cache key)"
cat > "$DEFS/expensive_report.py" <<PY
"""Cached expensive compute — demoed via @cached decorator (v1.1 — cache invalidated)."""
import time
import pandas as pd
import dagster as dg
from dagster_community_components import cached

CACHE_DIR = "$CACHE_DIR"

@dg.asset(code_version="1.1", group_name="cache_demo")
@cached(cache_dir=CACHE_DIR, code_version="1.1", ttl_seconds=3600)
def expensive_report(context) -> pd.DataFrame:
    context.log.info("[expensive_report] MISS — code_version bumped 1.0→1.1; cache key changed")
    time.sleep(3)
    return pd.DataFrame({
        "metric": ["revenue_usd", "order_count", "avg_order_value"],
        "value":  [125_430.75, 4_218, 29.73],
    })
PY

echo ""
echo ">>> RUN 3  — expected: MISS again (new code_version → new key) + ~3s COMPUTE"
LOG3="$PROJECT_ABS/.run3.log"
uv run dg launch --assets expensive_report >"$LOG3" 2>&1
{ grep -E '\[cached\]|\[expensive_report\]|STEP_SUCCESS' "$LOG3" || true; } | sed 's/^/    /'
echo "    ⏱  compute (step_success): $(_step_duration "$LOG3")"
echo ""
echo "    cache dir now has BOTH keys (one per code_version):"
ls -la "$CACHE_DIR" 2>/dev/null | grep -v '^total\|^d' | sed 's/^/      /'

# --- 9. Query the event log for cache observation events ----------------
# Every @cached materialization emits an AssetObservation with cache_status tag
# + cache_key/cache_path metadata — perfect shape for cache-hit-rate dashboards.
echo ""
echo ">>> Cache observation events from the event log (proof each run recorded its status):"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    recs = list(reversed(inst.fetch_observations(
        records_filter=dg.AssetKey("expensive_report"), limit=20,
    ).records))
    print(f"    {'run':<7}  {'status':<5}  {'cache_key':<26}  path")
    print(f"    {'---':<7}  {'-----':<5}  {'---------':<26}  ----")
    for i, r in enumerate(recs, start=1):
        obs = r.asset_observation
        if not obs:
            continue
        tags = dict(obs.tags or {})
        meta = {k: v.value for k, v in (obs.metadata or {}).items()}
        status = tags.get("cached_asset_status", "-")
        ck = (meta.get("cache_key") or "-")[:24]
        cp = meta.get("cache_path", "-")
        print(f"    RUN {i:<3}  {status:<5}  {ck:<26}  {cp}")
PY

# --- 10. Explainer --------------------------------------------------------
cat <<DONE

✓ cached_asset demo done.

What just happened:
  RUN 1  code_version=1.0  cache_key=A  → MISS  ran compute (3s), wrote parquet A
  RUN 2  code_version=1.0  cache_key=A  → HIT   loaded parquet A (<1s), skipped compute
  RUN 3  code_version=1.1  cache_key=B  → MISS  new code_version → new key, ran + wrote B

The cache dir now holds BOTH parquets — Dagster keeps them isolated by
key. Downgrade to code_version=1.0 → key A hits again (no recompute).

Browse in the UI:
  export DAGSTER_HOME=$DAGSTER_HOME
  cd $PROJECT_DIR
  uv run dg dev
  # → http://localhost:3000 → asset "expensive_report" → observations panel
  #   shows cached_asset_status=hit/miss + cache_key + cache_path per materialization

Try:
  - Re-run 3× more with code_version=1.1 → each is a HIT (~0s)
  - rm -rf .cache/expensive_report/ → next run is a MISS again
  - Add key_fn= to your @cached decorator to invalidate on external config

Cleanup: rm -rf $PROJECT_ABS
DONE
