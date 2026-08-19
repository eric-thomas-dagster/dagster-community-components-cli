#!/usr/bin/env bash
# slack_approval_gate — HITL approval via Slack reactions, composed
# with the existing HumanApprovalGateComponent.
#
# ## What this scaffolds
#
#   report_source          (synthetic report — AgenticPipelineComponent llm_call)
#         │
#         ▼
#   SlackApprovalGateComponent
#     ├── report_approval_slack_posted    → posts to Slack + records message_ts
#     └── report_approval_slack_watcher   → polls reactions, writes token on quorum
#         │
#         ▼ (token file appears in $APPROVAL_DIR)
#   HumanApprovalGateComponent
#     └── report_approval                 → reads token, `approved` asset check
#         │
#         ▼ (check passed=True)
#   AssetJobComponent + FilesystemMonitorSensorComponent
#     └── ship_report_job                 → sensor fires when token JSON drops
#
# ## Needs
#   - SLACK_BOT_TOKEN — a Slack bot user token (xoxb-...) with scopes
#       chat:write + reactions:read + reactions:write
#   - SLACK_APPROVER_USER_IDS — comma-separated Slack user IDs to allow-list
#     (e.g. "U1234ALICE,U5678BOB"). Get one from Slack: right-click user
#     → View profile → ⋮ More → Copy member ID.
#   - SLACK_CHANNEL — channel to post to (e.g. "#dagster-approvals" or
#     "C123ABC456" ID). Bot must be invited to the channel.
#   - OPENAI_API_KEY — for the synthetic report generation (~$0.001)
#   - uv
#
# ## Cost
#   ~$0.001 per run (OpenAI gpt-4o-mini for the report).
#
# ## Flow after materialization
#
#   1. Materialize `report_source` → generates report text
#   2. Materialize `report_approval_slack_posted` → posts to Slack, sensor
#      starts polling
#   3. Approvers react in Slack. Once quorum is reached, watcher sensor
#      writes token file. HumanApprovalGateComponent's check passes,
#      ship_report_job fires (or you materialize `report_approval`
#      manually).
#   4. Latency: 30-60s from last reaction to gate opening (poll interval).

set -eo pipefail

PROJECT_DIR="${1:-slack-approval-gate-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — the synthetic report step will fail."
fi
if [ -z "$SLACK_BOT_TOKEN" ]; then
  echo "! SLACK_BOT_TOKEN not set — SlackApprovalGateComponent will fail at post-time."
  echo "  Set it: export SLACK_BOT_TOKEN=xoxb-..."
fi
if [ -z "$SLACK_APPROVER_USER_IDS" ]; then
  echo "! SLACK_APPROVER_USER_IDS not set (comma-separated Slack user IDs like 'U1234ALICE,U5678BOB')."
  echo "  The scaffold will use PLACEHOLDER IDs — replace them before materializing."
  SLACK_APPROVER_USER_IDS="UPLACEHOLDER1,UPLACEHOLDER2"
fi
SLACK_CHANNEL="${SLACK_CHANNEL:-#dagster-approvals}"
REQUIRED_APPROVERS="${REQUIRED_APPROVERS:-1}"

info()  { echo "→ $*"; }
ok()    { echo "✓ $*"; }
fail()  { echo "✗ $*"; exit 1; }

# --- 1. fresh project ------------------------------------------------------
rm -rf "$PROJECT_DIR"
info "scaffolding Dagster project…"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -2
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(basename "$PROJECT_ABS" | tr '-' '_')"

# --- 2. deps + env ---------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  info "using local DCC checkout: $DCC_LOCAL_PATH"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

info "installing deps…"
uv add -q "$DCC_SRC" 'litellm>=1.30.0' 'slack_sdk>=3.0' 2>&1 | tail -1

# --- 3. approval dir + report dir -----------------------------------------
mkdir -p approvals reports
APPROVAL_DIR="$PROJECT_ABS/approvals"
REPORT_DIR="$PROJECT_ABS/reports"

# --- 4. defs.yaml files ---------------------------------------------------
# Convert comma-separated user IDs into a YAML list
IFS=',' read -ra IDS <<< "$SLACK_APPROVER_USER_IDS"
APPROVER_YAML=""
for id in "${IDS[@]}"; do
  APPROVER_YAML+="
    - $(echo "$id" | xargs)"
done

# 4a) Synthetic report — AgenticPipelineComponent with one llm_call
mkdir -p "src/$PKG/defs/report_source"
cat > "src/$PKG/defs/report_source/defs.yaml" <<EOF
type: dagster_community_components.AgenticPipelineComponent
attributes:
  asset_name_prefix: report
  group_name: hitl_demo

  source:
    kind: literal
    text: |
      Draft a 5-line quarterly product-launch decision report:
      "Should we ship v2 with the redesigned dashboard, or hold until
      the mobile UX tests are complete?"

  steps:
    - id: source
      op: llm_call
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are a senior PM writing a terse decision report. Cover:
        1) Recommendation (ship/hold), 2) Confidence, 3) Rationale
        (one line), 4) Risks (one line), 5) Next step (one line).
      max_tokens: 300

  outputs:
    assets: [source]
EOF
ok "wrote report_source/defs.yaml (AgenticPipelineComponent — synthetic report)"

# 4b) SlackApprovalGateComponent — posts + polls + writes token
mkdir -p "src/$PKG/defs/report_approval_slack"
cat > "src/$PKG/defs/report_approval_slack/defs.yaml" <<EOF
type: dagster_community_components.SlackApprovalGateComponent
attributes:
  asset_name: report_approval_slack
  upstream_asset_key: report_source

  # Shared with HumanApprovalGateComponent — Slack side writes the token
  # here on quorum; the existing gate reads it downstream.
  approval_dir: "$APPROVAL_DIR"

  slack_channel: "$SLACK_CHANNEL"
  slack_bot_token_env_var: SLACK_BOT_TOKEN

  # ✅ / ❌
  approve_emoji: white_check_mark
  reject_emoji: x

  ping_users_on_post:$APPROVER_YAML

  required_approvers: $REQUIRED_APPROVERS
  approver_allowlist:$APPROVER_YAML

  # 30s polling is fine for a demo; bump to 60-120s at scale to stay
  # under Slack rate limits.
  poll_interval_seconds: 30

  # No timeout for the demo — approver reacts, then quorum, then token.
EOF
ok "wrote report_approval_slack/defs.yaml (SlackApprovalGateComponent)"

# 4c) HumanApprovalGateComponent — asset-check-driven gate on the report
mkdir -p "src/$PKG/defs/report_approval"
cat > "src/$PKG/defs/report_approval/defs.yaml" <<EOF
type: dagster_community_components.HumanApprovalGateComponent
attributes:
  asset_name: report_approval
  upstream_asset_key: report_source
  approval_dir: "$APPROVAL_DIR"
  default_approval_key: default
  group_name: hitl_demo
  kinds: [human, approval]
  description: |
    Reads the JSON token file the Slack watcher writes when quorum is
    reached. Asset check 'approved' fails ERROR when the token is
    rejected or missing; passes when Slack quorum said approved.
EOF
ok "wrote report_approval/defs.yaml (HumanApprovalGateComponent)"

# 4d) FilesystemMonitorSensorComponent — auto-progresses on token drop
mkdir -p "src/$PKG/defs/approval_watcher"
cat > "src/$PKG/defs/approval_watcher/defs.yaml" <<EOF
type: dagster_community_components.FilesystemMonitorSensorComponent
attributes:
  sensor_name: approval_watcher
  directory_path: "$APPROVAL_DIR"
  file_pattern: '.*\.json\$'
  job_name: ship_report_job
  minimum_interval_seconds: 5
  default_status: running
EOF
ok "wrote approval_watcher/defs.yaml (FilesystemMonitorSensorComponent — file-drop reactor)"

# 4e) AssetJobComponent — the target the watcher sensor launches
mkdir -p "src/$PKG/defs/ship_report_job"
cat > "src/$PKG/defs/ship_report_job/defs.yaml" <<EOF
type: dagster_community_components.AssetJobComponent
attributes:
  job_name: ship_report_job
  asset_keys: [report_approval]
  description: |
    Sensor-launched job. Fires whenever the approval directory sees a
    new JSON token. Materializing report_approval passes the upstream
    report through the gate.
EOF
ok "wrote ship_report_job/defs.yaml (AssetJobComponent — sensor target)"

# --- 5. validate ----------------------------------------------------------
info "dg check defs…"
uv run dagster definitions validate 2>&1 | tail -5 || fail "definitions failed to load"

# --- 6. next-steps summary ------------------------------------------------
DM="${PKG}.definitions"

echo
ok "Scaffold complete."
echo
cat <<EOF
Flow to run end-to-end (interactive — needs real Slack approvers):

  # 1. Materialize the synthetic report
  cd $PROJECT_ABS
  DAGSTER_HOME=$DAGSTER_HOME uv run dagster asset materialize \\
    --select report_source -m $DM

  # 2. Materialize the Slack post — this fires the message to $SLACK_CHANNEL
  DAGSTER_HOME=$DAGSTER_HOME uv run dagster asset materialize \\
    --select report_approval_slack_posted -m $DM

  # 3. Start dg dev so the watcher sensor runs
  DAGSTER_HOME=$DAGSTER_HOME uv run dg dev

  # 4. In Slack ($SLACK_CHANNEL): react :white_check_mark: on the post.
  #    Once $REQUIRED_APPROVERS allowlisted approver(s) have reacted, the
  #    watcher sensor writes $APPROVAL_DIR/default.json with approved=true.
  #    Latency ~30s (poll interval).

  # 5. The FilesystemMonitor sensor (approval_watcher) then fires
  #    ship_report_job automatically → report_approval materializes → the
  #    'approved' check passes → downstream automation unblocks.

Watch it end-to-end:
  - Slack thread ($SLACK_CHANNEL): posts + approver reactions live here
  - dg dev UI: report_approval_slack_watcher sensor tab shows tick history
  - dg dev UI: report_approval asset → Materializations → 'approved' check

## Troubleshooting

- "Bot not in channel" error → run \`/invite @your-bot\` in $SLACK_CHANNEL
- No reactions detected → verify approver IDs match your workspace's
  user IDs (Copy member ID via Slack profile). Reactions from anyone
  NOT in the allowlist are ignored.
- Sidecar file exists but no token being written → check the sensor's
  tick log in dg dev — Slack API errors show there.
EOF
