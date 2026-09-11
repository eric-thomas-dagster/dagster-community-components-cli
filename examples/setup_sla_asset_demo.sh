#!/usr/bin/env bash
# sla_asset — @sla decorator: wall-clock SLA enforcement on asset compute.
#
# Fully offline. Demonstrates:
#   RUN 1: sleep 0.2s → within 0.5s SLA  → OK, no breach
#   RUN 2: sleep 1.0s → exceeds 0.5s SLA → breach observation emitted
#   RUN 3: sleep 1.0s again → 2nd breach in the window
#   RUN 4: sleep 1.0s again → 3rd breach → ESCALATED (tag sla_escalated=true)
#
# on_breach: warn  →  asset materializes + breach observation. Sensors can watch.
# Change to on_breach: "fail" and RUN 2+ raise dg.Failure with breach metadata.
#
# 100% offline — no API keys.

set -eo pipefail

PROJECT_DIR="${1:-sla-asset-demo}"
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

# --- 3. Install deps ------------------------------------------------------
uv add -q "$DCC_SRC"

PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# --- 4. The decorated asset ----------------------------------------------
cat > "$DEFS/slow_report.py" <<'PY'
"""Wall-clock SLA on asset compute via @sla — with escalation after N breaches."""
import os
import time
import dagster as dg
from dagster_community_components import sla


@dg.asset(group_name="sla_demo")
@sla(
    expected_duration_seconds=0.5,
    on_breach="warn",
    escalate_after_n_breaches=3,
    escalate_window_seconds=3600,
    key="slow_report_sla",
)
def slow_report(context) -> dict:
    sleep_s = float(os.environ.get("SLEEP_SECONDS", "0.2"))
    context.log.info(f"[slow_report] simulating {sleep_s}s of work")
    time.sleep(sleep_s)
    return {"rows_processed": 100, "sleep_s": sleep_s}
PY

# --- 5. dg check defs -----------------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

_run_and_report() {
  local n="$1"; local sleep_s="$2"; local expect="$3"
  echo ""
  echo ">>> RUN $n  (SLEEP_SECONDS=$sleep_s) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  SLEEP_SECONDS="$sleep_s" uv run dg launch --assets slow_report >"$LOG" 2>&1
  { grep -E '\[slow_report\]|\[sla\]|STEP_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
}

_run_and_report 1 0.2 "0.2s < 0.5s SLA → OK"
_run_and_report 2 1.0 "1.0s > 0.5s SLA → BREACH (1st in window)"
_run_and_report 3 1.0 "1.0s > 0.5s SLA → BREACH (2nd in window)"
_run_and_report 4 1.0 "1.0s > 0.5s SLA → BREACH (3rd in window → ESCALATED)"

# --- 6. Query the event log for breach observations ----------------------
echo ""
echo ">>> SLA breach observations from the event log:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    recs = list(reversed(inst.fetch_observations(
        records_filter=dg.AssetKey("slow_report"), limit=20,
    ).records))
    print(f"    {'run':<7}  {'actual':<8}  {'expected':<10}  {'overrun%':<9}  {'escalated':<10}  breach_key")
    print(f"    {'---':<7}  {'------':<8}  {'--------':<10}  {'--------':<9}  {'---------':<10}  ----------")
    for i, r in enumerate(recs, start=1):
        obs = r.asset_observation
        if not obs: continue
        tags = dict(obs.tags or {})
        meta = {k: v.value for k, v in (obs.metadata or {}).items()}
        actual = meta.get("sla_actual_seconds", "?")
        expected = meta.get("sla_expected_seconds", "?")
        overrun = meta.get("sla_overrun_pct", "?")
        escalated = tags.get("sla_escalated", "?")
        breach_key = tags.get("sla_breach", "?")
        print(f"    OBS {i:<3}  {str(actual)+'s':<8}  {str(expected)+'s':<10}  {str(overrun)+'%':<9}  {escalated:<10}  {breach_key}")
PY

# --- 7. Explainer ---------------------------------------------------------
cat <<DONE

✓ sla_asset demo done.

What just happened:
  RUN 1  0.2s < 0.5s SLA         → OK (no breach event)
  RUN 2  1.0s > 0.5s SLA         → BREACH #1 in window
  RUN 3  1.0s > 0.5s SLA         → BREACH #2 in window
  RUN 4  1.0s > 0.5s SLA         → BREACH #3 → ESCALATED (sensor-actionable)

Escalation counts breaches in escalate_window_seconds (default 1h) via
context.instance.get_event_records — no external state.

Sensors: watch for tag sla_escalated=true → page.

Browse in the UI:
  export DAGSTER_HOME=$DAGSTER_HOME
  cd $PROJECT_DIR
  uv run dg dev  # → http://localhost:3000 → slow_report → observations

Try:
  - Change on_breach="fail" → RUN 2 raises dg.Failure with breach metadata (compute already ran, just marked failed).
  - Change escalate_after_n_breaches=2 → escalation fires on RUN 3.
  - SLEEP_SECONDS=0.4 uv run dg launch --assets slow_report → within SLA, no observation.

Cleanup: rm -rf $PROJECT_ABS
DONE
