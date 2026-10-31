#!/usr/bin/env bash
# setup_compliance_evidence_demo.sh
#
# A different flavor of agent governance than setup_agent_governance_demo.sh:
# compliance-specific, not generic agent-action approval. An LLM drafts SOC2
# evidence-response narratives for a set of controls; a human reviewer signs
# off before anything ships to an auditor; the decision is audited.
#
#   soc2_controls (inline_dataframe — 2 sample controls)
#         │
#         ▼
#   control_narratives (vanta_evidence_response_agent — real LLM drafts
#                        one evidence narrative per control)
#         │
#         ▼
#   evidence_review_gate (multi_approver_gate — 1-of-2 reviewer sign-off)
#         │
#         ▼
#   evidence_review_audit (approval_audit_asset — who approved, when, why)
#
# Same vote-by-file-drop protocol as setup_agent_governance_demo.sh and the
# same two multi_approver_gate / approval_audit_asset interop gotchas apply
# here (see that demo's header comment for the full explanation):
#   1. vote files need "approver" in the JSON body (not just the filename)
#      for approval_audit_asset to populate its approver column.
#   2. approval_audit_asset's token_glob is set to "*@*.json" so it doesn't
#      pick up multi_approver_gate's own <key>.multi_state.json sidecar.
#
# vanta_evidence_response_agent has no demo_mode — it always calls a real LLM.
#
# What it demonstrates
#   • inline_dataframe — 2 sample SOC2 controls (no synthetic-data preset fits "controls")
#   • vanta_evidence_response_agent — drafts one evidence narrative per control
#   • multi_approver_gate — 1-of-2 reviewer sign-off before evidence ships
#   • approval_audit_asset — full audit trail of the review decision
#
# Cost: ~$0.0002/run — two gpt-4o-mini calls (one per control row).
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • OPENAI_API_KEY
#
# Usage
#   ./setup_compliance_evidence_demo.sh                  # → compliance_evidence_demo/
#   ./setup_compliance_evidence_demo.sh my_project        # custom name

set -eo pipefail

PROJECT_NAME="${1:-compliance_evidence_demo}"
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

info "Installing deps (pandas, litellm for the evidence-drafting agent)…"
uv add -q pandas litellm
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

info "Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add inline_dataframe --auto-install
$CLI add vanta_evidence_response_agent --auto-install
$CLI add multi_approver_gate --auto-install
$CLI add approval_audit_asset --auto-install

info "Writing defs.yaml (controls → draft evidence → review gate → audit)"

cat > "src/$PKG/defs/inline_dataframe/defs.yaml" <<EOF
type: $PKG.components.inline_dataframe.component.InlineDataframeComponent
attributes:
  asset_name: soc2_controls
  columns: [control_id, description]
  rows:
    - ["CC6.1", "Logical access to production systems is restricted to authorized personnel."]
    - ["CC7.2", "Security incidents are detected, logged, and triaged within defined SLAs."]
  group_name: compliance
EOF

cat > "src/$PKG/defs/vanta_evidence_response_agent/defs.yaml" <<EOF
type: $PKG.components.vanta_evidence_response_agent.component.VantaEvidenceResponseAgentComponent
attributes:
  asset_name: control_narratives
  upstream_asset_key: soc2_controls
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  control_id_column: control_id
  control_description_column: description
  response_column: draft_response
  group_name: compliance
EOF

cat > "src/$PKG/defs/multi_approver_gate/defs.yaml" <<EOF
type: $PKG.components.multi_approver_gate.component.MultiApproverGateComponent
attributes:
  asset_name: evidence_review_gate
  upstream_asset_key: control_narratives
  approval_dir: "${APPROVAL_DIR}"
  required_approvers: 1
  approver_allowlist:
    - compliance-lead@example.com
    - security-lead@example.com
  group_name: compliance
  description: "One reviewer must sign off before drafted evidence responses ship to the auditor."
EOF

cat > "src/$PKG/defs/approval_audit_asset/defs.yaml" <<EOF
type: $PKG.components.approval_audit_asset.component.ApprovalAuditAssetComponent
attributes:
  asset_name: evidence_review_audit
  approval_dir: "${APPROVAL_DIR}"
  token_glob: "*@*.json"
  group_name: compliance
  description: "Who signed off on which drafted evidence response, and why."
EOF

ok "Wrote 4 defs.yaml"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Round 1: drafting evidence narratives, then checking the (empty) review gate"
uv run dg launch --assets '*' 2>&1 | grep -E "quorum|RUN_SUCCESS|RUN_FAILURE" || true

info "Simulating 1 reviewer sign-off"
mkdir -p "$APPROVAL_DIR"
cat > "${APPROVAL_DIR}/default.compliance-lead@example.com.json" <<EOF
{"approved": true, "approver": "compliance-lead@example.com", "reason": "Narratives accurately describe our real controls -- cleared to send to the auditor."}
EOF

info "Round 2: re-checking the gate -- should now be APPROVED (1-of-2 required)"
uv run dg launch --assets 'evidence_review_gate,evidence_review_audit' 2>&1 | grep -E "quorum|RUN_SUCCESS|RUN_FAILURE"

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. vanta_evidence_response_agent drafted one real evidence narrative per
     control (gpt-4o-mini) -- CC6.1 and CC7.2
  2. evidence_review_gate started PENDING (0/1 reviewers)
  3. One reviewer sign-off was dropped as a file
  4. evidence_review_gate re-evaluated and opened -- quorum (1/1) APPROVED
  5. evidence_review_audit shows the reviewer, timestamp, and reason

Inspect it:
  cd $PROJECT_NAME
  uv run dg dev
    → click control_narratives → see the real drafted evidence text
    → click evidence_review_gate → Checks tab → quorum: PASSED
    → click evidence_review_audit → the reviewer's sign-off

Try a rejection:
  rm ${APPROVAL_DIR}/default.*.json
  cat > ${APPROVAL_DIR}/default.security-lead@example.com.json <<JSON
  {"approved": false, "approver": "security-lead@example.com", "reason": "CC7.2 narrative doesn't mention our actual SLA numbers.", "feedback": "Add the 1-hour P1 / 24-hour P3 SLA figures from the incident runbook."}
  JSON
  uv run dg launch --assets 'evidence_review_gate,evidence_review_audit'
    → quorum (1/1) REJECTED; evidence_review_audit captures the rejection
      and the specific feedback for whoever redrafts

Production variants:
  • Replace inline_dataframe with a real Vanta API pull of your actual
    control set (see vanta_evidence_response_agent's README for the real
    upstream shape it expects: control_id + description columns)
  • Point approval_dir at cloud storage for Dagster+ Serverless
  • Pair with setup_agent_governance_demo.sh's rejection_feedback_loop
    sensor to auto-redraft rejected narratives with reviewer feedback
EOF
