#!/usr/bin/env bash
# log_prints_asset — @log_prints decorator + LogPrintsAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @log_prints decorator — wraps a plain @dg.asset (Python)
#            print() calls in the compute get captured into context.log.
#   SHAPE 2: LogPrintsAssetComponent { wraps: SyntheticDataGeneratorComponent }
#            YAML-composed. The outer log_prints component wraps the inner
#            component's compute — any stdout the inner (or any lib it
#            calls) writes is captured verbatim into the Dagster log.
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-log-prints-asset-demo}"
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
# @log_prints on a plain @dg.asset. Best when you're porting old scripts
# where visibility comes from print() rather than a real logger.

cat > "$DEFS/py_ported_script.py" <<'PY'
"""SHAPE 1 — @log_prints wraps a @dg.asset directly.

Legacy Python scripts often use print() for visibility. @log_prints
redirects sys.stdout to context.log.info line-by-line — no rewrite
required. Each captured line becomes a real Dagster log event.
"""
import dagster as dg
from dagster_community_components import log_prints


@dg.asset(group_name="python_decorator")
@log_prints(prefix="[legacy] ")
def py_ported_script(context):
    print("hello world")
    print("starting the ported job")
    print(f"processed {123} rows")
    print("done")
    return {"status": "ok"}
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# LogPrintsAssetComponent WRAPS another DCC component. Any stdout the
# inner component (or a lib it calls — pandas warnings, requests
# redirects, tqdm progress bars, print() debug statements) writes gets
# captured line-by-line into context.log.info. Zero Python for this asset.

mkdir -p "$DEFS/yaml_customers"
cat > "$DEFS/yaml_customers/defs.yaml" <<'YAML'
# The OUTER LogPrintsAssetComponent wraps another DCC component's
# compute with a stdout-to-context.log redirect. The INNER component
# is SyntheticDataGeneratorComponent — if it ever writes to stdout
# (or any lib underneath does), those lines land in the Dagster log
# with the prefix below. Without any print() to capture, the wrap is
# a no-op (the inner runs unchanged); with print(), every line is
# visible in the run log panel.
type: dagster_community_components.LogPrintsAssetComponent
attributes:
  prefix: "[wrapped] "
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
  local n="$1"; local asset="$2"; local expect="$3"
  echo ""
  echo ">>> RUN $n  ($asset) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  uv run dg launch --assets "$asset" >"$LOG" 2>&1
  { grep -E '\[legacy\]|\[wrapped\]|Generated DataFrame|STEP_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@log_prints on @dg.asset) ═══"
_run 1 py_ported_script "OK — 4 print() lines captured with [legacy] prefix"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (LogPrintsAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 2 yaml_customers "OK — inner runs; wrap is a no-op if inner doesn't print, but any stdout from inner or its deps would be captured with [wrapped] prefix"

cat <<DONE

✓ log_prints_asset demo done.

Two shapes, same stdout-redirect primitive:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_ported_script.py
      @dg.asset + @log_prints(prefix='[legacy] ')
      → Every print() line becomes a real Dagster log event
        (searchable by run_id, visible in the UI's log panel).

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_customers/defs.yaml
      LogPrintsAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } }
      → No Python — one component stacked on another. If the inner
        component (or any lib it calls) writes to stdout, every line
        is captured verbatim into context.log.info with the configured
        prefix. When the inner is quiet (as SyntheticDataGenerator is),
        the wrap is a zero-cost no-op — the inner runs unchanged.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: LogPrintsAssetComponent{wraps:CachedAssetComponent{wraps:X}}
= @log_prints @cached @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py)   → 4 print() lines from py_ported_script showed up in the
                 log with the [legacy] prefix — no rewrite required
  RUN 2 (yaml) → inner SyntheticDataGen ran cleanly; wrap intercepted
                 stdout (would capture any print() the inner emitted;
                 SyntheticDataGen itself uses context.log.info directly
                 so there's nothing on stdout to capture — the wrap is a
                 transparent no-op in that case)

Best fits: porting Python scripts that use print() for visibility
without rewriting them. Also useful for third-party libs that print
warnings/progress to stderr/stdout — those get folded into the run log
instead of leaking to the process's terminal.

Cleanup: rm -rf $PROJECT_ABS
DONE
