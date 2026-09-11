#!/usr/bin/env bash
# sla_asset — @sla decorator + SlaAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @sla decorator — wraps a plain @dg.asset (Python)
#   SHAPE 2: SlaAssetComponent { wraps: SyntheticDataGeneratorComponent } —
#            YAML-composed. The outer SlaAssetComponent wraps ANOTHER
#            component's compute with the SLA primitive. Zero Python
#            in the demo `defs/` for this asset — pure YAML composition.
#
# Both shapes share the SAME breach observation format
# (sla_actual_seconds / sla_expected_seconds / sla_overrun_pct /
#  sla_escalated tag / sla_breach tag).
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-sla-asset-demo}"
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
# @sla on a plain @dg.asset. Best when you already have Python code you
# want to time.
#
# NOTE: no `-> dict` return annotation — even though this path always
# returns a dict, keeping the signature untyped keeps the decorator
# stack symmetric with the other DCC decorators (throttle/dry_run) that
# may return None on some paths.

cat > "$DEFS/py_slow_report.py" <<'PY'
"""SHAPE 1 — @sla wraps a @dg.asset directly."""
import os
import time
import dagster as dg
from dagster_community_components import sla


@dg.asset(group_name="python_decorator")
@sla(
    expected_duration_seconds=0.5,
    on_breach="warn",
    escalate_after_n_breaches=3,
    escalate_window_seconds=3600,
    key="py_slow_report_sla",
)
def py_slow_report(context):
    sleep_s = float(os.environ.get("SLEEP_SECONDS", "0.2"))
    context.log.info(f"[py_slow_report] simulating {sleep_s}s of work")
    time.sleep(sleep_s)
    return {"rows_processed": 100, "sleep_s": sleep_s}
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# SlaAssetComponent WRAPS another DCC component. The outer component adds
# an SLA timer around the inner component's compute — no Python required
# for this asset. The inner SyntheticDataGeneratorComponent generates 5000
# customer rows; we set a deliberately tight 0.001s SLA so the inner
# blows through it → breach observation on every run.

mkdir -p "$DEFS/yaml_slow_report"
cat > "$DEFS/yaml_slow_report/defs.yaml" <<'YAML'
# The OUTER SlaAssetComponent wraps another DCC component's compute
# with the SLA primitive. The INNER component is a
# SyntheticDataGeneratorComponent generating 5000 customer rows.
# expected_duration_seconds: 0.001 is deliberately tight so the inner
# always breaches — the point is to show breach observations on a
# wrapped compute, not to demonstrate within-SLA behavior.
type: dagster_community_components.SlaAssetComponent
attributes:
  expected_duration_seconds: 0.001
  on_breach: warn
  escalate_after_n_breaches: 3
  escalate_window_seconds: 3600
  sla_key: yaml_slow_report_sla
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_slow_report
      schema_type: customers
      row_count: 5000
      random_state: 42
      group_name: yaml_component
YAML

echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

_run() {
  local n="$1"; local asset="$2"; local sleep_s="$3"; local expect="$4"
  echo ""
  echo ">>> RUN $n  ($asset, SLEEP_SECONDS=$sleep_s) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  SLEEP_SECONDS="$sleep_s" uv run dg launch --assets "$asset" >"$LOG" 2>&1
  { grep -E '\[py_slow_report\]|\[sla\]|Generated|SyntheticData|sla_breach|STEP_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@sla on @dg.asset) ═══"
_run 1 py_slow_report 0.2 "0.2s < 0.5s SLA → OK, no breach"
_run 2 py_slow_report 1.0 "1.0s > 0.5s SLA → BREACH #1"
_run 3 py_slow_report 1.0 "1.0s > 0.5s SLA → BREACH #2"
_run 4 py_slow_report 1.0 "1.0s > 0.5s SLA → BREACH #3 → ESCALATED"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (SlaAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 5 yaml_slow_report 0.0 "5000-row synthetic data-gen > 0.001s SLA → BREACH; inner component ran"
_run 6 yaml_slow_report 0.0 "same — inner data-gen still runs even on breach (on_breach=warn)"

echo ""
echo ">>> SLA breach observations — proof both shapes emit the same events:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<20}  {'actual':<10}  {'expected':<10}  {'overrun%':<10}  {'escalated':<10}  breach_key")
    print(f"    {'-----':<20}  {'------':<10}  {'--------':<10}  {'--------':<10}  {'---------':<10}  ----------")
    for asset_name in ("py_slow_report", "yaml_slow_report"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        for r in recs:
            obs = r.asset_observation
            if not obs: continue
            tags = dict(obs.tags or {})
            meta = {k: v.value for k, v in (obs.metadata or {}).items()}
            actual = meta.get("sla_actual_seconds", "-")
            expected = meta.get("sla_expected_seconds", "-")
            overrun = meta.get("sla_overrun_pct", "-")
            escalated = tags.get("sla_escalated", "-")
            breach_key = tags.get("sla_breach", "-")
            print(f"    {asset_name:<20}  {str(actual)+'s':<10}  {str(expected)+'s':<10}  {str(overrun)+'%':<10}  {escalated:<10}  {breach_key}")
PY

cat <<DONE

✓ sla_asset demo done.

Two shapes, same event-log-backed SLA primitive:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_slow_report.py
      @dg.asset + @sla(expected_duration_seconds=0.5, key='py_slow_report_sla')

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_slow_report/defs.yaml
      SlaAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } }
      No Python — one component stacked on another. Add / remove /
      swap the outer decorator without touching the inner's config.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: BudgetAssetComponent{wraps:SlaAssetComponent{wraps:X}}
= @budget @sla @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py)   → OK        (0.2s < 0.5s SLA)
  RUN 2 (py)   → BREACH #1
  RUN 3 (py)   → BREACH #2
  RUN 4 (py)   → BREACH #3 → ESCALATED (sla_escalated=true, sensor-actionable)
  RUN 5 (yaml) → BREACH    (inner data-gen took > 0.001s SLA)
  RUN 6 (yaml) → BREACH    (still runs on_breach=warn — inner data-gen ran)

Escalation counts breaches in escalate_window_seconds (default 1h) via
context.instance.get_event_records — no external state.

Sensors: watch for tag sla_escalated=true → page.

Browse in the UI:
  export DAGSTER_HOME=$DAGSTER_HOME
  cd $PROJECT_DIR
  uv run dg dev  # → http://localhost:3000 → observations panel

Try:
  - Change on_breach="fail" → next breach raises dg.Failure with breach
    metadata (compute already ran, just marked failed).
  - Bump expected_duration_seconds on the YAML shape → inner data-gen
    is fast enough that most runs stay within SLA.

Cleanup: rm -rf $PROJECT_ABS
DONE
