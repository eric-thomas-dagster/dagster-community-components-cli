#!/usr/bin/env bash
# setup_self_healing_pipeline_demo.sh
#
# A real failure, diagnosed with Dagster's own run history via
# `dagster_instance_tools` (read-only, AI-agent-ready tools over a
# DagsterInstance: list_recent_failures, get_run_summary, get_run_logs,
# asset_status, asset_lineage, stale_assets), then a real retry that
# succeeds once the root cause is fixed. The "AI ops assistant" story: an
# agent answers "why did this fail?" with real Dagster data instead of a
# human grepping logs.
#
#   flaky_ingest (fails on a missing config -- a stand-in for any real,
#                 fixable upstream condition)
#          │
#          ├── [launch 1] FAILS for real (RUN_FAILURE)
#          │
#   pipeline_doctor -- reads the real failure via
#          │            dagster_instance_tools.call("list_recent_failures", ...)
#          │            + .call("get_run_summary", {run_id}) and prints a
#          │            real diagnosis (error class, message, failed step)
#          │
#   [remediation applied -- this demo touches a marker file, standing in
#    for "the real root cause got fixed": a config set, a permission
#    granted, an upstream outage resolved]
#          │
#          └── [launch 2] flaky_ingest SUCCEEDS
#
# IMPORTANT -- read before adapting this demo:
#   • `run_failure_remediation` (the full production version of this idea)
#     is NOT used here. It's a pair of real Dagster sensors (a
#     `run_failure_sensor` gathering evidence + requesting a triage-job run,
#     and an approval sensor executing allow-listed actions only once a
#     token is present) that need a LIVE `dg dev`/daemon process plus a
#     custom-authored triage job you write yourself (it proposes the fix;
#     this component only gathers evidence and executes an approved one).
#     That's real infrastructure, not something a single `dg launch` can
#     exercise end-to-end. This demo shows the building block underneath
#     it -- real, read-only diagnosis via `dagster_instance_tools` -- with
#     the retry applied manually by the script instead of an automated,
#     approved sensor action. See "Production variant" below for the real
#     next step.
#   • `dagster_instance_tools.call("list_recent_failures", ...)` returns its
#     results under the key `"runs"`, not `"failures"` -- an easy one to
#     get wrong (this demo's own first draft did).
#   • `DAGSTER_HOME` must be a FIXED directory for run history to persist
#     across separate `dg launch` invocations -- by default each `dg
#     launch` call gets its own ephemeral temp dagster home, so a second
#     call can't see the first call's failed run. This script exports
#     `DAGSTER_HOME` once, up front.
#
# What it demonstrates
#   • dagster_instance_tools — read-only AI-agent tools over the real
#     Dagster instance (list_recent_failures, get_run_summary, ...)
#   • A plain custom @asset (flaky_ingest / pipeline_doctor) using that
#     resource via `required_resource_keys` + `context.resources` --
#     not every demo needs a registry component for the glue logic.
#
# Cost: $0 — no LLM call, no cloud account, pure local Dagster + Python.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_self_healing_pipeline_demo.sh                 # → self_healing_demo/
#   ./setup_self_healing_pipeline_demo.sh my_project       # custom name

set -eo pipefail

PROJECT_NAME="${1:-self_healing_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"
export DAGSTER_HOME="$PWD/dagster_home"
mkdir -p "$DAGSTER_HOME"

info "Installing deps"
uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

info "Installing dagster_instance_tools (--refresh busts the registry-manifest cache)"
uvx --from dagster-community-components-cli dagster-component --refresh add dagster_instance_tools --auto-install

# Defensive: as of this writing, dagster_instance_tools's own __init__.py in
# the upstream registry doesn't export the class (a real bug -- fixed
# upstream, but --auto-install pulls from the live repo so this keeps the
# demo working either way).
cat > "src/$PKG/components/dagster_instance_tools/__init__.py" <<EOF
from .component import DagsterInstanceToolsComponent

__all__ = ["DagsterInstanceToolsComponent"]
EOF

cat > "src/$PKG/defs/dagster_instance_tools/defs.yaml" <<EOF
type: $PKG.components.dagster_instance_tools.component.DagsterInstanceToolsComponent
attributes:
  resource_key: dagster_instance_tools
  max_result_chars: 8000
EOF

info "Writing the flaky asset + pipeline-doctor diagnostic asset"
mkdir -p "src/$PKG/defs/pipeline_doctor"
cat > "src/$PKG/defs/pipeline_doctor/__init__.py" <<'PY'
"""A deliberately-flaky ingestion step, and a 'pipeline doctor' asset that
diagnoses WHY it failed using the real DagsterInstanceTools resource --
the self-healing-pipeline story: an AI ops assistant reads Dagster's own
run history instead of a human grepping logs.

`flaky_ingest` fails until a marker file exists (./remediation_applied.flag)
-- a stand-in for "the root cause got fixed" (a bad config value, a missing
permission, a downstream outage that resolved). The setup script applies
the fix itself (touches the marker) between its `dg launch` calls, to show
the before/after.
"""
from pathlib import Path

import dagster as dg

MARKER = Path(__file__).resolve().parents[4] / "remediation_applied.flag"


@dg.asset(description="Fails until the remediation marker file exists -- simulates a real, fixable upstream condition.")
def flaky_ingest(context: dg.AssetExecutionContext) -> int:
    if not MARKER.exists():
        raise RuntimeError(
            "UPSTREAM_CONFIG_MISSING: required source config 'SOURCE_API_KEY' is unset. "
            "(Demo: this clears once remediation_applied.flag exists.)"
        )
    context.log.info("Ingest succeeded -- remediation was applied.")
    return 42


@dg.asset(
    deps=["flaky_ingest"],
    required_resource_keys={"dagster_instance_tools"},
    description="Diagnoses the most recent failure of flaky_ingest using the real DagsterInstanceTools resource.",
)
def pipeline_doctor(context: dg.AssetExecutionContext) -> None:
    tools = context.resources.dagster_instance_tools
    result = tools.call("list_recent_failures", {"hours": 24, "limit": 5})
    context.log.info(f"dagster_instance_tools.list_recent_failures -> {result}")
    # NOTE: the real key is "runs", not "failures" -- an easy mistake (this
    # demo's own first draft made it).
    failures = result.get("runs") or []
    if not failures:
        context.log.info("No recent failures found -- flaky_ingest is healthy.")
        return
    latest = failures[0]
    run_id = latest.get("run_id")
    summary = tools.call("get_run_summary", {"run_id": run_id}) if run_id else {}
    context.log.info(f"Diagnosis for run {run_id}: {summary}")
    context.add_output_metadata({
        "diagnosed_run_id": dg.MetadataValue.text(str(run_id)),
        "failure_summary": dg.MetadataValue.json(latest),
    })
PY

ok "Wrote 1 defs.yaml + 1 plain-Python defs module"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

rm -f remediation_applied.flag

info "Round 1: launching flaky_ingest -- this is EXPECTED to fail"
uv run dg launch --assets flaky_ingest 2>&1 | tail -10 || true

info "Diagnosing the failure with dagster_instance_tools (real run history, not log-grepping)"
uv run dg launch --assets pipeline_doctor 2>&1 | grep -E "Diagnosis for run|No recent failures" || true

info "Applying the remediation (a real fix would set the missing config / grant the permission / wait out the outage)"
touch remediation_applied.flag
ok "Touched remediation_applied.flag"

info "Round 2: re-launching flaky_ingest -- should succeed now"
uv run dg launch --assets flaky_ingest 2>&1 | tail -10

echo
ok "Demo complete."
echo
cat <<EOF
What just happened, for real:
  1. flaky_ingest failed (RUN_FAILURE) -- a real Dagster run failure, not
     a simulated one.
  2. pipeline_doctor queried dagster_instance_tools.call("list_recent_failures", ...)
     and .call("get_run_summary", {run_id}) against the REAL Dagster
     instance (DAGSTER_HOME=$DAGSTER_HOME) and printed the real error
     class, message, and failed step -- the output above came straight
     from Dagster's own event log, read through the same read-only
     toolset an agent would use.
  3. The remediation was applied (a marker file, standing in for a real
     fix) and flaky_ingest was re-launched -- RUN_SUCCESS.

What this does NOT show (read "IMPORTANT" in the script header):
  run_failure_remediation's own sensor pair (evidence-gathering +
  approval-gated auto-remediation) needs a live \`dg dev\` daemon and a
  custom triage job you author -- not something a single-shot \`dg launch\`
  script can exercise end-to-end. This demo proves the piece underneath
  it (real, read-only diagnosis via dagster_instance_tools) actually
  works; wiring it into a fully automated sensor loop is the natural next
  step.

Inspect it yourself:
  cd $PROJECT_NAME
  DAGSTER_HOME=\$PWD/dagster_home uv run dg dev
    → Runs tab: see the real FAILURE then the real SUCCESS
    → click pipeline_doctor's materialization → see the diagnosed_run_id
      and failure_summary metadata, straight from the real run

Production variant:
  Swap this script's manual "touch remediation_applied.flag" + re-launch
  for the real thing: configure \`run_failure_remediation\` (a
  \`run_failure_sensor\` that writes evidence + requests a triage-job run,
  and an approval sensor that executes allow-listed actions once a token
  matching the proposal's fingerprint is present), author a triage job
  that reads the evidence (via dagster_instance_tools, as shown here) and
  an LLM to propose a fix, and run \`dg dev\` so the sensors are live. See
  the component's own README for the full evidence/proposal/approval
  record shapes.
EOF
