#!/usr/bin/env bash
# maintainer_investigation_room — the "AI maintainer investigation room" pattern.
#
# A prospect asks: "an agent triages a GitHub issue on a public repo. Multiple
# specialist agents fan out on repo shape, docs, reproduction, and prior
# history. A triage node joins by named input. A skeptic critiques. A routing
# decision node joins again. A report writer emits the maintainer-facing
# markdown. A human signs off. Then the report ships."
#
# Prefect's Aug-13 pre-MVP Loom showed this pattern as a bespoke "execution
# plan" — nodes with typed named I/O ports, edges wired by port name. Same
# shape here, 100% components, ONE AgenticPipelineComponent YAML using the
# `mcp_call` op + typed `inputs:` field (v2 wiring):
#
#   maintainer_investigation (AgenticPipelineComponent — Prefect-shape plan)
#     intake       (mcp_call — GitHub MCP get_issue)
#      ├── repo_evidence     (llm_call, inputs: {issue_facts: from intake})
#      ├── docs_evidence     (llm_call, inputs: {issue_facts: from intake})
#      ├── reproduction      (llm_call, inputs: {issue_facts: from intake})
#      └── history_evidence  (llm_call, inputs: {issue_facts: from intake})
#     preliminary  (synthesize, inputs: {issue_facts, reproduction, docs_evidence,
#                                        repo_evidence, history_evidence, triage_policy})
#     skeptic      (llm_call, inputs: {issue_facts, preliminary})
#     decision     (synthesize, inputs: {skeptic, issue_facts, preliminary, triage_policy})
#     report       (llm_call, inputs: {decision})
#         │
#         ▼
#   report_approval (HumanApprovalGateComponent) ← waits for {approved:true}
#         │
#         ▼ (ship_report_job triggered by filesystem sensor)
#
# Plus: approval_watcher (FilesystemMonitorSensorComponent) auto-progresses
# report_approval the moment an approval token appears.
#
# The demo walks two scenarios:
#   scenario_a → gets approved → report ships to reports/investigation_<key>.json
#   scenario_b → left pending  → shows the wait state (drop token via CLI to
#                                see the sensor auto-progress it)
#
# NEED:
#   - OPENAI_API_KEY for LLM calls (~15 calls per issue at gpt-4o-mini)
#   - GITHUB_PERSONAL_ACCESS_TOKEN for the GitHub MCP (any classic PAT with
#     public_repo scope; free to create at github.com/settings/tokens)
#   - npx (bundled with Node) for the stdio MCP server
#   - uv

set -eo pipefail

PROJECT_DIR="${1:-maintainer-investigation-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"
DAGSTER_ISSUE_NUM="${DAGSTER_ISSUE_NUM:-30000}"   # any real dagster-io/dagster issue

# --- 0. preflight ---------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1
fi
if ! command -v npx >/dev/null 2>&1; then
  echo "✗ npx required (Node.js — the GitHub MCP server runs via 'npx -y @modelcontextprotocol/server-github')"
  exit 1
fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — LLM steps will fail at materialize time."
  echo "  Set it: export OPENAI_API_KEY=sk-..."
fi
if [ -z "$GITHUB_PERSONAL_ACCESS_TOKEN" ]; then
  echo "! GITHUB_PERSONAL_ACCESS_TOKEN not set — GitHub MCP will fail."
  echo "  Create a classic PAT (public_repo scope only): https://github.com/settings/tokens/new"
  echo "  Then: export GITHUB_PERSONAL_ACCESS_TOKEN=ghp_..."
fi

info()  { echo "→ $*"; }
ok()    { echo "✓ $*"; }
fail()  { echo "✗ $*"; exit 1; }

# --- 1. fresh project scaffold --------------------------------------------
rm -rf "$PROJECT_DIR"
info "scaffolding Dagster project…"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(basename "$PROJECT_ABS" | tr '-' '_')"

# --- 2. env + deps --------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  info "using local DCC checkout: $DCC_LOCAL_PATH"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

info "installing deps…"
uv add -q "$DCC_SRC" pandas openai 'litellm>=1.30.0' 'mcp>=1.0.0' 2>&1 | tail -1

# --- 3. seed dirs (all inside project — Windows-portable) -----------------
mkdir -p approvals reports mcp_scratch
APPROVAL_DIR="$PROJECT_ABS/approvals"
REPORT_DIR="$PROJECT_ABS/reports"

# --- 4. defs.yaml files ---------------------------------------------------
# 4a) AgenticPipelineComponent — the ENTIRE 8-step Prefect-shape execution
# plan in one YAML: MCP fetch → 4 specialists → typed-input triage join →
# skeptic critique → maintainer-facing report. Every step is a first-class
# Dagster asset. Every join uses typed named `inputs:` — the direct 1:1
# match to Prefect's execution-plan graph shape.
mkdir -p "src/$PKG/defs/maintainer_investigation"
cat > "src/$PKG/defs/maintainer_investigation/defs.yaml" <<EOF
type: dagster_community_components.AgenticPipelineComponent
attributes:
  asset_name_prefix: mir
  group_name: investigation
  kinds: [llm, agent, pipeline]

  # Dynamic-partitioned on a composite key like 'dagster-io/dagster#30000'.
  # A companion PartitionedAssetLauncherJobComponent (see launcher/defs.yaml)
  # takes owner/repo/issue_number via run config, formats this partition
  # key, registers it, and kicks off the pipeline. Every triage becomes
  # one persistent partition — browsable / re-runnable in the UI.
  partition_type: dynamic
  dynamic_partition_name: mir_investigations
  # ':int' on issue_number makes the parsed value an int — so
  # `tool_args: {issue_number: "{partition.issue_number}"}` lands as int
  # in the MCP call (GitHub's get_issue rejects string issue_number).
  partition_key_parser: "{owner}/{repo}#{issue_number:int}"

  # PLAN INPUTS — the analog to Prefect's "PLAN INPUTS" node. Every value
  # is derived from the parsed partition_key so one YAML handles any
  # {owner, repo, issue_number} triple.
  source:
    kind: literal
    text: |
      owner={partition.owner}
      repo={partition.repo}
      issue_number={partition.issue_number}
      triage_policy=defect | expected-behavior | needs-more-info | docs-gap | feature-request

  steps:
    # ── Issue intake via the GitHub MCP — no LLM. Deterministic fetch. ──
    - id: intake
      op: mcp_call
      server:
        name: github
        type: stdio
        command: [npx, -y, "@modelcontextprotocol/server-github"]
      mcp_tool_name: get_issue
      tool_args:
        owner: "{partition.owner}"
        repo: "{partition.repo}"
        issue_number: "{partition.issue_number}"
      parse_as: auto

    # ── 4 specialists — each takes the intake facts by NAMED input. ──
    #    (Matches Prefect's graph: every specialist reads issue_facts.)

    - id: repo_evidence
      op: llm_call
      inputs:
        issue_facts: {from: intake}
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are the repo-cartography specialist. Given the ISSUE_FACTS
        below, identify which dagster-io/dagster modules/paths are most
        likely relevant. Cite concrete module paths where you can infer
        them from the text. Do not invent paths.
      prompt_template: |
        ISSUE_FACTS
        ==========
        {issue_facts}
      max_tokens: 400

    - id: docs_evidence
      op: llm_call
      inputs:
        issue_facts: {from: intake}
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are the documentation specialist. Given the ISSUE_FACTS
        below, describe what docs.dagster.io *says should happen* in the
        reporter's scenario and where documentation may be missing or
        contradicts observed behavior.
      prompt_template: |
        ISSUE_FACTS
        ==========
        {issue_facts}
      max_tokens: 400

    - id: reproduction
      op: llm_call
      inputs:
        issue_facts: {from: intake}
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are the reproduction specialist. Given the ISSUE_FACTS
        below, propose the smallest decisive local reproducer. Note
        what's missing from the report (versions, env, code) — what
        would you ask the reporter? DO NOT claim you ran the repro.
      prompt_template: |
        ISSUE_FACTS
        ==========
        {issue_facts}
      max_tokens: 400

    - id: history_evidence
      op: llm_call
      inputs:
        issue_facts: {from: intake}
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are the support historian. Given the ISSUE_FACTS below,
        assess whether the symptoms match any well-known gotcha or
        pattern (env config, IO manager mismatch, version drift,
        docker-vs-local runtime, etc.). Cite patterns by name.
      prompt_template: |
        ISSUE_FACTS
        ==========
        {issue_facts}
      max_tokens: 400

    # ── Preliminary triage — typed multi-input join. Six named inputs, ──
    #    same shape as Prefect's "Preliminary triage (join)" node.
    - id: preliminary
      op: synthesize
      inputs:
        issue_facts:      {from: intake}
        reproduction:     {from: reproduction}
        docs_evidence:    {from: docs_evidence}
        repo_evidence:    {from: repo_evidence}
        history_evidence: {from: history_evidence}
        triage_policy:    {literal: "defect | expected-behavior | needs-more-info | docs-gap | feature-request"}
      model: gpt-4o
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are the triage lead. Given intake facts + 4 evidence reports
        + the triage policy, return a preliminary triage decision:
          - **classification:** one of the TRIAGE_POLICY values
          - **confidence:** low | medium | high
          - **rationale:** 2-3 sentences citing which evidence drove the call
          - **owner:** core | cloud | integrations | docs
          - **next_action:** one concrete step
      prompt_template: |
        ISSUE_FACTS
        ==========
        {issue_facts}

        REPRODUCTION
        ============
        {reproduction}

        DOCS_EVIDENCE
        =============
        {docs_evidence}

        REPO_EVIDENCE
        =============
        {repo_evidence}

        HISTORY_EVIDENCE
        ================
        {history_evidence}

        TRIAGE_POLICY
        =============
        {triage_policy}
      max_tokens: 500

    # ── Independent skeptic — reads issue_facts + preliminary by name. ──
    - id: skeptic
      op: llm_call
      inputs:
        issue_facts: {from: intake}
        preliminary: {from: preliminary}
      model: gpt-4o
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are an independent skeptical reviewer. Critique the preliminary
        triage against the intake facts. Where does the confidence feel
        unearned? Is the classification the tightest fit? Is next_action
        concrete? DO NOT rewrite it — critique only.
      prompt_template: |
        ISSUE_FACTS
        ==========
        {issue_facts}

        PRELIMINARY
        ===========
        {preliminary}
      max_tokens: 400

    # ── Final routing decision — 4-input join, same shape as Prefect's ──
    #    "Final routing decision (join)" node.
    - id: decision
      op: synthesize
      inputs:
        skeptic:       {from: skeptic}
        issue_facts:   {from: intake}
        preliminary:   {from: preliminary}
        triage_policy: {literal: "defect | expected-behavior | needs-more-info | docs-gap | feature-request"}
      model: gpt-4o
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are the routing decision-maker. Join the preliminary triage
        with the skeptic's critique, cross-referenced against issue facts
        and the triage policy. Emit the final classification, confidence,
        owner, and next_action. If the skeptic surfaced a legit gap,
        downgrade confidence or reclassify.
      prompt_template: |
        SKEPTIC
        =======
        {skeptic}

        ISSUE_FACTS
        ===========
        {issue_facts}

        PRELIMINARY
        ===========
        {preliminary}

        TRIAGE_POLICY
        =============
        {triage_policy}
      max_tokens: 500

    # ── Report writer — polishes the decision into maintainer-facing markdown. ──
    - id: report
      op: llm_call
      inputs:
        decision: {from: decision}
      model: gpt-4o
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        Render the final maintainer-facing report as clean markdown.
        Structure:

          # Issue triage — {partition.owner}/{partition.repo}#{partition.issue_number}
          **Classification:** ...  **Confidence:** ...  **Owner:** ...

          ## Rationale
          ...

          ## Next action
          ...

          ## Uncertainty
          What we could not confirm without more info from the reporter.

        Keep it under 400 words. Human-readable at a glance.
      prompt_template: |
        DECISION
        ========
        {decision}
      max_tokens: 700

  outputs:
    # Every node is a first-class asset in the Dagster graph — downstream
    # things (dbt models, warehouse tables, notifications) can depend on
    # ANY of them, not just the report.
    assets: [intake, repo_evidence, docs_evidence, reproduction, history_evidence, preliminary, skeptic, decision, report]

    # Also write the report to a side file so downstream sensors +
    # webhooks can consume without loading pickled assets.
    # {partition_key}-templated so every triage lands in its own file.
    text_sinks:
      - from: report
        path: "$REPORT_DIR/investigation_{partition.owner}_{partition.repo}_{partition.issue_number}.md"
EOF
ok "wrote maintainer_investigation/defs.yaml (AgenticPipelineComponent — dynamic-partitioned, 9-step Prefect-shape execution plan, typed named inputs, config-driven via launcher)"

# 4b) PartitionedAssetLauncherJobComponent — config-driven entry point.
# User (or an external system) POSTs {owner, repo, issue_number}; the
# launcher formats a composite partition key, registers it on the same
# DynamicPartitionsDefinition the pipeline uses, then materializes the
# pipeline with that partition_key. One entry point, N runs, N
# persistent partitions in the asset graph.
mkdir -p "src/$PKG/defs/launcher"
cat > "src/$PKG/defs/launcher/defs.yaml" <<EOF
type: dagster_community_components.PartitionedAssetLauncherJobComponent
attributes:
  job_name: launch_mir_triage
  target_asset_keys:
    - mir_intake
    - mir_repo_evidence
    - mir_docs_evidence
    - mir_reproduction
    - mir_history_evidence
    - mir_preliminary
    - mir_skeptic
    - mir_decision
    - mir_report
  dynamic_partitions_name: mir_investigations
  partition_key_template: "{owner}/{repo}#{issue_number}"
  config_schema:
    owner:        {type: str, default: dagster-io}
    repo:         {type: str, default: dagster}
    issue_number: {type: int}
  tags:
    purpose: ai-triage-launcher
EOF
ok "wrote launcher/defs.yaml (PartitionedAssetLauncherJobComponent — config-driven entry point)"

# 4c) HumanApprovalGateComponent — sign-off before the report ships
mkdir -p "src/$PKG/defs/report_approval"
cat > "src/$PKG/defs/report_approval/defs.yaml" <<EOF
type: dagster_community_components.HumanApprovalGateComponent
attributes:
  asset_name: report_approval
  upstream_asset_key: mir_report
  approval_dir: "$APPROVAL_DIR"
  default_approval_key: default
  group_name: investigation
  kinds: [human, approval]
  description: |
    Human sign-off before the triage report ships. Always materializes;
    the asset check 'approved' carries the state. Reads
    approvals/default.json:
      - missing              → check passed=False (WARN), status=approval_pending
      - {"approved": false}  → check passed=False (ERROR), rejection reason
      - {"approved": true}   → check passed=True, report passes through
                                with approver + reason in metadata
EOF
ok "wrote report_approval/defs.yaml (HumanApprovalGateComponent)"

# 4d) FilesystemMonitorSensorComponent — auto-progress on token drop
mkdir -p "src/$PKG/defs/approval_watcher"
cat > "src/$PKG/defs/approval_watcher/defs.yaml" <<EOF
type: dagster_community_components.FilesystemMonitorSensorComponent
attributes:
  sensor_name: approval_watcher
  directory_path: "$APPROVAL_DIR"
  file_pattern: '.*\.json$'
  job_name: ship_report_job
  minimum_interval_seconds: 5
  default_status: running
EOF
ok "wrote approval_watcher/defs.yaml (FilesystemMonitorSensorComponent)"

# 4e) AssetJobComponent — the target the sensor launches
mkdir -p "src/$PKG/defs/ship_report_job"
cat > "src/$PKG/defs/ship_report_job/defs.yaml" <<EOF
type: dagster_community_components.AssetJobComponent
attributes:
  job_name: ship_report_job
  asset_keys: [report_approval]
  description: |
    Sensor-launched job. approval_watcher fires this the moment a new
    approvals/*.json drops. Materializing report_approval passes the
    upstream report through the gate.
  tags:
    purpose: ai-triage-ship
EOF
ok "wrote ship_report_job/defs.yaml (AssetJobComponent — sensor target)"

# --- 5. validate ----------------------------------------------------------
info "dg check defs…"
uv run dagster definitions validate 2>&1 | tail -5 || fail "definitions failed to load"

# --- 6. materialize scenario A (fetch + investigate + wait for approval) --
DM="${PKG}.definitions"

info "launching the triage via the config-driven launcher job (owner/repo/issue_number → derived partition → pipeline)…"
# The launcher takes owner + repo + issue_number as run config, formats
# the partition key `{owner}/{repo}#{issue_number}`, registers it on the
# `mir_investigations` DynamicPartitionsDefinition, then materializes
# all 9 pipeline assets with that partition_key. One config-shaped form
# in the UI, or one --config YAML from the CLI, or one POST via GraphQL —
# same entry point.
cat > /tmp/mir_launch_${DAGSTER_ISSUE_NUM}.yaml <<CFG
ops:
  launch_mir_triage_op:
    config:
      owner: dagster-io
      repo: dagster
      issue_number: ${DAGSTER_ISSUE_NUM}
CFG
uv run dagster job execute -m "$DM" -j launch_mir_triage --config /tmp/mir_launch_${DAGSTER_ISSUE_NUM}.yaml 2>&1 | tail -3 || fail "launcher run failed"
REPORT_FILE="$REPORT_DIR/investigation_dagster-io_dagster_${DAGSTER_ISSUE_NUM}.md"
ok "report drafted — see $REPORT_FILE"

echo
info "─── report preview (first 40 lines) ───"
head -40 "$REPORT_FILE" 2>/dev/null || echo "(no draft file yet — the AgenticPipeline's text_sinks writes on report step completion)"
echo "───────────────────────────────────────"
echo

# --- 7. approval flow -----------------------------------------------------
info "materializing report_approval WITHOUT a token — asset materializes but check 'approved' fails (approval_pending)…"
uv run dagster asset materialize --select report_approval -m "$DM" 2>&1 \
  | grep -E "STEP_SUCCESS|Asset check|approval_" | tail -3 || true
ok "gate ran; asset check flagged approval_pending"

info "dropping approval token…"
cat > "$APPROVAL_DIR/default.json" <<'JSON'
{"approved": true, "approver": "demo-runner", "reason": "Looks accurate; ship it."}
JSON

info "re-materializing report_approval — check 'approved' should now pass…"
uv run dagster asset materialize --select report_approval -m "$DM" 2>&1 \
  | grep -E "STEP_SUCCESS|Asset check.*passed|Approved by" | tail -3 \
  || fail "gate did not pass with valid token"
ok "gate passed; report ready to ship"

# --- 8. summary -----------------------------------------------------------
echo
ok "Demo complete."
echo
cat <<EOF
The "maintainer investigation room" pattern ran end-to-end as a
9-node Prefect-shape execution plan, ONE AgenticPipelineComponent YAML:

  1. intake (mcp_call — GitHub MCP get_issue)
  2. repo_evidence, docs_evidence, reproduction, history_evidence
     (4x llm_call fan-out — each with typed inputs from intake)
  3. preliminary (synthesize, 6 typed named inputs joined)
  4. skeptic (llm_call, 2 typed named inputs)
  5. decision (synthesize, 4 typed named inputs)
  6. report (llm_call, 1 typed named input from decision)
  7. HumanApprovalGateComponent → blocked, then passed on token drop

Config-driven entry point (the launcher job): user posts
{owner, repo, issue_number} as run config → launcher formats a composite
partition key → registers the dynamic partition → materializes the whole
pipeline for that partition. Every triage becomes one persistent
partition in the mir_investigations DynamicPartitionsDefinition —
browsable + re-runnable in the UI.

Report:
  $REPORT_FILE

Next: open the UI and see the graph:
  cd $PROJECT_ABS
  DAGSTER_HOME=$DAGSTER_HOME uv run dg dev

Launch another triage from the UI: pick the launch_mir_triage job in the
Jobs tab → click Materialize → fill in owner/repo/issue_number in the
run-config form. The launcher registers a new partition and materializes
the pipeline for it. No YAML edit needed per invocation.

Or from the CLI:
  cat > /tmp/mir_launch.yaml <<CFG
  ops:
    launch_mir_triage_op:
      config:
        owner: prefecthq
        repo: prefect
        issue_number: 12345
  CFG
  uv run dagster job execute -m $DM -j launch_mir_triage --config /tmp/mir_launch.yaml

The approval_watcher sensor auto-progresses the gate the moment a new
JSON drops into $APPROVAL_DIR. Drop a second token to see that in action:
  echo '{"approved":true,"approver":"you"}' > $APPROVAL_DIR/second.json
EOF
