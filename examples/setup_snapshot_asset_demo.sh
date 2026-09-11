#!/usr/bin/env bash
# snapshot_asset — @snapshot decorator + SnapshotAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @snapshot decorator — wraps a plain @dg.asset (Python)
#            Every materialization writes a point-in-time snapshot to an
#            fsspec URI keyed by code_version + timestamp + run_id.
#
#   SHAPE 2: SnapshotAssetComponent { wraps: SyntheticDataGeneratorComponent } —
#            YAML-composed. Snapshotting wraps the inner component's compute
#            with zero Python.
#
# Both shapes emit the SAME snapshot_asset=written observations with
# snapshot_path / snapshot_bytes / snapshot_format metadata — rollback is
# a Dagster event log query.
#
# 100% offline (no API keys). Snapshots land inside the project dir for
# Windows-safe portability.

set -eo pipefail

PROJECT_DIR="${1:-snapshot-asset-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi

rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout: $DCC_LOCAL_PATH)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

# Snapshot dirs live INSIDE the project so cleanup is `rm -rf $PROJECT_DIR`
# and Windows git-bash /tmp vs Python C:/tmp mismatch never bites.
SNAP_PY_DIR="$PROJECT_ABS/.snap-py"
SNAP_YAML_DIR="$PROJECT_ABS/.snap-yaml"
mkdir -p "$SNAP_PY_DIR" "$SNAP_YAML_DIR"

uv add -q "$DCC_SRC" pandas pyarrow faker

PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# ═══ SHAPE 1: PYTHON DECORATOR ═══════════════════════════════════════════
# @snapshot on a plain @dg.asset. Best when you already have Python code
# and want to add checkpointing. Every run writes a new snapshot; rollback
# is `find snapshots WHERE version != current`.

cat > "$DEFS/py_daily_report.py" <<PY
"""SHAPE 1 — @snapshot wraps a @dg.asset directly."""
import pandas as pd
import dagster as dg
from dagster_community_components import snapshot

SNAP_URI = "$SNAP_PY_DIR"


@dg.asset(code_version="v1", group_name="python_decorator")
@snapshot(uri=SNAP_URI, format="parquet")
def py_daily_report(context) -> pd.DataFrame:
    context.log.info(f"[py_daily_report] building report + snapshotting to {SNAP_URI}")
    return pd.DataFrame({
        "metric": ["revenue_usd", "order_count", "avg_order_value"],
        "value":  [125_430.75, 4_218, 29.73],
    })
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# SnapshotAssetComponent WRAPS another DCC component. The outer component
# snapshots the inner component's compute output after every successful
# materialization — no Python required for this asset.

mkdir -p "$DEFS/yaml_daily_report"
cat > "$DEFS/yaml_daily_report/defs.yaml" <<YAML
# The OUTER SnapshotAssetComponent wraps another DCC component's compute
# with the snapshot primitive. Every materialization of the inner
# SyntheticDataGeneratorComponent writes a parquet snapshot to
# $SNAP_YAML_DIR keyed by code_version + timestamp + run_id.
type: dagster_community_components.SnapshotAssetComponent
attributes:
  uri: $SNAP_YAML_DIR
  format: parquet
  code_version: v1
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_daily_report
      schema_type: customers
      row_count: 100
      random_state: 42
      group_name: yaml_component
YAML

echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

_run() {
  local n="$1"; local asset="$2"; local expect="$3"; local snap_dir="$4"
  echo ""
  echo ">>> RUN $n  ($asset) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  uv run dg launch --assets "$asset" >"$LOG" 2>&1
  { grep -E '\[py_daily_report\]|\[snapshot|@snapshot|Generated|SyntheticData|STEP_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
  echo "    ---- snapshot dir after run: ----"
  { find "$snap_dir" -type f 2>/dev/null | sort || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@snapshot on @dg.asset) ═══"
_run 1 py_daily_report "writes 1 parquet snapshot to \$SNAP_PY_DIR/py_daily_report/v1/" "$SNAP_PY_DIR"
_run 2 py_daily_report "writes 2nd parquet snapshot — two snapshots now in the folder" "$SNAP_PY_DIR"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (SnapshotAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 3 yaml_daily_report "inner data-gen runs; outer writes 1 parquet snapshot to \$SNAP_YAML_DIR/yaml_daily_report/v1/" "$SNAP_YAML_DIR"
_run 4 yaml_daily_report "inner data-gen runs again; 2nd snapshot lands next to first" "$SNAP_YAML_DIR"

echo ""
echo ">>> Snapshot observations — proof both shapes emit the same events:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<20}  {'status':<8}  {'format':<8}  {'bytes':<8}  path (last 60 chars)")
    print(f"    {'-----':<20}  {'------':<8}  {'------':<8}  {'-----':<8}  --------------------")
    for asset_name in ("py_daily_report", "yaml_daily_report"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        if not recs:
            print(f"    {asset_name:<20}  (no snapshot observations)")
            continue
        for r in recs:
            obs = r.asset_observation
            if not obs: continue
            tags = dict(obs.tags or {})
            meta = {k: v.value for k, v in (obs.metadata or {}).items()}
            status = tags.get("snapshot_asset", "-")
            fmt = tags.get("snapshot_format", "-")
            byts = str(meta.get("snapshot_bytes", "-"))
            path = str(meta.get("snapshot_path", "-"))[-60:]
            print(f"    {asset_name:<20}  {status:<8}  {fmt:<8}  {byts:<8}  ...{path}")
PY

cat <<DONE

✓ snapshot_asset demo done.

Two shapes, same event-log-backed snapshot primitive:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_daily_report.py
      @dg.asset(code_version='v1') + @snapshot(uri=..., format='parquet')

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_daily_report/defs.yaml
      SnapshotAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } }
      No Python — one component stacked on another. Snapshot every
      materialization without touching the inner's config.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: SnapshotAssetComponent{wraps:CachedAssetComponent{wraps:X}}
= @snapshot @cached @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py)   → 1 parquet snapshot in $SNAP_PY_DIR/py_daily_report/v1/
  RUN 2 (py)   → 2 parquet snapshots (per-run timestamped filenames)
  RUN 3 (yaml) → 1 parquet snapshot in $SNAP_YAML_DIR/yaml_daily_report/v1/
  RUN 4 (yaml) → 2 parquet snapshots (2nd next to first)

Path shape: <uri>/<asset_name>/<code_version>/<timestamp>__<run_id_short>.<ext>

Rollback = event log query:
    SELECT snapshot_path FROM observations
    WHERE snapshot_asset='written' AND asset_key='daily_report'
    ORDER BY timestamp DESC LIMIT 1

Use fsspec URIs to write to s3://, gs://, abfs:// with the same code.

Cleanup: rm -rf $PROJECT_ABS
DONE
