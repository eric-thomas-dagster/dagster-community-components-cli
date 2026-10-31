#!/usr/bin/env bash
# setup_agent_governance_demo.sh
#
# An agent proposes an action; a QUORUM of humans (not just one) must sign
# off before it ships; every decision is permanently audited; and a
# REJECTION produces real, structured feedback the next attempt can act on
# -- not just a dead end.
#
#   deployment_proposal (agentic_pipeline, real LLM call)
#         |
#         v
#   deployment_gate (multi_approver_gate, 2-of-3 quorum, vote-by-file-drop)
#         |                                   |
#    (approved)                          (rejected + feedback)
#         |                                   |
#         v                                   v
#   [ships]                    rejection_feedback_loop sensor:
#                                 writes .feedback/<key>.txt per rejecter,
#                                 triggers regenerate_deployment_proposal job
#
#   deployment_approval_audit (approval_audit_asset) reads the same
#   approval_dir -- one row per vote, who/when/why/feedback.
#
# This demo runs the real happy path AND the real rejection path, with
# votes dropped as files (the real vote-by-file-drop protocol every
# approval-gate component in this registry shares -- no UI, no Slack/Teams
# webhook needed for the demo).
#
# IMPORTANT gotchas found and documented here (not guessed):
#   1. multi_approver_gate derives the voter's identity from the FILENAME
#      (<key>.<approver_id>.json) and does not require an "approver" field
#      in the body. approval_audit_asset (designed around
#      HumanApprovalGateComponent's token shape) reads "approver" from the
#      BODY instead. Chaining the two means your vote files need BOTH --
#      see the defs.yaml below.
#   2. multi_approver_gate writes a `<key>.multi_state.json` sidecar (its
#      timeout clock) into the same approval_dir. approval_audit_asset's
#      default token_glob ("*.json") picks that up as a spurious row --
#      set token_glob to something that only matches real vote files (here,
#      "*@*.json", since every approver_id in this demo is an email).
#   3. A real crash was found in rejection_feedback_loop while building this
#      demo: its internal file-move helper called `.rsplit()` on a
#      `pathlib.Path` (a str-only method) when moving a consumed rejection
#      token -- it has been fixed upstream in dagster-component-templates.
#      If `dagster-component add rejection_feedback_loop` still pulls the
#      broken version, the sensor will throw AttributeError on its first
#      real rejection tick; update to the latest component source.
#
# What it demonstrates
#   • agentic_pipeline — single llm_call step drafts a deployment decision memo
#   • multi_approver_gate — 2-of-3 quorum sign-off, vote-by-file-drop
#   • approval_audit_asset — full who/when/why/feedback audit trail
#   • asset_job — simple re-run target for a rejected proposal
#   • rejection_feedback_loop — sensor that turns a rejection into reusable feedback
#
# Cost: ~$0.0001/run — one gpt-4o-mini call. Everything else is free/local.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • OPENAI_API_KEY (or set a different model/api_key_env_var in defs.yaml)
#
# Usage
#   ./setup_agent_governance_demo.sh                  # → agent_governance_demo/
#   ./setup_agent_governance_demo.sh my_project        # custom name

set -eo pipefail

PROJECT_NAME="${1:-agent_governance_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
APPROVAL_DIR="${PROJECT_DIR}/.approvals"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"
[ -z "$OPENAI_API_KEY" ] && fail "OPENAI_API_KEY not set. export OPENAI_API_KEY=sk-..."

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"
export DAGSTER_HOME="${PROJECT_DIR}/.dagster_home"
mkdir -p "$DAGSTER_HOME"

info "Installing deps (pandas, litellm for the agent step)…"
uv add -q pandas litellm
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

info "Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add agentic_pipeline --auto-install
$CLI add multi_approver_gate --auto-install
$CLI add approval_audit_asset --auto-install
$CLI add asset_job --auto-install
$CLI add rejection_feedback_loop --auto-install

info "Writing defs.yaml (propose → quorum gate → audit + feedback loop)"

cat > "src/$PKG/defs/agentic_pipeline/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: deployment
  group_name: agent
  source:
    kind: literal
    text: >-
      Candidate model v3.2 metrics: accuracy 94.2%, p95 latency 180ms,
      cost \$0.0020/request. Current production model: accuracy 92.1%,
      p95 latency 210ms, cost \$0.0015/request.
  steps:
    - id: proposal
      op: llm_call
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: "You are an ML platform engineer drafting a deployment decision memo for a two-reviewer sign-off. Be concise (3-4 sentences): recommend promote or hold, and why."
  outputs:
    assets: [proposal]
EOF

cat > "src/$PKG/defs/multi_approver_gate/defs.yaml" <<EOF
type: $PKG.components.multi_approver_gate.component.MultiApproverGateComponent
attributes:
  asset_name: deployment_gate
  upstream_asset_key: deployment_proposal
  approval_dir: "${APPROVAL_DIR}"
  required_approvers: 2
  approver_allowlist:
    - alice@example.com
    - bob@example.com
    - carol@example.com
  group_name: governance
  description: "Two-of-three sign-off before the proposed model deployment passes through."
EOF

cat > "src/$PKG/defs/approval_audit_asset/defs.yaml" <<EOF
type: $PKG.components.approval_audit_asset.component.ApprovalAuditAssetComponent
attributes:
  asset_name: deployment_approval_audit
  approval_dir: "${APPROVAL_DIR}"
  # See gotcha #2 above -- excludes multi_approver_gate's own *.multi_state.json sidecar.
  token_glob: "*@*.json"
  group_name: governance
  description: "Full who/when/why audit trail of deployment-gate votes."
EOF

cat > "src/$PKG/defs/asset_job/defs.yaml" <<EOF
type: $PKG.components.asset_job.component.AssetJobComponent
attributes:
  job_name: regenerate_deployment_proposal
  asset_keys: [deployment_proposal]
  description: "Re-runs the agent's proposal step after a rejection with feedback."
EOF

cat > "src/$PKG/defs/rejection_feedback_loop/defs.yaml" <<EOF
type: $PKG.components.rejection_feedback_loop.component.RejectionFeedbackLoopComponent
attributes:
  sensor_name: deployment_feedback_loop
  approval_dir: "${APPROVAL_DIR}"
  target_job_name: regenerate_deployment_proposal
  max_iterations: 3
  minimum_interval_seconds: 30
EOF

ok "Wrote 5 defs.yaml"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Round 1: drafting the proposal, then checking the (empty) quorum"
uv run dg launch --assets 'deployment_proposal,deployment_gate,deployment_approval_audit' 2>&1 | grep -E "quorum|RUN_SUCCESS|RUN_FAILURE" || true

info "Simulating 2 human approvals (vote-by-file-drop -- see gotcha #1 above for why both 'approver' in the body AND the filename suffix matter)"
mkdir -p "$APPROVAL_DIR"
cat > "${APPROVAL_DIR}/default.alice@example.com.json" <<EOF
{"approved": true, "approver": "alice@example.com", "reason": "Accuracy gain outweighs the small cost/latency delta."}
EOF
cat > "${APPROVAL_DIR}/default.bob@example.com.json" <<EOF
{"approved": true, "approver": "bob@example.com", "reason": "Latency improved too -- clean win, approve."}
EOF

info "Round 2: re-checking the gate -- quorum should now be APPROVED"
uv run dg launch --assets 'deployment_gate,deployment_approval_audit' 2>&1 | grep -E "quorum|RUN_SUCCESS|RUN_FAILURE"

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran the full happy path:
  1. agentic_pipeline drafted a real deployment decision memo (gpt-4o-mini)
  2. multi_approver_gate started PENDING (0/2 votes)
  3. Two approval votes were dropped as files
  4. multi_approver_gate re-evaluated and opened — quorum (2/2) APPROVED
  5. approval_audit_asset shows both votes: who, when, why

Inspect it:
  cd $PROJECT_NAME
  uv run dg dev
    → click deployment_gate → Checks tab → quorum: PASSED
    → click deployment_approval_audit → see both approvers' reasons

Try the REJECTION path (this is the interesting half):
  rm ${APPROVAL_DIR}/default.*.json
  cat > ${APPROVAL_DIR}/default.carol@example.com.json <<JSON
  {"approved": false, "approver": "carol@example.com", "reason": "Cost delta needs finance sign-off first.", "feedback": "Add a cost-per-1k-requests comparison and a rollback plan before resubmitting."}
  JSON
  cat > ${APPROVAL_DIR}/default.bob@example.com.json <<JSON
  {"approved": false, "approver": "bob@example.com", "reason": "Agrees with carol.", "feedback": "See carol's note on cost comparison."}
  JSON
  uv run dg launch --assets 'deployment_gate,deployment_approval_audit'
    → quorum (2/2) REJECTED; deployment_approval_audit captures both
      rejections AND their feedback text

  Then let the sensor turn that into a real re-run (requires \`dg dev\`
  running so the sensor daemon is live, or evaluate it once directly):
    uv run dg dev
    → Sensors tab → deployment_feedback_loop → "Test Sensor"
    → writes ${APPROVAL_DIR}/.feedback/<approver>.txt per rejecter and
      triggers regenerate_deployment_proposal — the agent gets a second
      attempt with the humans' actual feedback to work from

Production variants:
  • Point approval_dir at s3://.../abfs://... for Dagster+ Serverless
    (the container filesystem is ephemeral)
  • Swap the single llm_call for agentic_pipeline's route/critique_loop/
    debate ops for a more sophisticated proposal
  • Wire the agent's next attempt to actually READ
    ${APPROVAL_DIR}/.feedback/<key>.txt (see rejection_feedback_loop's own
    README for the mcp_call wiring pattern)
EOF
