#!/usr/bin/env bash
# budget_asset — @budget decorator: per-asset $ cost tracking + rolling-window budget cap.
#
# Fully offline. Demonstrates:
#   RUN 1: cost=$0.40, cumulative=$0.40, within $1.00 budget  → materializes OK
#   RUN 2: cost=$0.40, cumulative=$0.80, within $1.00 budget  → materializes OK
#   RUN 3: cost=$0.40, cumulative=$1.20, BREACHES $1.00 cap   → observation tagged budget_breach=true
#
# on_breach: warn  →  run still succeeds, breach flagged in materialization metadata + observation.
# Change to on_breach: "fail" and RUN 3 raises dg.Failure BEFORE compute (saving the run).
# Change to on_breach: "skip" and RUN 3 returns MaterializeResult(budget_skipped=true).
#
# 100% offline — no API keys, no network.

set -eo pipefail

PROJECT_DIR="${1:-budget-asset-demo}"
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

# --- 4. The decorated asset — Python, not YAML ----------------------------
# NOTE: decorators are Python primitives — walkthroughs demo them with a real
# @dg.asset file, not a YAML wrapper. The decorator IS the point.

cat > "$DEFS/costly_pipeline.py" <<'PY'
"""Per-asset $ cost tracking via @budget — demo asset (cost_fn variant)."""
import dagster as dg
from dagster_community_components import budget


def usd_cost(context, elapsed_s, result) -> float:
    """Cost model: this compute costs $0.40 flat (imagine an API call priced per event)."""
    return 0.40


@dg.asset(group_name="budget_demo")
@budget(cost_fn=usd_cost, budget_usd=1.00, window_days=30, on_breach="warn")
def costly_pipeline(context) -> dict:
    context.log.info("[costly_pipeline] pretending to spend $0.40")
    return {"processed": 100, "unit_cost_usd": 0.40}
PY

# --- 5. dg check defs -----------------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

# _run_and_report <run_number> <expected_breach>
_run_and_report() {
  local n="$1"; local expect="$2"
  echo ""
  echo ">>> RUN $n  — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  uv run dg launch --assets costly_pipeline >"$LOG" 2>&1
  { grep -E '\[costly_pipeline\]|\[budget\]|STEP_SUCCESS|Failure' "$LOG" || true; } | sed 's/^/    /'
}

_run_and_report 1 "cost=\$0.40 cumulative=\$0.40 within \$1.00 budget → OK"
_run_and_report 2 "cost=\$0.40 cumulative=\$0.80 within \$1.00 budget → OK"
_run_and_report 3 "cost=\$0.40 cumulative=\$1.20 BREACHES \$1.00 cap → observation tagged budget_breach"

# --- 6. Query the event log for cost observations -------------------------
echo ""
echo ">>> Budget observations from the event log (proof each run recorded cost + breach status):"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    recs = list(reversed(inst.fetch_observations(
        records_filter=dg.AssetKey("costly_pipeline"), limit=20,
    ).records))
    print(f"    {'run':<7}  {'cost':<10}  {'cumulative':<12}  {'breach':<7}  budget")
    print(f"    {'---':<7}  {'----':<10}  {'----------':<12}  {'------':<7}  ------")
    for i, r in enumerate(recs, start=1):
        obs = r.asset_observation
        if not obs: continue
        tags = dict(obs.tags or {})
        meta = {k: v.value for k, v in (obs.metadata or {}).items()}
        cost = meta.get("budget_cost_estimate_usd", "-")
        cum = meta.get("budget_cumulative_usd", "-")
        breach = tags.get("budget_breach", "false")
        cap = meta.get("budget_cap_usd", "-")
        print(f"    RUN {i:<3}  ${str(cost):<9}  ${str(cum):<11}  {breach:<7}  ${cap}")
PY

# --- 7. Explainer ---------------------------------------------------------
cat <<DONE

✓ budget_asset demo done.

What just happened:
  RUN 1: cost=\$0.40  cumulative=\$0.40   → within budget, OK
  RUN 2: cost=\$0.40  cumulative=\$0.80   → within budget, OK
  RUN 3: cost=\$0.40  cumulative=\$1.20   → BREACH → observation tagged budget_breach=true

Browse in the UI:
  export DAGSTER_HOME=$DAGSTER_HOME
  cd $PROJECT_DIR
  uv run dg dev
  # → http://localhost:3000 → asset "costly_pipeline" → observations panel
  #   Every materialization has cost + cumulative + breach status.

Try:
  - Edit costly_pipeline.py → change on_breach: "warn" to "fail"
      RUN 4 will raise dg.Failure BEFORE compute (saves the run since cumulative already \$1.20).
  - Change on_breach: "skip" → RUN 4 returns MaterializeResult(budget_skipped=true).
  - Replace usd_cost with an LLM token-priced cost_fn (see @budget README).

Cleanup: rm -rf $PROJECT_ABS
DONE
