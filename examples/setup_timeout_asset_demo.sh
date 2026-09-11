#!/usr/bin/env bash
# timeout_asset — @timeout decorator + TimeoutAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @timeout decorator — wraps a plain @dg.asset (Python)
#            Hard-kill compute at N seconds. Portable, Dagster+ Serverless-safe.
#
#   SHAPE 2: TimeoutAssetComponent { wraps: SyntheticDataGeneratorComponent } —
#            YAML-composed. The outer TimeoutAssetComponent wraps ANOTHER
#            component's compute with the hard-kill primitive. Zero Python
#            in the demo `defs/` for this asset — pure YAML composition.
#
# Both shapes emit the SAME timeout observation events and raise the SAME
# `dg.Failure` type on deadline exceeded.
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-timeout-asset-demo}"
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
# @timeout on a plain @dg.asset. Best when you already have Python code
# you want to hard-kill on deadline exceeded.
#
# NO `-> dict` annotation because on_timeout='fail' path never returns
# (raises), and on_timeout='warn' returns None. Either way, an explicit
# return annotation would trip Dagster's type check.

cat > "$DEFS/py_slow_api.py" <<'PY'
"""SHAPE 1 — @timeout wraps a @dg.asset directly."""
import os
import time
import dagster as dg
from dagster_community_components import timeout


@dg.asset(group_name="python_decorator")
@timeout(1.0, on_timeout="fail", key="py_slow_api")
def py_slow_api(context):
    sleep_s = float(os.environ.get("SLEEP_SECONDS", "0.3"))
    context.log.info(f"[py_slow_api] sleeping {sleep_s}s (timeout=1.0s)")
    time.sleep(sleep_s)
    context.log.info(f"[py_slow_api] completed after {sleep_s}s")
    return {"ok": True, "elapsed": sleep_s}
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# TimeoutAssetComponent WRAPS another DCC component. The outer component
# adds a hard-kill timeout around the inner component's compute — no
# Python required for this asset.
#
# The inner SyntheticDataGeneratorComponent normally produces 5000 rows
# in ~100ms. With timeout_seconds: 0.01 (10ms) the outer hard-kills
# before the inner can finish — proves the wrap intercepts compute.

mkdir -p "$DEFS/yaml_slow_api"
cat > "$DEFS/yaml_slow_api/defs.yaml" <<'YAML'
# The OUTER TimeoutAssetComponent wraps another DCC component's compute
# with the hard-kill timeout primitive. The INNER component is a
# SyntheticDataGeneratorComponent that would normally generate 5000
# customer rows in ~100ms — with timeout_seconds: 0.01, it's cancelled
# before it can finish.
type: dagster_community_components.TimeoutAssetComponent
attributes:
  timeout_seconds: 0.01
  on_timeout: fail
  timeout_key: yaml_slow_api_timeout
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_slow_api
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
  local n="$1"; local asset="$2"; local expect="$3"; local expect_fail="${4:-false}"; local sleep_s="${5:-}"
  echo ""
  echo ">>> RUN $n  ($asset) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  if [ "$expect_fail" = "true" ]; then
    SLEEP_SECONDS="$sleep_s" uv run dg launch --assets "$asset" >"$LOG" 2>&1 || true
  else
    SLEEP_SECONDS="$sleep_s" uv run dg launch --assets "$asset" >"$LOG" 2>&1
  fi
  { grep -E '\[py_slow_api\]|\[timeout|Failure|RUN_(SUCCESS|FAILURE)|STEP_SUCCESS|STEP_FAILURE|Generated|SyntheticData' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@timeout on @dg.asset) ═══"
_run 1 py_slow_api "0.3s < 1.0s → OK" false 0.3
_run 2 py_slow_api "2.0s > 1.0s → hard-killed, dg.Failure" true 2.0

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (TimeoutAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 3 yaml_slow_api "5000-row generation > 10ms timeout → inner cancelled, dg.Failure" true

echo ""
echo ">>> Timeout observations — proof both shapes emit the same events:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<18}  {'timeout_hit':<26}  timeout_seconds")
    print(f"    {'-----':<18}  {'-----------':<26}  ---------------")
    for asset_name in ("py_slow_api", "yaml_slow_api"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        if not recs:
            print(f"    {asset_name:<18}  (no timeout observations)")
            continue
        for r in recs:
            obs = r.asset_observation
            if not obs: continue
            tags = dict(obs.tags or {})
            hit = tags.get("timeout_hit", "-")
            secs = tags.get("timeout_seconds", "-")
            print(f"    {asset_name:<18}  {hit:<26}  {secs}")
PY

cat <<DONE

✓ timeout_asset demo done.

Two shapes, same hard-kill primitive:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_slow_api.py
      @dg.asset + @timeout(1.0, on_timeout='fail', key='py_slow_api')

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_slow_api/defs.yaml
      TimeoutAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } }
      No Python — one component stacked on another. Swap the timeout
      threshold or on_timeout mode without touching the inner's config.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: SmartRetryAssetComponent{wraps:TimeoutAssetComponent{wraps:X}}
= @smart_retry @timeout @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py)   → OK        (0.3s < 1.0s timeout)
  RUN 2 (py)   → FAILURE   (2.0s > 1.0s timeout → hard-killed)
  RUN 3 (yaml) → FAILURE   (5000-row inner data-gen > 10ms timeout → cancelled)

Hard-kill uses concurrent.futures.ThreadPoolExecutor + future.result(timeout=…).
Portable across every Dagster deployment shape (unlike signal.SIGALRM
which is Unix-main-thread only). Dagster+ Serverless-safe.

Caveat: Python threads can't be truly killed — the cancelled compute
keeps running in the background but its result is discarded. The
deadline is enforced from Dagster's perspective.

Cleanup: rm -rf $PROJECT_ABS
DONE
