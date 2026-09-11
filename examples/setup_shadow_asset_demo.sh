#!/usr/bin/env bash
# shadow_asset — @shadow decorator + ShadowAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @shadow decorator — wraps a plain @dg.asset (Python)
#            Dual-run old + new implementations; diff outputs; production
#            uses primary; shadow reports via AssetObservation.
#
#   SHAPE 2: ShadowAssetComponent { wraps: <primary>, shadow_wraps: <alt> } —
#            YAML-composed. The outer ShadowAssetComponent runs BOTH inner
#            components side-by-side, materializes the primary, diffs the
#            shadow. Zero Python — perfect for vendor swaps.
#
# Both shapes emit the SAME shadow_match observations.
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-shadow-asset-demo}"
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
# @shadow on a plain @dg.asset. The primary compute produces the canonical
# asset value; the shadow runs after with the same inputs and its output is
# diffed and emitted as an AssetObservation. Shadow exceptions are trapped —
# production is unaffected.

cat > "$DEFS/py_order_totals.py" <<'PY'
"""SHAPE 1 — @shadow wraps a @dg.asset directly.

Simulates a migration: `old_report_impl` (primary) is the currently-shipping
implementation; `new_report_impl` (shadow) is a candidate rewrite. They
diverge on one row so the shadow observation reports a mismatch.
"""
import pandas as pd
import dagster as dg
from dagster_community_components import shadow


def _new_report_impl(context):
    # Candidate rewrite — same shape, slightly different values (rounding
    # difference simulates a real migration bug).
    return pd.DataFrame({
        "customer_id": [1, 2, 3],
        "total_usd":  [100.00, 200.00, 305.00],  # last row diverges: 300 vs 305
    })


@dg.asset(group_name="python_decorator")
@shadow(_new_report_impl, enforce_match=False)
def py_order_totals(context):
    context.log.info("[py_order_totals] running PRIMARY (old impl)")
    result = pd.DataFrame({
        "customer_id": [1, 2, 3],
        "total_usd":  [100.00, 200.00, 300.00],
    })
    context.log.info(f"[py_order_totals] primary produced {len(result)} rows")
    return result
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# ShadowAssetComponent WRAPS two DCC components side-by-side. `wraps:` is
# the primary (materializes); `shadow_wraps:` runs alongside and its output
# is diffed. Perfect for vendor/library swaps — replace `wraps:` with the
# new implementation, keep `shadow_wraps:` as the old one during rollout.
#
# The two SyntheticDataGeneratorComponents share the schema + row_count
# but use different random_state values → same shape, different data →
# a mismatch observation is emitted every run.

mkdir -p "$DEFS/yaml_order_totals"
cat > "$DEFS/yaml_order_totals/defs.yaml" <<'YAML'
# The OUTER ShadowAssetComponent wraps TWO DCC components side-by-side.
# The primary (wraps:) materializes; the shadow (shadow_wraps:) runs
# alongside and its result is diffed. The two data generators produce
# the SAME schema but different rows (random_state differs) — the diff
# observation reports a mismatch every run.
type: dagster_community_components.ShadowAssetComponent
attributes:
  enforce_match: false          # false (default) = observe; true = fail-on-mismatch
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_order_totals
      schema_type: customers
      row_count: 100
      random_state: 42
      group_name: yaml_component
  shadow_wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_order_totals
      schema_type: customers
      row_count: 100
      random_state: 43
      group_name: yaml_component
YAML

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
  { grep -E '\[py_order_totals\]|\[shadow|MISMATCH|Generated|SyntheticData|STEP_SUCCESS|RUN_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@shadow on @dg.asset) ═══"
_run 1 py_order_totals "primary materializes; shadow diff → MISMATCH observation (last row: 300 vs 305)"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (ShadowAssetComponent wraps two components) ═══"
_run 2 yaml_order_totals "primary materializes; shadow diff → MISMATCH observation (100 rows, different random_state)"

echo ""
echo ">>> Shadow observations — proof both shapes emit the same events:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<20}  {'shadow_match':<14}  {'diff_mode':<12}  diff_rows")
    print(f"    {'-----':<20}  {'------------':<14}  {'---------':<12}  ---------")
    for asset_name in ("py_order_totals", "yaml_order_totals"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        if not recs:
            print(f"    {asset_name:<20}  (no shadow observations)")
            continue
        for r in recs:
            obs = r.asset_observation
            if not obs: continue
            tags = dict(obs.tags or {})
            meta = {k: v.value for k, v in (obs.metadata or {}).items()}
            match = tags.get("shadow_match", "-")
            mode = meta.get("shadow_diff_mode", "-")
            drows = meta.get("shadow_diff_rows", "-")
            print(f"    {asset_name:<20}  {match:<14}  {str(mode):<12}  {drows}")
PY

cat <<DONE

✓ shadow_asset demo done.

Two shapes, same dual-run + diff primitive:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_order_totals.py
      @dg.asset + @shadow(new_report_impl, enforce_match=False)

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_order_totals/defs.yaml
      ShadowAssetComponent { wraps: PrimaryComp, shadow_wraps: AltComp }
      No Python — two components stacked side-by-side. Primary
      materializes; shadow's output is diffed and reported.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Direct fit for vendor swaps:

  ShadowAssetComponent {
    wraps: SnowflakeQueryComponent { ... }           # new
    shadow_wraps: RedshiftQueryComponent { ... }     # old
  }

What just happened:
  RUN 1 (py)   → primary materialized; shadow observation: shadow_match=false (mode=dataframe, diff_rows=1)
  RUN 2 (yaml) → primary materialized; shadow observation: shadow_match=false (mode=dataframe, diff_rows≈100)

Production always uses the primary result. Shadow exceptions are trapped —
they never fail the run unless enforce_match=true.

Migration playbook: ship shadow → verify convergence via the event log →
flip primary → drop shadow. Every stage tracked as an AssetObservation.

Cleanup: rm -rf $PROJECT_ABS
DONE
