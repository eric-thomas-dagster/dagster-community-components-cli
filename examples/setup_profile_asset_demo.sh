#!/usr/bin/env bash
# profile_asset — @profile decorator + ProfileAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @profile decorator — wraps a plain @dg.asset (Python)
#   SHAPE 2: ProfileAssetComponent { wraps: SyntheticDataGeneratorComponent } —
#            YAML-composed. Zero Python in the demo `defs/` for this asset —
#            the outer profile component wraps the inner data-gen's compute
#            and auto-emits the profile AssetObservation.
#
# Both shapes emit the SAME AssetObservation shape: `profile` (full JSON
# nested dict) + `profile_row_count` / `profile_column_count` tags.
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-profile-asset-demo}"
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
# @profile on a plain @dg.asset. Best when you already have Python code
# whose returned DataFrame you want to auto-profile per materialization.

cat > "$DEFS/py_orders.py" <<'PY'
"""SHAPE 1 — @profile wraps a @dg.asset directly.

No `-> pd.DataFrame` annotation: @profile yields dg.Output(df, metadata=...)
under the hood; the @dg.asset function is effectively a generator.
"""
import random
import pandas as pd
import dagster as dg
from dagster_community_components import profile


@dg.asset(group_name="python_decorator")
@profile(categorical_max_distinct=10, top_n_columns=5)
def py_orders(context):
    context.log.info("[py_orders] building 500 synthetic order rows")
    random.seed(42)
    n = 500
    return pd.DataFrame({
        "order_id":  [f"o_{i:05d}" for i in range(n)],
        "region":    [random.choice(["us-east", "us-west", "eu", "apac"]) for _ in range(n)],
        "amount":    [round(random.uniform(5.0, 500.0), 2) for _ in range(n)],
        "status":    [random.choice(["paid", "refunded", "pending"]) for _ in range(n)],
        "is_first_purchase": [random.random() < 0.15 for _ in range(n)],
    })
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# ProfileAssetComponent WRAPS another DCC component. The outer component
# extracts the DataFrame returned by the inner component's compute and
# emits a profile AssetObservation — no Python required for this asset.

mkdir -p "$DEFS/yaml_customers"
cat > "$DEFS/yaml_customers/defs.yaml" <<'YAML'
# The OUTER ProfileAssetComponent wraps another DCC component's compute
# with auto-profiling. The INNER component is a SyntheticDataGeneratorComponent
# that generates 500 customer rows — after the inner runs, the outer
# profile computes null_ratio / distinct_count / min / max / mean / std
# per column and emits it all as one AssetObservation.
type: dagster_community_components.ProfileAssetComponent
attributes:
  categorical_max_distinct: 10
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_customers
      schema_type: customers
      row_count: 500
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
  { grep -E '\[profile|\[py_orders|Generated DataFrame|STEP_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@profile on @dg.asset) ═══"
_run 1 py_orders "OK — profile emitted (500 rows, 5 cols)"
_run 2 py_orders "OK — profile emitted again (deterministic; observation history grows)"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (ProfileAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 3 yaml_customers "OK — inner data-gen runs (500 customer rows) + profile emitted"

echo ""
echo ">>> Profile observations — proof both shapes emit the same shape:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import json
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<20}  {'rows':<6}  {'cols':<6}  sample column stats")
    print(f"    {'-----':<20}  {'----':<6}  {'----':<6}  -------------------")
    for asset_name in ("py_orders", "yaml_customers"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        for r in recs:
            obs = r.asset_observation
            if not obs:
                continue
            tags = dict(obs.tags or {})
            meta = {k: v.value for k, v in (obs.metadata or {}).items()}
            rows = tags.get("profile_row_count", "-")
            cols = tags.get("profile_column_count", "-")
            prof = meta.get("profile") or "{}"
            try:
                pj = json.loads(prof) if isinstance(prof, str) else prof
                col_names = list((pj.get("columns") or {}).keys())[:3]
                first_col = col_names[0] if col_names else "-"
                first_stats = pj.get("columns", {}).get(first_col, {}) if first_col != "-" else {}
                dtype = first_stats.get("dtype", "-")
                distinct = first_stats.get("distinct_count", "-")
                sample = f"{first_col}({dtype}, distinct={distinct})"
            except Exception:
                sample = "-"
            print(f"    {asset_name:<20}  {rows:<6}  {cols:<6}  {sample}")
PY

cat <<DONE

✓ profile_asset demo done.

Two shapes, same profile-shape AssetObservation:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_orders.py
      @dg.asset + @profile(categorical_max_distinct=10, top_n_columns=5)

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_customers/defs.yaml
      ProfileAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } }
      No Python — one component stacked on another. The outer profile
      intercepts the DataFrame the inner returns and emits null_ratio /
      distinct_count / min / max / mean / std per column as one AssetObservation.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: ProfileAssetComponent{wraps:CachedAssetComponent{wraps:X}}
= @profile @cached @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py)          → profile emitted (500 rows × 5 cols); random\_state=42
                        so numeric columns have stable min/max/mean/std
  RUN 2 (py)          → profile emitted again — event log grows a second
                        snapshot; drift detection = query the log, diff
  RUN 3 (yaml)        → inner SyntheticDataGen runs (500 customer rows);
                        outer profile computes + emits observation

Drift detection: query fetch_observations() for each asset. Each row is a
JSON snapshot — compute row_count / null_ratio deltas between adjacent
observations, alert when a threshold trips. No external metrics store.

Cleanup: rm -rf $PROJECT_ABS
DONE
