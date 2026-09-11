#!/usr/bin/env bash
# sensitive_asset — @sensitive decorator + SensitiveAssetComponent.wraps: composability.
#
# Fully offline. Demonstrates BOTH shapes of the DCC decorator API:
#
#   SHAPE 1: @sensitive decorator — wraps a plain @dg.asset (Python)
#            PII / secret redaction on `context.log.*` calls +
#            `MaterializeResult.metadata` BEFORE the event log persists them.
#
#   SHAPE 2: SensitiveAssetComponent { wraps: SyntheticDataGeneratorComponent } —
#            YAML-composed. The redactor proxies the inner component's
#            context.log so any secrets the inner logs get scrubbed too.
#
# Both shapes emit the SAME sensitive_redacted_count observations.
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-sensitive-asset-demo}"
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
# @sensitive on a plain @dg.asset. Best when you already have Python code
# that logs sensitive fields. The decorator proxies context.log so keys
# matching the patterns are redacted BEFORE the log line lands in the
# event log — not after.

cat > "$DEFS/py_user_export.py" <<'PY'
"""SHAPE 1 — @sensitive wraps a @dg.asset directly.

The redactor scans structured `key=value` fragments in log messages +
matches configured key patterns. Below, `ssn`, `password`, and `*_token`
patterns are all triggered. The scrubbed log line is what Dagster's
event log ends up storing.
"""
import dagster as dg
from dagster_community_components import sensitive


@dg.asset(group_name="python_decorator")
@sensitive(keys=["password", "*_token", "ssn"], strategy="redact")
def py_user_export(context):
    # These key=value fragments will all be scrubbed in the event log:
    context.log.info(
        "user secrets — ssn=123-45-6789 password=hunter2 api_token=sk-abc123"
    )
    context.log.info("public field row_count=42")
    return dg.MaterializeResult(
        metadata={
            "ssn": "123-45-6789",       # → [REDACTED]
            "password": "hunter2",       # → [REDACTED]
            "row_count": 42,             # untouched
        }
    )
PY

# ═══ SHAPE 2: YAML COMPOSABILITY ═════════════════════════════════════════
# SensitiveAssetComponent WRAPS another DCC component. The outer component
# proxies the inner's context.log calls through the redactor and scrubs
# any MaterializeResult.metadata the inner returns. Zero Python for this
# asset — the redactor is defensive infrastructure that runs on every
# materialization even when the inner has nothing sensitive to scrub.

mkdir -p "$DEFS/yaml_user_export"
cat > "$DEFS/yaml_user_export/defs.yaml" <<'YAML'
# The OUTER SensitiveAssetComponent proxies the inner component's
# context.log calls through the redactor. This inner component doesn't
# actually LOG any secrets, so redacted_count=0 — but the observation is
# still emitted every run. In production the shape is:
#   SensitiveAssetComponent { wraps: RestApiFetcherComponent { ... } }
# where any secret the inner accidentally logs gets scrubbed before the
# event log persists it — SOC2-audit-worthy per-asset attestation.
type: dagster_community_components.SensitiveAssetComponent
attributes:
  keys:
    - password
    - "*_token"
    - ssn
    - authorization
    - api_key
  strategy: redact
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_user_export
      schema_type: customers
      row_count: 10
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
  echo "    ---- redacted log lines: ----"
  { grep -E 'processing user|non-sensitive|Generated DataFrame|REDACTED|STEP_SUCCESS' "$LOG" || true; } | sed 's/^/    /'
}

echo ""
echo "═══ SHAPE 1: PYTHON DECORATOR (@sensitive on @dg.asset) ═══"
_run 1 py_user_export "log line shows ssn/password/api_token replaced with [REDACTED]"

echo ""
echo "═══ SHAPE 2: YAML COMPOSABILITY (SensitiveAssetComponent wraps SyntheticDataGeneratorComponent) ═══"
_run 2 yaml_user_export "inner has nothing sensitive to log → redacted_count=0, but observation IS emitted (audit proof the redactor was active)"

echo ""
echo ">>> Sensitive observations — proof both shapes emit the same events:"
DAGSTER_HOME="$DAGSTER_HOME" uv run python - <<'PY'
import dagster as dg
from dagster import DagsterInstance

with DagsterInstance.get() as inst:
    print(f"    {'asset':<20}  {'redacted_count':<16}  strategy")
    print(f"    {'-----':<20}  {'--------------':<16}  --------")
    for asset_name in ("py_user_export", "yaml_user_export"):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=20,
        ).records))
        if not recs:
            print(f"    {asset_name:<20}  (no sensitive observations)")
            continue
        for r in recs:
            obs = r.asset_observation
            if not obs: continue
            tags = dict(obs.tags or {})
            meta = {k: v.value for k, v in (obs.metadata or {}).items()}
            count = tags.get("sensitive_redacted_count", "-")
            strat = str(meta.get("sensitive_strategy", "-"))
            print(f"    {asset_name:<20}  {count:<16}  {strat}")
PY

cat <<DONE

✓ sensitive_asset demo done.

Two shapes, same log-proxy redaction primitive:

  ─ SHAPE 1: Python decorator
      src/$PKG/defs/py_user_export.py
      @dg.asset + @sensitive(keys=['password', '*_token', 'ssn'], strategy='redact')

  ─ SHAPE 2: YAML composability  ← the money shot
      src/$PKG/defs/yaml_user_export/defs.yaml
      SensitiveAssetComponent { wraps: SyntheticDataGeneratorComponent { ... } }
      No Python — the outer proxies the inner's context.log calls through
      the redactor. Any secret an inner component accidentally logs is
      scrubbed BEFORE the event log persists it.

Composability is the "@decorator @dg.asset" idiom expressed as YAML.
Stacks arbitrarily deep: SensitiveAssetComponent{wraps:SnapshotAssetComponent{wraps:X}}
= @sensitive @snapshot @X-compute in Python decorator terms.

What just happened:
  RUN 1 (py)   → 'user secrets — ssn=[REDACTED] password=[REDACTED] api_token=[REDACTED]'
                 (row_count=42 log line untouched — no pattern match)
                 metadata 'ssn' + 'password' keys also replaced with [REDACTED]
                 → redacted_count = matched fields (log + metadata)
  RUN 2 (yaml) → inner data-gen has nothing sensitive to log → redacted_count=0
                 → observation IS emitted every run — audit proof the redactor was active
                 → in production: SensitiveAssetComponent { wraps: RestApiFetcherComponent }
                   catches any secret the inner accidentally logs

Redaction strategies:
  - redact (default) → [REDACTED]
  - hash             → sha256:XXXXXXXX (deterministic for pattern-matching)
  - mask             → ***last4

Fits SOC2 audit playbook: per-asset attestation via
sensitive_redacted_count observation on every run.

Cleanup: rm -rf $PROJECT_ABS
DONE
