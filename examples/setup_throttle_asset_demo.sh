#!/usr/bin/env bash
# throttle_asset — @throttle decorator + ThrottleAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @throttle decorator — wraps a plain @dg.asset (Python)
#   SHAPE 2: ThrottleAssetComponent { wraps: SyntheticDataGeneratorComponent } —
#            YAML-composed. The outer ThrottleAssetComponent wraps ANOTHER
#            component's compute with the throttle primitive. Zero Python
#            in the demo `defs/` for this asset — pure YAML composition.
#
# Both shapes share the SAME event-log-backed rate-limit state and emit
# the SAME throttle_skipped observations.
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-throttle-asset-demo}"
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
# @throttle on a plain @dg.asset. Best when you already have Python code
# you want to rate-limit.

cat > "$DEFS/py_report.py" <<'PY'
"""SHAPE 1 — @throttle wraps a @dg.asset directly.

No `-> dict` annotation because on_throttle='skip' returns None, which
would fail Dagster's type check against a dict annotation.
"""
import dagster as dg
from dagster_community_components import throttle


@dg.asset(group_name="python_decorator")
@throttle(min_gap_seconds=60, on_throttle="skip", key="py_report")
def py_report(context):
    context.log.info("[py_report] compute ran — record materialization")
    return {"rows": 100, "cost_usd": 5.00}
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# ThrottleAssetComponent WRAPS another DCC component. The outer component
# adds throttle rate-limiting around the inner component's compute — no
# Python required for this asset.

mkdir -p "$DEFS/yaml_report"
cat > "$DEFS/yaml_report/defs.yaml" <<'YAML'
# The OUTER ThrottleAssetComponent wraps another DCC component's compute
# with the throttle primitive. The INNER component is a
# SyntheticDataGeneratorComponent that would normally generate 1000
# customer rows every time — with this wrap, it can only run once per 60s.
type: dagster_community_components.ThrottleAssetComponent
attributes:
  min_gap_seconds: 60
  on_throttle: skip
  key: yaml_report_throttle
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_report
      schema_type: customers
      row_count: 1000
      random_state: 42
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
  { grep -E '\[py_report\]|\[throttle|throttle_skipped|Generated|SyntheticData|STEP_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@throttle on @dg.asset) ═══"
_run 1 py_report "1st materialization → OK, compute runs"
_run 2 py_report "<60s later → THROTTLED (compute skipped)"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (ThrottleAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 3 yaml_report "1st materialization → OK; inner SyntheticDataGen runs (1000 rows)"
_run 4 yaml_report "<60s later → THROTTLED (inner data-gen NOT invoked)"

echo ""
echo ">>> Waiting 65s to prove post-throttle recovery on Shape 2..."
sleep 65

_run 5 yaml_report ">60s since last materialization → OK; inner data-gen runs again"

echo ""
echo ">>> Throttle observations — proof both shapes emit the same events:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<15}  {'skipped':<24}  wait_seconds")
    print(f"    {'-----':<15}  {'-------':<24}  ------------")
    for asset_name in ("py_report", "yaml_report"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        for r in recs:
            obs = r.asset_observation
            if not obs: continue
            tags = dict(obs.tags or {})
            meta = {k: v.value for k, v in (obs.metadata or {}).items()}
            skipped = tags.get("throttle_skipped", "-")
            wait = meta.get("throttle_wait_seconds", "?")
            print(f"    {asset_name:<15}  {skipped:<24}  {wait}")
PY

cat <<DONE

✓ throttle_asset demo done.

Two shapes, same event-log-backed primitive:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_report.py
      @dg.asset + @throttle(min_gap_seconds=60, key='py_report')

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_report/defs.yaml
      ThrottleAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } }
      No Python — one component stacked on another. Add / remove /
      swap the outer decorator without touching the inner's config.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: BudgetAssetComponent{wraps:SlaAssetComponent{wraps:X}}
= @budget @sla @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py)   → OK        (first materialization for py_report)
  RUN 2 (py)   → THROTTLED  (<60s since RUN 1)
  RUN 3 (yaml) → OK        (first materialization for yaml_report; inner data-gen runs)
  RUN 4 (yaml) → THROTTLED  (inner data-gen NOT invoked; compute skipped)
  RUN 5 (yaml) → OK        (65s+ past RUN 3; inner data-gen runs again)

Rate-limit state lives in the Dagster event log (last successful
ASSET_MATERIALIZATION timestamp) — no Redis, no cross-process
coordination, works across workers + restarts + concurrent runs.

Cleanup: rm -rf $PROJECT_ABS
DONE
