#!/usr/bin/env bash
# hooks_asset — @on_hooks decorator + HooksAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @on_hooks decorator — wraps a plain @dg.asset (Python).
#            on_success + on_failure callbacks fire per outcome. We
#            exercise BOTH paths (one success asset + one failing asset).
#   SHAPE 2: HooksAssetComponent { wraps: SyntheticDataGeneratorComponent }
#            YAML-composed. The outer hooks component wraps the inner
#            component's compute — after the inner materializes, the
#            on_success callback fires with the (context, result) pair.
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-hooks-asset-demo}"
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

# ─── Callback module — plain Python fns referenced by `mod:fn` ────────────
# `@on_hooks` / HooksAssetComponent take callbacks as string refs so both
# Python + YAML shapes use the same wiring. In a real project this file
# would live wherever your team's shared hooks live (slack notifier,
# JIRA ticket creator, oncall pager, etc.).
cat > "src/$PKG/hooks.py" <<'PY'
"""Shared hook callbacks — referenced from both shapes as `<pkg>.hooks:fn`."""


def notify_success(context, result):
    """Fired on successful materialization. In prod: ping slack, mark greenboard."""
    context.log.info(f"[hook.notify_success] asset materialized OK; result_type={type(result).__name__}")


def notify_failure(context, exc):
    """Fired on failure. In prod: page oncall, open jira ticket."""
    context.log.error(f"[hook.notify_failure] asset FAILED with {type(exc).__name__}: {exc}")


def audit_log(context, result):
    """Second success hook — proves multiple hooks fire in order."""
    context.log.info(f"[hook.audit_log] compliance/audit log entry written")
PY

# ═══ SHAPE 1a: PYTHON DECORATOR — SUCCESS PATH ══════════════════════════
cat > "$DEFS/py_success.py" <<PY
"""SHAPE 1a — @on_hooks on a @dg.asset that succeeds."""
import dagster as dg
from dagster_community_components import on_hooks


@dg.asset(group_name="python_decorator")
@on_hooks(
    on_success=["$PKG.hooks:notify_success", "$PKG.hooks:audit_log"],
    on_failure=["$PKG.hooks:notify_failure"],
)
def py_success(context):
    context.log.info("[py_success] compute ran")
    return {"status": "ok", "rows": 42}
PY

# ═══ SHAPE 1b: PYTHON DECORATOR — FAILURE PATH ═══════════════════════════
cat > "$DEFS/py_failure.py" <<PY
"""SHAPE 1b — @on_hooks on a @dg.asset that raises."""
import dagster as dg
from dagster_community_components import on_hooks


@dg.asset(group_name="python_decorator")
@on_hooks(
    on_success=["$PKG.hooks:notify_success"],
    on_failure=["$PKG.hooks:notify_failure"],
)
def py_failure(context):
    context.log.info("[py_failure] compute about to raise")
    raise RuntimeError("simulated compute failure — on_failure hook should fire")
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# HooksAssetComponent WRAPS another DCC component. After the inner runs
# cleanly, the on_success callbacks fire (in list order) with (context,
# result_of_inner_compute). No Python required for this asset — pure
# YAML composition; the callback references are the only Python.

mkdir -p "$DEFS/yaml_customers"
cat > "$DEFS/yaml_customers/defs.yaml" <<YAML
type: dagster_community_components.HooksAssetComponent
attributes:
  on_success:
    - "$PKG.hooks:notify_success"
    - "$PKG.hooks:audit_log"
  on_failure:
    - "$PKG.hooks:notify_failure"
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_customers
      schema_type: customers
      row_count: 100
      random_state: 42
      group_name: yaml_component
YAML

echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

_run() {
  local n="$1"; local asset="$2"; local expect="$3"; local allow_fail="${4:-}"
  echo ""
  echo ">>> RUN $n  ($asset) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  if [ -n "$allow_fail" ]; then
    uv run dg launch --assets "$asset" >"$LOG" 2>&1 || true
  else
    uv run dg launch --assets "$asset" >"$LOG" 2>&1
  fi
  { grep -E '\[hook\.|\[hooks\]|\[py_success\]|\[py_failure\]|\[hooks wrap\]|Generated DataFrame|STEP_SUCCESS|STEP_FAILURE' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1a: PYTHON DECORATOR — SUCCESS PATH (2 on_success hooks fire) ═══"
_run 1 py_success "OK — compute runs; notify_success + audit_log both fire"

echo ""
echo "═══ SHAPE 1b: PYTHON DECORATOR — FAILURE PATH (on_failure hook fires) ═══"
_run 2 py_failure "STEP_FAILURE — notify_failure hook fires; original error re-raised" allow_fail

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (HooksAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 3 yaml_customers "OK — inner data-gen runs; on_success hooks fire with the DataFrame result"

cat <<DONE

✓ hooks_asset demo done.

Two shapes, same on_success/on_failure callback wiring:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_success.py + py_failure.py
      @dg.asset + @on_hooks(on_success=[...], on_failure=[...])
      Signatures:
        on_success(context, result) -> None   (fires after successful return)
        on_failure(context, exception) -> None (fires on raise; then re-raised)
      Callback exceptions are LOGGED (not re-raised) — hooks NEVER change
      the compute's outcome (matches Prefect semantics).

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_customers/defs.yaml
      HooksAssetComponent { on_success/on_failure: [...], wraps: SyntheticDataGeneratorComponent { ... } }
      → No Python for this asset — one component stacked on another. The
        outer hooks intercept the inner's return / exception and route
        each to the configured callbacks.

  Both shapes reference callbacks the same way: \`<module>:<function>\`.
  Shared \`src/$PKG/hooks.py\` — one authoring surface for the whole team.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: HooksAssetComponent{wraps:SlaAssetComponent{wraps:X}}
= @on_hooks @sla @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py_success)      → notify_success + audit_log both fired
                            (multiple hooks fire in list order)
  RUN 2 (py_failure)      → notify_failure fired, then RuntimeError
                            re-raised — asset STEP_FAILURE as expected
  RUN 3 (yaml_customers)  → inner SyntheticDataGen ran (100 customer
                            rows); notify_success + audit_log both fired
                            with the DataFrame result

Best fits: asset-scoped success/failure callbacks that live NEXT TO the
asset. Dagster's built-in hooks are job-scoped (attached in job wiring
off to the side) — this is the asset-first equivalent.

Cleanup: rm -rf $PROJECT_ABS
DONE
