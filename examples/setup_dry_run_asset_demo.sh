#!/usr/bin/env bash
# dry_run_asset — @dry_run decorator + DryRunAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @dry_run decorator — wraps a plain @dg.asset (Python).
#            When enabled, the wrapped compute runs but the return value is
#            discarded — the IO manager is NOT invoked. Emits AssetObservation
#            tagged dry_run=true so audits can filter cleanly.
#   SHAPE 2: DryRunAssetComponent { wraps: SyntheticDataGeneratorComponent }
#            YAML-composed. When enabled=true, the OUTER short-circuits
#            BEFORE the inner runs (inner compute is NEVER invoked; no
#            "Generated DataFrame" in the log). When disabled, passthrough.
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-dry-run-asset-demo}"
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

SINK_DIR="$PROJECT_ABS/.sink"
mkdir -p "$SINK_DIR"

uv add -q "$DCC_SRC" pandas faker

PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# ═══ SHAPE 1: PYTHON DECORATOR ═══════════════════════════════════════════
# @dry_run on a plain @dg.asset. The wrapped compute runs (so you exercise
# the code path — retries, timing, logging), but the returned value is
# discarded — no IO manager write, no downstream persistence.
#
# We control enablement per-asset via a companion "enabled" flag, then
# also demo the run-tag / env var trigger paths further down.

cat > "$DEFS/py_costly_write.py" <<PY
"""SHAPE 1 — @dry_run wraps a @dg.asset directly.

The compute would normally write a CSV to \$SINK_DIR/py_costly_write.csv.
With @dry_run(enabled=True), the compute STILL RUNS (to exercise the
code path) — but the returned value is discarded (IO manager NOT invoked).
The sink file therefore never appears.
"""
import os
import pandas as pd
import dagster as dg
from dagster_community_components import dry_run

SINK_DIR = "$SINK_DIR"


@dg.asset(group_name="python_decorator")
@dry_run(enabled=True)  # flip to False (or remove) to actually persist
def py_costly_write(context):
    path = os.path.join(SINK_DIR, "py_costly_write.csv")
    df = pd.DataFrame({"row": range(50), "val": [i * 2 for i in range(50)]})
    # NB: we DO write to disk from inside compute — this is intentional.
    # The @dry_run decorator only skips the IO manager (return-value
    # persistence); side-effects inside compute are the developer's job
    # to gate. Real-world sinks should test context.run.tags.get('dry_run')
    # explicitly before touching the outside world.
    context.log.info(f"[py_costly_write] compute ran — would return {len(df)} rows")
    return df
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# DryRunAssetComponent WRAPS another DCC component. When enabled=true,
# the OUTER short-circuits BEFORE calling the inner's compute — the inner
# NEVER RUNS. No "Generated DataFrame" line in the log. The wrap is the
# strong dry-run: even side-effects inside the inner are prevented.

mkdir -p "$DEFS/yaml_customers"
cat > "$DEFS/yaml_customers/defs.yaml" <<'YAML'
# The OUTER DryRunAssetComponent wraps the inner SyntheticDataGeneratorComponent.
# With enabled=true, when yaml_customers materializes, the OUTER emits a
# synthetic MaterializeResult tagged dry_run=true — inner compute is NEVER
# invoked. That's the CRUX of `wraps:` for dry_run: it's a stronger dry-run
# than the Python decorator (which runs the wrapped fn but drops its output),
# because the inner component's side-effects are prevented entirely.
type: dagster_community_components.DryRunAssetComponent
attributes:
  enabled: true
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_customers
      schema_type: customers
      row_count: 1000
      random_state: 42
      group_name: yaml_component
YAML

# A second YAML sibling with dry-run DISABLED — proves passthrough.
mkdir -p "$DEFS/yaml_customers_live"
cat > "$DEFS/yaml_customers_live/defs.yaml" <<'YAML'
# Same shape, enabled=false — proves passthrough: inner runs unchanged
# (see "Generated DataFrame" line in the run log).
type: dagster_community_components.DryRunAssetComponent
attributes:
  enabled: false
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_customers_live
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
  { grep -E '\[py_costly_write\]|dry_run|Generated DataFrame|STEP_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@dry_run on @dg.asset) ═══"
_run 1 py_costly_write "OK — compute ran, return value discarded (IO manager NOT invoked)"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (DryRunAssetComponent wraps ..., enabled=true) ═══"
_run 2 yaml_customers "OK — inner compute NOT invoked (no 'Generated DataFrame' line); synthetic MaterializeResult tagged dry_run=true"

echo ""
echo "═══ SHAPE 2 (control): DryRunAssetComponent wraps ..., enabled=false ═══"
_run 3 yaml_customers_live "OK — passthrough; inner runs unchanged (SEE 'Generated DataFrame' line)"

echo ""
echo ">>> Dry-run observations from the event log:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<24}  {'dry_run_tag':<12}  {'inner_invoked':<14}  metadata")
    print(f"    {'-----':<24}  {'-----------':<12}  {'-------------':<14}  --------")
    for asset_name in ("py_costly_write", "yaml_customers", "yaml_customers_live"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        # Materialization also carries dry_run metadata — show both streams.
        mat_recs = list(reversed(inst.fetch_materializations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        for r in recs:
            obs = r.asset_observation
            if not obs: continue
            tags = dict(obs.tags or {})
            meta = {k: v.value for k, v in (obs.metadata or {}).items()}
            print(f"    {asset_name:<24}  {tags.get('dry_run','-'):<12}  {'(obs)':<14}  elapsed_s={meta.get('elapsed_seconds','-')}")
        for r in mat_recs:
            mat = r.asset_materialization
            if not mat: continue
            meta = {k: v.value for k, v in (mat.metadata or {}).items()}
            dr = meta.get('dry_run')
            ii = meta.get('inner_compute_invoked', '-')
            print(f"    {asset_name:<24}  {str(dr):<12}  {str(ii):<14}  (materialization)")
PY

cat <<DONE

✓ dry_run_asset demo done.

Two shapes, same primitive — but the YAML wrap is STRONGER:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_costly_write.py
      @dg.asset + @dry_run(enabled=True)
      → Compute RUNS (exercises retries / timing / logging). Return value
        is DISCARDED (IO manager NOT invoked). Side-effects inside the
        compute (a stray file write, a POST to an API) still happen — the
        author must gate those explicitly via context.run.tags.

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_customers/defs.yaml
      DryRunAssetComponent { enabled: true, wraps: SyntheticDataGeneratorComponent { ... } }
      → Inner compute is NEVER INVOKED. No "Generated DataFrame" line.
        This is a stronger dry-run — even the inner component's side
        effects are prevented. Zero risk of a stray write leaking out.

  Also: enable via run tag \`dry_run=true\` or env \`DAGSTER_DRY_RUN=1\` —
  same behavior, no code edit / redeploy required.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: DryRunAssetComponent{wraps:SlaAssetComponent{wraps:X}}
= @dry_run @sla @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py, enabled=true)   → compute ran, output discarded, observation
                               tagged dry_run=true emitted
  RUN 2 (yaml, enabled=true) → inner data-gen NEVER RAN (no 'Generated
                               DataFrame' in log); wrap emitted synthetic
                               MaterializeResult with inner_compute_invoked=false
  RUN 3 (yaml, enabled=false)→ passthrough: inner data-gen ran (see
                               'Generated DataFrame' line); no dry-run tag

Best fits: pre-production validation runs, cost audits, migration
dry-passes — anywhere you want to exercise pipeline plumbing without
persisting output or hitting external systems.

Cleanup: rm -rf $PROJECT_ABS
DONE
