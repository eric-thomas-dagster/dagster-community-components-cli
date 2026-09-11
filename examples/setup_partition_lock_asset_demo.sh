#!/usr/bin/env bash
# partition_lock_asset — @partition_lock decorator + PartitionLockAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @partition_lock decorator — wraps a plain @dg.asset (Python).
#            Per-partition mutex via Dagster event log. When on_conflict=skip
#            and the partition is already held, compute is short-circuited
#            (returns None + emits partition_lock_skipped observation).
#   SHAPE 2: PartitionLockAssetComponent { wraps: SyntheticDataGeneratorComponent }
#            YAML-composed. Same mutex behavior wrapped over another
#            component. When skipped, the inner data-gen is NEVER invoked.
#
# Sequential runs normally can't demonstrate lock conflict (finally-block
# releases the lock before the next launch), so we PRE-INJECT a stale
# "acquired" observation via `report_runless_asset_event` to simulate a
# concurrent holder. That's the same event-log state a concurrent run
# would create.
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-partition-lock-asset-demo}"
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

uv add -q "$DCC_SRC" pandas faker

PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# ═══ SHAPE 1: PYTHON DECORATOR ═══════════════════════════════════════════
# @partition_lock on a plain @dg.asset over a static-partition set.
# on_conflict=skip so the second (conflicting) launch short-circuits
# instead of hanging on the poll loop.

cat > "$DEFS/py_backfill.py" <<'PY'
"""SHAPE 1 — @partition_lock wraps a @dg.asset over static partitions."""
import time
import dagster as dg
from dagster_community_components import partition_lock


REGIONS = dg.StaticPartitionsDefinition(["us-east", "us-west"])


@dg.asset(partitions_def=REGIONS, group_name="python_decorator")
@partition_lock(ttl_seconds=300, on_conflict="skip")
def py_backfill(context):
    """Simulated per-region backfill — protected by a per-partition mutex."""
    pk = context.partition_key
    context.log.info(f"[py_backfill] running compute for partition_key={pk!r} (sleeping 1s)")
    time.sleep(1)
    return {"partition_key": pk, "rows_written": 100}
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# PartitionLockAssetComponent WRAPS SyntheticDataGeneratorComponent. Same
# static-partition set. When the lock is held (concurrent run OR pre-injected
# observation), the inner data-gen is NEVER INVOKED.

mkdir -p "$DEFS/yaml_customers"
cat > "$DEFS/yaml_customers/defs.yaml" <<'YAML'
type: dagster_community_components.PartitionLockAssetComponent
attributes:
  ttl_seconds: 300
  on_conflict: skip
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_customers
      schema_type: customers
      row_count: 100
      random_state: 42
      partition_type: static
      partition_values: us-east,us-west
      group_name: yaml_component
YAML

echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

# ─── Helper: inject a stale-but-still-in-TTL `partition_lock_acquired`
# observation for a given asset+partition_key, simulating another run
# currently holding the lock. Uses `report_runless_asset_event` which
# writes directly to the event log outside a run context.
_inject_held_lock() {
  local asset_name="$1"; local pk="$2"
  DAGSTER_HOME="$DAGSTER_HOME" uv run python - "$asset_name" "$pk" <<'PY'
import sys
import dagster as dg
from dagster import DagsterInstance, AssetObservation, AssetKey

asset_name, pk = sys.argv[1], sys.argv[2]
with DagsterInstance.get() as inst:
    inst.report_runless_asset_event(
        AssetObservation(
            asset_key=AssetKey(asset_name),
            tags={"partition_lock_acquired": pk},
            metadata={
                "partition_key": dg.MetadataValue.text(pk),
                "partition_lock_ttl_seconds": dg.MetadataValue.float(300.0),
                "injected_for_demo": dg.MetadataValue.bool(True),
            },
        )
    )
    print(f"    [injected] partition_lock_acquired={pk!r} on asset={asset_name!r}")
PY
}

_run() {
  local n="$1"; local asset="$2"; local partition="$3"; local expect="$4"
  echo ""
  echo ">>> RUN $n  ($asset:$partition) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  uv run dg launch --assets "$asset" --partition "$partition" >"$LOG" 2>&1
  { grep -E '\[py_backfill\]|@partition_lock|partition_lock_skipped|Generated DataFrame|STEP_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@partition_lock on @dg.asset) ═══"
_run 1 py_backfill us-east "acquires lock, runs (1s sleep), releases"

echo ""
echo ">>> [inject] simulating a concurrent run currently holding the lock for partition us-west:"
_inject_held_lock py_backfill us-west

_run 2 py_backfill us-west "SKIPPED — held lock detected, compute short-circuits (no [py_backfill] line)"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (PartitionLockAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 3 yaml_customers us-east "acquires lock, inner data-gen runs (SEE 'Generated DataFrame'), releases"

echo ""
echo ">>> [inject] simulating a concurrent run currently holding the lock for partition us-west:"
_inject_held_lock yaml_customers us-west

_run 4 yaml_customers us-west "SKIPPED — inner data-gen NOT INVOKED (no 'Generated DataFrame' line)"

echo ""
echo ">>> Lock observations from the event log:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<20}  {'event':<28}  partition_key")
    print(f"    {'-----':<20}  {'-----':<28}  -------------")
    for asset_name in ("py_backfill", "yaml_customers"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=40,
        ).records))
        for r in recs:
            obs = r.asset_observation
            if not obs: continue
            tags = dict(obs.tags or {})
            for tag_key in ("partition_lock_acquired", "partition_lock_released", "partition_lock_skipped"):
                if tag_key in tags:
                    print(f"    {asset_name:<20}  {tag_key:<28}  {tags[tag_key]}")
PY

cat <<DONE

✓ partition_lock_asset demo done.

Two shapes, same event-log-backed per-partition mutex:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_backfill.py
      @dg.asset(partitions_def=REGIONS) + @partition_lock(ttl_seconds=300, on_conflict='skip')
      → Static-partition asset. Each partition_key is its own lock scope.

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_customers/defs.yaml
      PartitionLockAssetComponent { ttl_seconds: 300, on_conflict: skip,
                                     wraps: SyntheticDataGeneratorComponent {
                                       partition_type: static,
                                       partition_values: us-east,us-west, ...
                                     } }
      → No Python — one component stacked on another. Inner defines its
        own partitions_def; outer gates each partition's compute on the
        per-partition lock.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: PartitionLockAssetComponent{wraps:SlaAssetComponent{wraps:X}}
= @partition_lock @sla @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py:us-east)      → acquired lock, compute ran (1s), released
  RUN 2 (py:us-west)      → PRE-INJECTED "acquired" observation via
                            report_runless_asset_event → run saw the
                            held lock, on_conflict=skip fired, compute
                            NEVER RAN (no [py_backfill] log line)
  RUN 3 (yaml:us-east)    → acquired lock, inner data-gen ran (100
                            customer rows for us-east), released
  RUN 4 (yaml:us-west)    → PRE-INJECTED "acquired" → SKIPPED; inner
                            SyntheticDataGen NEVER INVOKED (no 'Generated
                            DataFrame' line)

Lock state lives in the Dagster event log — restart-safe, worker-safe.
Auto-expires via ttl_seconds (protects against stuck holders); no
manual cleanup jobs. Pair with a sensor that alerts on lock-held
observations older than TTL to catch dead holders.

Race-condition disclosure: this is a probabilistic mutex, not a
distributed atomic. Two runs starting within ~200ms could both see
"unlocked" and acquire. Fine for "prevent 5-minute concurrent backfill
duplicates"; NOT for money transfers.

Cleanup: rm -rf $PROJECT_ABS
DONE
