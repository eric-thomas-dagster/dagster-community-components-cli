#!/usr/bin/env bash
# smart_retry — @smart_retry decorator: classification-aware retry
# (transient vs permanent) with exponential backoff.
#
# Fully offline. Demonstrates three shapes:
#   RUN 1 (MODE=success): 1 attempt → OK
#   RUN 2 (MODE=flaky):   raises ConnectionError (transient) 2× → succeeds on attempt 3
#   RUN 3 (MODE=permanent): raises ValueError (permanent) → FAILS on attempt 1 (no retry)
#
# The key insight over Dagster's built-in `RetryPolicy`: classification. A
# ValueError never gets retried because it's classified as permanent — no
# thrashing 5× while the underlying data is malformed. A ConnectionError
# gets full retry treatment because it's transient.
#
# 100% offline — no API keys, no external services.

set -eo pipefail

PROJECT_DIR="${1:-smart-retry-demo}"
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
# State file persists across attempts within one run (used by MODE=flaky
# to succeed on the 3rd attempt).
cat > "$DEFS/api_call.py" <<'PY'
"""@smart_retry — transient vs permanent classification, with a shared state file
so `flaky` mode succeeds only after a couple of transient retries."""
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


@dg.asset(group_name="retry_demo")
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
    key="api_call_retry",
)
def api_call(context) -> dict:
    mode = os.environ.get("MODE", "success")
    attempt = _read_attempt() + 1
    _write_attempt(attempt)
    context.log.info(f"[api_call] MODE={mode}  attempt={attempt}")

    if mode == "success":
        # Reset counter on success paths
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

# --- 5. dg check defs -----------------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -6; then
  echo "    ✗ dg check failed"; exit 1
fi

# Fresh state per demo run
export RETRY_STATE_FILE="$PROJECT_ABS/.retry_state.txt"

_run() {
  local n="$1"; local mode="$2"; local expect="$3"; local expect_fail="${4:-false}"
  echo ""
  echo ">>> RUN $n  (MODE=$mode) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  rm -f "$RETRY_STATE_FILE"
  if [ "$expect_fail" = "true" ]; then
    MODE="$mode" uv run dg launch --assets api_call >"$LOG" 2>&1 || true
  else
    MODE="$mode" uv run dg launch --assets api_call >"$LOG" 2>&1
  fi
  { grep -E '\[api_call\]|\[smart_retry\]|classified as|PERMANENT|attempt |RUN_(SUCCESS|FAILURE)|Failure' "$LOG" || true; } | sed 's/^/    /'
}

_run 1 success   "1 attempt → success"
_run 2 flaky     "2× ConnectionError (transient) → retries → succeeds on attempt 3"
_run 3 permanent "1× ValueError (permanent) → fails immediately, no retry" true

# --- 6. Explainer ---------------------------------------------------------
cat <<DONE

✓ smart_retry demo done.

What just happened:
  RUN 1 (MODE=success)   → 1 attempt, no retry logic engaged
  RUN 2 (MODE=flaky)     → attempts 1, 2 raise ConnectionError (classified TRANSIENT)
                            → smart_retry backs off + retries; attempt 3 succeeds
  RUN 3 (MODE=permanent) → attempt 1 raises ValueError (classified PERMANENT)
                            → smart_retry raises dg.Failure immediately, no retry

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
  - Add {"kind": "http_status", "transient_codes": [429, 500, 502, 503, 504]} to rules
    → HTTPError with those status codes gets transient treatment; 400/401/403/404 gets permanent.
  - Add llm_fallback={"model": "gpt-4o-mini", "api_key_env_var": "OPENAI_API_KEY"} for
    day-1 LLM-classifies unmatched exceptions (opt-in, off by default).
  - Add rate_limit={"max_events": 3, "window_seconds": 60, "mode": "fail"} → caps
    retries within a sliding window to prevent runaway spend.
  - Add circuit_breaker={"threshold": 5, "observation_window_seconds": 300, "cooldown_seconds": 60}
    → fails fast when the breaker is OPEN.

Cleanup: rm -rf $PROJECT_ABS
DONE
