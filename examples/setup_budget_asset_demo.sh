#!/usr/bin/env bash
# budget_asset — @budget decorator + BudgetAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @budget decorator — wraps a plain @dg.asset (Python)
#            + custom cost_fn returning $0.40 flat per run
#   SHAPE 2: BudgetAssetComponent { wraps: SyntheticDataGeneratorComponent } —
#            YAML-composed. The outer BudgetAssetComponent wraps ANOTHER
#            component's compute with the budget primitive. Zero Python
#            in the demo `defs/` for this asset — pure YAML composition.
#            Uses cost_per_second (wall-clock rate) instead of cost_fn.
#
# Both shapes share the SAME event-log-backed budget history
# (budget_cost_estimate_usd + budget_cumulative_usd + budget_cap_usd
#  metadata + budget_breach tag).
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-budget-asset-demo}"
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
# @budget on a plain @dg.asset with a custom cost_fn (real per-call
# pricing model — the shape you'd use for OpenAI/Anthropic token pricing).
# Three runs at $0.40 each — cumulative crosses the $1.00 cap on RUN 3.

cat > "$DEFS/py_costly_pipeline.py" <<'PY'
"""SHAPE 1 — @budget wraps a @dg.asset directly with a custom cost_fn."""
import dagster as dg
from dagster_community_components import budget


def usd_cost(context, elapsed_s, result) -> float:
    """Cost model: this compute costs $0.40 flat.

    For a real LLM/API asset, this is where you'd multiply
    result.usage.total_tokens by your provider's price."""
    return 0.40


@dg.asset(group_name="python_decorator")
@budget(cost_fn=usd_cost, budget_usd=1.00, window_days=30, on_breach="warn")
def py_costly_pipeline(context) -> dict:
    context.log.info("[py_costly_pipeline] pretending to spend $0.40")
    return {"processed": 100, "unit_cost_usd": 0.40}
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# BudgetAssetComponent WRAPS another DCC component. The outer component
# adds budget tracking around the inner component's compute — no Python
# required for this asset. cost_per_second: 100.0 means the wall-clock
# cost of the inner (which takes ~50ms to generate 1000 rows) is ~$5,
# BLOWING THROUGH the $1.00 cap on the very first run — a good stress
# test of the pre-flight-still-runs-once shape (subsequent runs get
# on_breach=warn breaches with a big cumulative).

mkdir -p "$DEFS/yaml_costly_pipeline"
cat > "$DEFS/yaml_costly_pipeline/defs.yaml" <<'YAML'
# The OUTER BudgetAssetComponent wraps another DCC component's compute
# with the budget primitive. The INNER component generates 1000 synthetic
# customer rows. At cost_per_second=100.0 and ~50ms wall-clock per run,
# each run costs ~$5 → immediately blows through the $1.00 cap.
type: dagster_community_components.BudgetAssetComponent
attributes:
  budget_usd: 1.00
  cost_per_second: 100.0
  window_days: 30
  on_breach: warn
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_costly_pipeline
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
  { grep -E '\[py_costly_pipeline\]|\[budget\]|Generated|SyntheticData|budget_breach|STEP_SUCCESS|Failure' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@budget on @dg.asset, cost_fn variant) ═══"
_run 1 py_costly_pipeline "cost=\$0.40 cumulative=\$0.40 within \$1.00 → OK"
_run 2 py_costly_pipeline "cost=\$0.40 cumulative=\$0.80 within \$1.00 → OK"
_run 3 py_costly_pipeline "cost=\$0.40 cumulative=\$1.20 BREACHES \$1.00 cap → budget_breach=true"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (BudgetAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 4 yaml_costly_pipeline "inner data-gen ~50ms × \$100/s ≈ \$5 → BLOWS \$1.00 cap immediately"
_run 5 yaml_costly_pipeline "same — inner data-gen runs, cumulative grows, still tagged budget_breach=true"

echo ""
echo ">>> Budget observations — proof both shapes emit the same events:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<22}  {'cost':<10}  {'cumulative':<12}  {'breach':<7}  budget")
    print(f"    {'-----':<22}  {'----':<10}  {'----------':<12}  {'------':<7}  ------")
    for asset_name in ("py_costly_pipeline", "yaml_costly_pipeline"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        for r in recs:
            obs = r.asset_observation
            if not obs: continue
            tags = dict(obs.tags or {})
            meta = {k: v.value for k, v in (obs.metadata or {}).items()}
            cost = meta.get("budget_cost_estimate_usd", "-")
            cum = meta.get("budget_cumulative_usd", "-")
            breach = tags.get("budget_breach", "false")
            cap = meta.get("budget_cap_usd", "-")
            print(f"    {asset_name:<22}  ${str(cost):<9}  ${str(cum):<11}  {breach:<7}  ${cap}")
PY

cat <<DONE

✓ budget_asset demo done.

Two shapes, same event-log-backed budget primitive:

  ─ SHAPE 1: Python decorator (cost_fn variant)
      src/$PKG/defs/py_costly_pipeline.py
      @dg.asset + @budget(cost_fn=usd_cost, budget_usd=1.00)

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_costly_pipeline/defs.yaml
      BudgetAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } }
      No Python — one component stacked on another. Add / remove /
      swap the outer decorator without touching the inner's config.
      cost_per_second: 100.0 = wall-clock rate (contrast with cost_fn
      for token-priced APIs).

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: BudgetAssetComponent{wraps:SlaAssetComponent{wraps:X}}
= @budget @sla @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py)   → OK        (cost=\$0.40 cumulative=\$0.40)
  RUN 2 (py)   → OK        (cost=\$0.40 cumulative=\$0.80)
  RUN 3 (py)   → BREACH    (cost=\$0.40 cumulative=\$1.20 > \$1.00 cap)
  RUN 4 (yaml) → BREACH    (inner data-gen wall-clock × \$100/s ≫ \$1.00 cap)
  RUN 5 (yaml) → BREACH    (cumulative grows; inner data-gen still runs on_breach=warn)

Cumulative cost is queried FROM THE EVENT LOG (AssetObservation events
tagged budget_cost_asset=<key> within window_days) — no side database.

Browse in the UI:
  export DAGSTER_HOME=$DAGSTER_HOME
  cd $PROJECT_DIR
  uv run dg dev
  # → http://localhost:3000 → observations panel

Try:
  - Change on_breach="fail" → pre-flight check raises dg.Failure before
    the next run's compute (saves the run since cumulative is already over).
  - Change on_breach="skip" → next run returns MaterializeResult(budget_skipped=true).
  - Bump budget_usd on the yaml shape → wraps stops breaching.

Cleanup: rm -rf $PROJECT_ABS
DONE
