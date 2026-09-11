#!/usr/bin/env bash
# smart_retry — @smart_retry decorator + SmartRetryComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @smart_retry decorator — wraps a plain @dg.asset (Python)
#            + shared state file so `flaky` mode succeeds only after
#            a couple of transient retries.
#            Three runs: success / flaky / permanent — exercises all
#            three classification paths.
#   SHAPE 2: SmartRetryComponent { wraps: SyntheticDataGeneratorComponent } —
#            YAML-composed. The outer SmartRetryComponent wraps ANOTHER
#            component's compute with retry classification. The inner
#            data-gen won't fail, so this demonstrates that the wrap
#            loads + runs cleanly (retry logic layers on top; inner
#            failures WOULD trigger classification).
#
# Both shapes share the SAME classification engine (transient →
# RetryRequested → real Dagster step restart; permanent → dg.Failure
# immediately, no retry).
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-smart-retry-demo}"
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
# @smart_retry on a plain @dg.asset with a state file that persists
# across step-restart attempts within one run — used so `flaky` mode
# succeeds on the 3rd attempt.

cat > "$DEFS/py_api_call.py" <<'PY'
"""SHAPE 1 — @smart_retry wraps a @dg.asset directly."""
import os
import dagster as dg
from dagster_community_components import smart_retry

STATE_FILE = os.environ.get("RETRY_STATE_FILE", "/tmp/smart_retry_demo_state.txt")


def _read_attempt() -> int:
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except FileNotFoundError:
        return 0
    except Exception:
        return 0


def _write_attempt(n: int) -> None:
    with open(STATE_FILE, "w") as f:
        f.write(str(n))


@dg.asset(group_name="python_decorator")
@smart_retry(
    rules=[
        {"kind": "exception_class",
         "transient": ["ConnectionError", "TimeoutError"],
         "permanent": ["ValueError", "KeyError"]},
    ],
    max_attempts=5,
    backoff="fixed",
    initial_delay_seconds=0.2,
    max_delay_seconds=0.2,
    jitter=False,
    key="py_api_call_retry",
)
def py_api_call(context) -> dict:
    mode = os.environ.get("MODE", "success")
    attempt = _read_attempt() + 1
    _write_attempt(attempt)
    context.log.info(f"[py_api_call] MODE={mode}  attempt={attempt}")

    if mode == "success":
        _write_attempt(0)
        return {"ok": True, "attempts_needed": 1}
    if mode == "flaky":
        if attempt < 3:
            raise ConnectionError(f"transient failure on attempt {attempt}")
        _write_attempt(0)
        return {"ok": True, "attempts_needed": attempt}
    if mode == "permanent":
        raise ValueError("permanent failure — never retry")
    raise RuntimeError(f"unknown MODE={mode!r}")
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# SmartRetryComponent WRAPS another DCC component. The outer component
# adds classification-aware retry around the inner component's compute
# — no Python required for this asset.
#
# The inner SyntheticDataGeneratorComponent won't fail on its own, so
# this run demonstrates the wrap LOADS + RUNS CLEANLY (retry logic
# layers over inner compute). If the inner were, e.g., a
# `rest_api_fetcher` that hit a 503, the outer SmartRetryComponent
# would classify it TRANSIENT via the http_status rule and issue a
# RetryRequested; a 404 would be classified PERMANENT and raise
# dg.Failure immediately.

mkdir -p "$DEFS/yaml_api_call"
cat > "$DEFS/yaml_api_call/defs.yaml" <<'YAML'
# The OUTER SmartRetryComponent wraps another DCC component's compute
# with the smart_retry primitive. The INNER component generates 500
# synthetic customer rows. Data-gen won't fail on its own, so this
# demonstrates the wrap loads + runs cleanly — retry logic layers on
# top; inner failures WOULD trigger classification.
type: dagster_community_components.SmartRetryComponent
attributes:
  retry_rules:
    - kind: http_status
      transient_codes: [429, 500, 502, 503, 504]
      permanent_codes: [400, 401, 403, 404, 422]
    - kind: exception_class
      transient:
        - ConnectionError
        - TimeoutError
      permanent:
        - ValueError
        - KeyError
  retry_policy:
    max_attempts: 5
    backoff: fixed
    initial_delay_seconds: 0.2
    max_delay_seconds: 0.2
    jitter: false
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_api_call
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

# Fresh state per demo run
export RETRY_STATE_FILE="$PROJECT_ABS/.retry_state.txt"

_run() {
  local n="$1"; local asset="$2"; local expect="$3"; local expect_fail="${4:-false}"
  local extra_env="${5:-}"
  echo ""
  echo ">>> RUN $n  ($asset $extra_env) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  rm -f "$RETRY_STATE_FILE"
  if [ "$expect_fail" = "true" ]; then
    eval "$extra_env uv run dg launch --assets $asset" >"$LOG" 2>&1 || true
  else
    eval "$extra_env uv run dg launch --assets $asset" >"$LOG" 2>&1
  fi
  { grep -E '\[py_api_call\]|\[smart_retry\]|classified as|PERMANENT|attempt |Generated|SyntheticData|RUN_(SUCCESS|FAILURE)|STEP_SUCCESS|Failure' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@smart_retry on @dg.asset — 3 modes) ═══"
_run 1 py_api_call "1 attempt → success"           false "MODE=success"
_run 2 py_api_call "2× ConnectionError (transient) → retries → succeeds on attempt 3"  false "MODE=flaky"
_run 3 py_api_call "1× ValueError (permanent) → fails immediately, no retry"  true  "MODE=permanent"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (SmartRetryComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 4 yaml_api_call "inner data-gen won't fail → wrap loads + runs cleanly (retry layered on top)"

cat <<DONE

✓ smart_retry demo done.

Two shapes, same classification-aware retry primitive:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_api_call.py
      @dg.asset + @smart_retry(rules=[exception_class + http_status], ...)

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_api_call/defs.yaml
      SmartRetryComponent { wraps: SyntheticDataGeneratorComponent { ... } }
      No Python — one component stacked on another. Add / remove /
      swap the outer decorator without touching the inner's config.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: SlaAssetComponent{wraps:SmartRetryComponent{wraps:X}}
= @sla @smart_retry @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py, MODE=success)   → 1 attempt, no retry logic engaged
  RUN 2 (py, MODE=flaky)     → attempts 1, 2 raise ConnectionError (classified TRANSIENT)
                                → smart_retry backs off + retries via Dagster step-restart;
                                  attempt 3 succeeds
  RUN 3 (py, MODE=permanent) → attempt 1 raises ValueError (classified PERMANENT)
                                → smart_retry raises dg.Failure immediately, no retry
  RUN 4 (yaml)               → inner SyntheticDataGeneratorComponent runs cleanly.
                                The wrap is fully loaded — if the inner had raised, say,
                                a ConnectionError, the outer would have classified TRANSIENT
                                and issued a RetryRequested. If it had raised ValueError,
                                classified PERMANENT and raised dg.Failure with no retry.

The value over Dagster's built-in RetryPolicy: **classification**. A
ValueError is not a network blip — retrying wastes time and money and
delays the real failure. smart_retry classifies via user-supplied rules
(exception_class, http_status, HTTP response body regex) so transient
faults get full retry treatment and permanent ones fail fast.

Browse in the UI:
  export DAGSTER_HOME=$DAGSTER_HOME
  cd $PROJECT_DIR
  uv run dg dev  # → http://localhost:3000 → api_call → run history

Try:
  - Wrap a real REST fetcher: SmartRetryComponent { wraps: RestApiFetcherComponent }
    → 429 → transient (retries); 404 → permanent (fails immediately).
  - Add llm_fallback={"model": "gpt-4o-mini", "api_key_env_var": "OPENAI_API_KEY"} for
    day-1 LLM-classifies unmatched exceptions (opt-in, off by default).
  - Add rate_limit={"max_events": 3, "window_seconds": 60, "mode": "fail"} → caps
    retries within a sliding window to prevent runaway spend.
  - Add circuit_breaker={"threshold": 5, "observation_window_seconds": 300, "cooldown_seconds": 60}
    → fails fast when the breaker is OPEN.

Cleanup: rm -rf $PROJECT_ABS
DONE
