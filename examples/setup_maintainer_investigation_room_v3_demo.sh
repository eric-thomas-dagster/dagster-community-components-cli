#!/usr/bin/env bash
# maintainer_investigation_room_v3 — the comprehensive AI-agentic
# orchestration demo. Uses EVERY new primitive shipped in v0.10.69–v0.10.73:
#
#   - PartitionedAssetLauncherJobComponent  — config-driven entry point
#   - AgenticPipelineComponent
#     - per_step_ops: true                  — one op per step, visible in Runs page
#     - fastmcp transport                   — GitHub MCP via FastMCP v2 client
#     - tool_use_loop op                    — open-ended agent tool-use (LangGraph-shape)
#     - handoff op → LangGraph              — bring your existing framework code
#     - debate op                           — 3 skeptics + arbitrator
#     - critique_loop op                    — drafter + critic iterations
#   - SlackApprovalGateComponent            — Slack quorum HITL
#   - HumanApprovalGateComponent            — asset-check-driven gate
#   - FilesystemMonitorSensorComponent      — auto-progresses ship job
#   - AssetJobComponent                     — sensor target
#
# ## Architecture (11 first-class Dagster assets, 1 sensor, 2 jobs)
#
#   launch_mir_triage_v3 (job — config-driven entry)
#         │
#         ▼ (partition key = {owner}/{repo}#{issue_number:int})
#   mir_intake (mcp_call → GitHub MCP via fastmcp)
#         │
#         ├── mir_repo_evidence (tool_use_loop — LLM picks GitHub MCP tools iteratively)
#         ├── mir_reproduction (handoff → LangGraph state-machine)
#         ├── mir_docs_evidence (llm_call)
#         └── mir_history_evidence (llm_call)
#         │
#         ▼
#   mir_preliminary (synthesize — 5 typed named inputs)
#         │
#         ▼
#   mir_skeptic_debate (debate — 3 skeptic proposers + arbitrator)
#         │
#         ▼
#   mir_decision (synthesize — 4 typed named inputs)
#         │
#         ▼
#   mir_report (critique_loop — drafter + critic, ≤ 2 iterations,
#                stops early when critic scores draft ≥ 85/100)
#         │
#         ▼
#   mir_report_approval_slack (SlackApprovalGateComponent posts + polls)
#         │
#         ▼ (Slack quorum → JSON token drops)
#   mir_report_approval (HumanApprovalGateComponent — asset check gates ship)
#         │
#         ▼ (FilesystemMonitorSensorComponent auto-fires)
#   ship_mir_report_v3_job (AssetJobComponent)
#
# ## What per_step_ops gives you in the Runs page
#
#   Every step above shows as ITS OWN op with an independent step_key:
#     mir_pipeline.mir_ingest
#     mir_pipeline.mir_intake
#     mir_pipeline.mir_repo_evidence
#     mir_pipeline.mir_reproduction    ← LangGraph invoked here
#     mir_pipeline.mir_docs_evidence
#     mir_pipeline.mir_history_evidence
#     mir_pipeline.mir_preliminary
#     mir_pipeline.mir_skeptic_debate
#     mir_pipeline.mir_decision
#     mir_pipeline.mir_report
#     mir_pipeline.mir_extract
#
#   Dagster's native per-op retry works — if mir_report fails, retry restarts
#   from that op only. Full dbt-style resume semantics without splitting into
#   N separate @asset decorators.
#
# ## Needs
#   - OPENAI_API_KEY                  — all LLM steps (~$0.05 per triage)
#   - GITHUB_PERSONAL_ACCESS_TOKEN    — GitHub MCP (any classic PAT, public_repo scope)
#   - npx                             — for the GitHub MCP stdio server
#   - uv                              — package manager
#   - langgraph>=0.2                  — installed by this script for the handoff op
#
# ## Optional (skipped gracefully if unset)
#   - SLACK_BOT_TOKEN                 — enables real Slack HITL
#   - SLACK_APPROVER_USER_IDS         — comma-separated user IDs
#   - SLACK_CHANNEL                   — default #dagster-triage
#
# ## Cost
#   ~$0.05 per full triage (5 llm_call/synthesize/etc + tool_use_loop with
#   several MCP calls + LangGraph 5-node handoff + critique_loop 2 iterations)

set -eo pipefail

PROJECT_DIR="${1:-maintainer-triage-comprehensive}"
COMMIT_SHA="${COMMIT_SHA:-main}"
DAGSTER_ISSUE_NUM="${DAGSTER_ISSUE_NUM:-30000}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if ! command -v npx >/dev/null 2>&1; then echo "✗ npx required (Node.js) — the GitHub MCP server runs via npx"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "! OPENAI_API_KEY not set — LLM steps will fail. Set it: export OPENAI_API_KEY=sk-..."
fi
if [ -z "$GITHUB_PERSONAL_ACCESS_TOKEN" ]; then
  echo "! GITHUB_PERSONAL_ACCESS_TOKEN not set — the GitHub MCP step will fail."
  echo "  Create a PAT at https://github.com/settings/tokens/new (public_repo scope)."
fi
SLACK_CHANNEL="${SLACK_CHANNEL:-#dagster-triage}"
REQUIRED_APPROVERS="${REQUIRED_APPROVERS:-1}"

info()  { echo "→ $*"; }
ok()    { echo "✓ $*"; }
fail()  { echo "✗ $*"; exit 1; }

# --- 1. fresh project scaffold --------------------------------------------
rm -rf "$PROJECT_DIR"
info "scaffolding Dagster project…"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -2
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(basename "$PROJECT_ABS" | tr '-' '_')"

# --- 2. deps + env --------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  info "using local DCC checkout: $DCC_LOCAL_PATH"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"

info "installing deps (including langgraph for the handoff op)…"
uv add -q "$DCC_SRC" 'litellm>=1.30.0' 'mcp>=1.0.0' 'fastmcp>=2.0' 'slack_sdk>=3.0' 'langgraph>=0.2' 2>&1 | tail -1

# --- 3. seed dirs ---------------------------------------------------------
mkdir -p approvals reports "src/$PKG/framework_targets"
APPROVAL_DIR="$PROJECT_ABS/approvals"
REPORT_DIR="$PROJECT_ABS/reports"

# --- 4. LangGraph target for the handoff op -------------------------------
# Write the reference implementation directly into the customer's project.
# In a real deployment this file would be YOUR framework code — the
# `handoff` op imports it and calls run_reproduction_analysis(...).
touch "src/$PKG/framework_targets/__init__.py"
cat > "src/$PKG/framework_targets/reproducer.py" <<'PYEOF'
"""LangGraph target for the MIR-v3 handoff op. plan → hypothesize → verify → refine → finalize."""
import os
import time
from typing import Any, Dict, List


def _llm(messages, model="gpt-4o-mini", max_tokens=400):
    import litellm
    litellm.drop_params = True
    kwargs = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
    if os.environ.get("OPENAI_API_KEY"):
        kwargs["api_key"] = os.environ["OPENAI_API_KEY"]
    t0 = time.time()
    resp = litellm.completion(**kwargs)
    latency = int((time.time() - t0) * 1000)
    try:
        cost = float(litellm.completion_cost(completion_response=resp))
    except Exception:
        cost = 0.0
    return {"content": resp.choices[0].message.content or "", "cost_usd": cost, "latency_ms": latency}


def _plan_node(state):
    r = _llm([
        {"role": "system", "content": "You are a bug reproduction planner. Given the issue facts, outline a 2-sentence PLAN to reproduce the bug."},
        {"role": "user", "content": state["issue_facts"]},
    ])
    return {**state, "hypothesis": r["content"],
            "trajectory": list(state.get("trajectory") or []) + [{"node": "plan", "text": r["content"], "cost_usd": r["cost_usd"], "latency_ms": r["latency_ms"]}],
            "total_cost_usd": (state.get("total_cost_usd") or 0.0) + r["cost_usd"]}


def _hypothesize_node(state):
    r = _llm([
        {"role": "system", "content": "You are a bug hypothesis generator. Given the plan, propose ONE testable hypothesis about the root cause."},
        {"role": "user", "content": f"PLAN:\n{state['hypothesis']}\n\nISSUE:\n{state['issue_facts']}"},
    ])
    return {**state, "hypothesis": r["content"],
            "trajectory": state["trajectory"] + [{"node": "hypothesize", "text": r["content"], "cost_usd": r["cost_usd"], "latency_ms": r["latency_ms"]}],
            "total_cost_usd": state["total_cost_usd"] + r["cost_usd"]}


def _verify_node(state):
    r = _llm([
        {"role": "system", "content": "You are a devil's advocate reviewer. Given the hypothesis, list ONE reason it might be WRONG."},
        {"role": "user", "content": f"HYPOTHESIS:\n{state['hypothesis']}\n\nISSUE:\n{state['issue_facts']}"},
    ])
    return {**state, "verification": r["content"],
            "trajectory": state["trajectory"] + [{"node": "verify", "text": r["content"], "cost_usd": r["cost_usd"], "latency_ms": r["latency_ms"]}],
            "total_cost_usd": state["total_cost_usd"] + r["cost_usd"]}


def _refine_node(state):
    r = _llm([
        {"role": "system", "content": "Rewrite the hypothesis addressing the verifier's concern. Add a suggested minimal repro (2-3 lines)."},
        {"role": "user", "content": f"HYPOTHESIS:\n{state['hypothesis']}\n\nVERIFIER:\n{state['verification']}\n\nISSUE:\n{state['issue_facts']}"},
    ])
    return {**state, "refined": r["content"],
            "trajectory": state["trajectory"] + [{"node": "refine", "text": r["content"], "cost_usd": r["cost_usd"], "latency_ms": r["latency_ms"]}],
            "total_cost_usd": state["total_cost_usd"] + r["cost_usd"]}


def _finalize_node(state):
    final = (
        "## Reproduction analysis (LangGraph handoff)\n\n"
        f"**Hypothesis:** {state['hypothesis']}\n\n"
        f"**Verifier concern:** {state['verification']}\n\n"
        f"**Refined + minimal repro:**\n{state['refined']}\n"
    )
    return {**state, "final_answer": final,
            "trajectory": state["trajectory"] + [{"node": "finalize", "text": final, "cost_usd": 0.0, "latency_ms": 0}],
            "n_nodes": 5}


def _build_graph():
    from langgraph.graph import END, START, StateGraph
    g = StateGraph(dict)
    g.add_node("plan", _plan_node)
    g.add_node("hypothesize", _hypothesize_node)
    g.add_node("verify", _verify_node)
    g.add_node("refine", _refine_node)
    g.add_node("finalize", _finalize_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "hypothesize")
    g.add_edge("hypothesize", "verify")
    g.add_edge("verify", "refine")
    g.add_edge("refine", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


def run_reproduction_analysis(issue_facts: str) -> Dict[str, Any]:
    """Entry point invoked by the AgenticPipelineComponent's `handoff` op."""
    graph = _build_graph()
    result = graph.invoke({"issue_facts": issue_facts, "trajectory": [], "total_cost_usd": 0.0})
    return {
        "final_answer": result["final_answer"],
        "n_nodes_executed": result.get("n_nodes", 0),
        "cost_usd": round(result.get("total_cost_usd") or 0.0, 6),
        "trajectory": result.get("trajectory") or [],
    }
PYEOF
ok "wrote framework_targets/reproducer.py (LangGraph state machine, invoked via handoff op)"

# --- 5. defs.yaml: launcher (config-driven entry) -------------------------
mkdir -p "src/$PKG/defs/launcher"
cat > "src/$PKG/defs/launcher/defs.yaml" <<EOF
type: dagster_community_components.PartitionedAssetLauncherJobComponent
attributes:
  job_name: launch_mir_triage_v3
  target_asset_keys:
    - mir_intake
    - mir_repo_evidence
    - mir_reproduction
    - mir_docs_evidence
    - mir_history_evidence
    - mir_preliminary
    - mir_skeptic_debate
    - mir_decision
    - mir_report
  dynamic_partitions_name: mir_investigations
  partition_key_template: "{owner}/{repo}#{issue_number}"
  config_schema:
    owner:        {type: str, default: dagster-io}
    repo:         {type: str, default: dagster}
    issue_number: {type: int, default: ${DAGSTER_ISSUE_NUM}}
  tags:
    purpose: mir-v3-comprehensive
EOF
ok "wrote launcher/defs.yaml (PartitionedAssetLauncherJobComponent — config entry)"

# --- 6. defs.yaml: the comprehensive AgenticPipeline ----------------------
mkdir -p "src/$PKG/defs/maintainer_investigation"
cat > "src/$PKG/defs/maintainer_investigation/defs.yaml" <<EOF
type: dagster_community_components.AgenticPipelineComponent
attributes:
  asset_name_prefix: mir
  group_name: investigation
  kinds: [llm, agent, pipeline, mir_v3]

  # ⭐ PER-STEP OPS: every step below becomes its own @op — visible in
  # the Runs page as a separate step_key, retryable independently.
  per_step_ops: true

  # Dynamic-partitioned on composite key {owner}/{repo}#{issue_number}.
  # The launcher fires with a run-config, computes the partition_key,
  # registers it, materializes the pipeline for that partition.
  partition_type: dynamic
  dynamic_partition_name: mir_investigations
  partition_key_parser: "{owner}/{repo}#{issue_number:int}"

  source:
    kind: literal
    text: |
      owner={partition.owner}
      repo={partition.repo}
      issue_number={partition.issue_number}
      triage_policy=defect | expected-behavior | needs-more-info | docs-gap | feature-request

  steps:
    # ── 1. Deterministic issue fetch — GitHub MCP via FastMCP ──
    - id: intake
      op: mcp_call
      server:
        # ⭐ FastMCP transport (auto-detection, better auth than raw MCP SDK)
        # falls back to plain stdio here since GitHub MCP is stdio-only,
        # but the fastmcp Client handles it transparently. Swap to
        # `type: http` / `type: fastmcp` with a URL for remote MCP servers.
        name: github
        type: stdio
        command: [npx, -y, "@modelcontextprotocol/server-github"]
      mcp_tool_name: get_issue
      tool_args:
        owner: "{partition.owner}"
        repo: "{partition.repo}"
        issue_number: "{partition.issue_number}"
      parse_as: auto

    # ── 2a. Repo-code exploration — ⭐ tool_use_loop ──
    #   LLM has GitHub MCP tools available; iteratively searches code,
    #   reads files, follows leads until finalize with a written summary.
    #   Bounded by max_iterations. One asset materializes with the full
    #   tool-call trajectory in metadata.
    - id: repo_evidence
      op: tool_use_loop
      inputs:
        issue_facts: {from: intake}
      model: gpt-4o
      api_key_env_var: OPENAI_API_KEY
      max_iterations: 8
      system_prompt: |
        You are the repo-cartography specialist. Use the GitHub MCP tools
        (search_code, list_directory, read_file) to identify concrete
        code paths in dagster-io/dagster that are likely relevant to
        this issue. Do NOT invent paths — cite files you actually read
        or searched. When you have 2-3 concrete cites, call \`finalize\`
        with a 3-line evidence summary.
      prompt_template: |
        ISSUE_FACTS
        ===========
        {issue_facts}
      mcp_servers:
        - name: github
          type: stdio
          command: [npx, -y, "@modelcontextprotocol/server-github"]
      allowed_tools:
        - search_code
        - get_file_contents
        - list_repository_contents
      finalize_tool_name: finalize

    # ── 2b. Reproduction analysis — ⭐ handoff to LangGraph ──
    #   User's existing LangGraph state-machine (plan → hypothesize →
    #   verify → refine → finalize) becomes ONE Dagster step. Framework's
    #   per-node trace lives inside asset metadata; adjacent Dagster
    #   steps (fan-out, MCP, HITL) stay first-class.
    - id: reproduction
      op: handoff
      inputs:
        issue_facts: {from: intake}
      framework: langgraph
      entry_module: ${PKG}.framework_targets.reproducer
      entry_callable: run_reproduction_analysis
      initial_state:
        issue_facts: "{issue_facts}"
      output_text_key: final_answer

    # ── 2c + 2d. Simpler specialists (llm_call) ──
    - id: docs_evidence
      op: llm_call
      inputs:
        issue_facts: {from: intake}
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are the documentation specialist. Given ISSUE_FACTS below,
        describe what docs.dagster.io *says should happen* in the
        reporter's scenario and where docs may be missing/wrong.
      prompt_template: |
        ISSUE_FACTS
        ===========
        {issue_facts}
      max_tokens: 400

    - id: history_evidence
      op: llm_call
      inputs:
        issue_facts: {from: intake}
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are the support historian. Given ISSUE_FACTS below, assess
        whether the symptoms match any well-known gotcha or pattern
        (env config, IO manager mismatch, version drift, etc.).
      prompt_template: |
        ISSUE_FACTS
        ===========
        {issue_facts}
      max_tokens: 400

    # ── 3. Preliminary triage (5-input synthesize) ──
    - id: preliminary
      op: synthesize
      inputs:
        issue_facts:      {from: intake}
        reproduction:     {from: reproduction}
        docs_evidence:    {from: docs_evidence}
        repo_evidence:    {from: repo_evidence}
        history_evidence: {from: history_evidence}
      model: gpt-4o
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are the triage lead. Given intake facts + 4 evidence reports,
        return a preliminary triage decision:
          - classification (one of the triage_policy values)
          - confidence (low | medium | high)
          - rationale (2-3 sentences citing evidence)
          - owner (core | cloud | integrations | docs)
          - next_action (one concrete step)
      prompt_template: |
        ISSUE_FACTS
        ==========
        {issue_facts}

        REPRODUCTION (LangGraph)
        =========================
        {reproduction}

        REPO_EVIDENCE (tool_use_loop)
        =============================
        {repo_evidence}

        DOCS_EVIDENCE
        =============
        {docs_evidence}

        HISTORY_EVIDENCE
        ================
        {history_evidence}
      max_tokens: 500

    # ── 4. Multi-skeptic debate — ⭐ debate op ──
    #   3 skeptic proposers write critiques of the preliminary in
    #   parallel; arbitrator LLM picks the strongest.
    - id: skeptic_debate
      op: debate
      source: preliminary
      proposers:
        - model: gpt-4o
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "You are a Bayesian skeptic. Attack the confidence level in this triage."
          temperature: 0.6
        - model: gpt-4o
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "You are a classification skeptic. Argue the classification is the wrong bucket."
          temperature: 0.6
        - model: gpt-4o
          api_key_env_var: OPENAI_API_KEY
          system_prompt: "You are an ownership skeptic. Argue the wrong team is being routed to."
          temperature: 0.6
      arbitrator:
        model: gpt-4o
        api_key_env_var: OPENAI_API_KEY
        system_prompt: "Pick the skeptic critique that would most improve the triage. Do not rewrite — just pick + say why."

    # ── 5. Final decision (4-input synthesize) ──
    - id: decision
      op: synthesize
      inputs:
        skeptic:      {from: skeptic_debate}
        issue_facts:  {from: intake}
        preliminary:  {from: preliminary}
        reproduction: {from: reproduction}
      model: gpt-4o
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are the routing decision-maker. Cross-reference the
        preliminary triage with the winning skeptic critique + the
        reproduction analysis. Emit the final classification,
        confidence, owner, and next_action. If the skeptic surfaced a
        legit gap, downgrade confidence or reclassify.
      prompt_template: |
        SKEPTIC (winner from 3-way debate)
        ==================================
        {skeptic}

        ISSUE_FACTS
        ===========
        {issue_facts}

        PRELIMINARY
        ===========
        {preliminary}

        REPRODUCTION
        ============
        {reproduction}
      max_tokens: 500

    # ── 6. Maintainer-facing report — ⭐ critique_loop op ──
    #   Drafter writes report; critic reviews; drafter revises. Cap at
    #   2 iterations, BUT stop early when the critic scores the draft
    #   >= 85/100 (skips the revise step). Real-world triage drafts
    #   are often good on first pass → ~40% call-count savings when
    #   the drafter nails it, still capped at 2 for hard cases.
    #   Final polished markdown lands as mir_report.
    - id: report
      op: critique_loop
      source: decision
      iterations: 2
      until_score_gte: 85
      drafter:
        model: gpt-4o
        api_key_env_var: OPENAI_API_KEY
        system_prompt: |
          Render the final maintainer-facing report as clean markdown:
            # Issue triage — {partition.owner}/{partition.repo}#{partition.issue_number}
            **Classification:** ...  **Confidence:** ...  **Owner:** ...
            ## Rationale
            ...
            ## Next action
            ...
            ## Uncertainty
            What we could not confirm without more info from the reporter.
      critic:
        model: gpt-4o
        api_key_env_var: OPENAI_API_KEY
        system_prompt: |
          Critique this maintainer triage report for: (a) clarity,
          (b) whether next_action is specific enough for the assigned
          team to act, (c) whether uncertainty is honestly stated.
          Be terse. Actionable critique only.

  outputs:
    assets:
      - intake
      - repo_evidence
      - reproduction
      - docs_evidence
      - history_evidence
      - preliminary
      - skeptic_debate
      - decision
      - report
    text_sinks:
      - from: report
        path: "$REPORT_DIR/investigation_{partition.owner}_{partition.repo}_{partition.issue_number}.md"
EOF
ok "wrote maintainer_investigation/defs.yaml (AgenticPipelineComponent — comprehensive 9-step, per_step_ops, using tool_use_loop + handoff + debate + critique_loop)"

# --- 7. defs.yaml: Slack approval gate (optional — falls back gracefully) --
if [ -n "$SLACK_BOT_TOKEN" ] && [ -n "$SLACK_APPROVER_USER_IDS" ]; then
  IFS=',' read -ra IDS <<< "$SLACK_APPROVER_USER_IDS"
  APPROVER_YAML=""
  for id in "${IDS[@]}"; do APPROVER_YAML+="
    - $(echo "$id" | xargs)"; done
  mkdir -p "src/$PKG/defs/report_approval_slack"
  cat > "src/$PKG/defs/report_approval_slack/defs.yaml" <<EOF
type: dagster_community_components.SlackApprovalGateComponent
attributes:
  asset_name: mir_report_approval_slack
  upstream_asset_key: mir_report
  approval_dir: "$APPROVAL_DIR"
  slack_channel: "$SLACK_CHANNEL"
  slack_bot_token_env_var: SLACK_BOT_TOKEN
  approve_emoji: white_check_mark
  reject_emoji: x
  ping_users_on_post:$APPROVER_YAML
  required_approvers: $REQUIRED_APPROVERS
  approver_allowlist:$APPROVER_YAML
  poll_interval_seconds: 30
  partition_type: dynamic
  dynamic_partition_name: mir_investigations
EOF
  ok "wrote report_approval_slack/defs.yaml (SlackApprovalGateComponent — Slack quorum HITL)"
else
  info "Slack env vars unset — skipping SlackApprovalGateComponent (falling back to file-drop-only HITL). Set SLACK_BOT_TOKEN + SLACK_APPROVER_USER_IDS to include."
fi

# --- 8. defs.yaml: HumanApprovalGate + FilesystemMonitor + ship job -------
mkdir -p "src/$PKG/defs/report_approval"
cat > "src/$PKG/defs/report_approval/defs.yaml" <<EOF
type: dagster_community_components.HumanApprovalGateComponent
attributes:
  asset_name: mir_report_approval
  upstream_asset_key: mir_report
  approval_dir: "$APPROVAL_DIR"
  partition_type: dynamic
  dynamic_partition_name: mir_investigations
  group_name: investigation
  kinds: [human, approval]
EOF
ok "wrote report_approval/defs.yaml (HumanApprovalGateComponent — asset-check gate)"

mkdir -p "src/$PKG/defs/approval_watcher"
cat > "src/$PKG/defs/approval_watcher/defs.yaml" <<EOF
type: dagster_community_components.FilesystemMonitorSensorComponent
attributes:
  sensor_name: approval_watcher_v3
  directory_path: "$APPROVAL_DIR"
  file_pattern: '.*\.json\$'
  job_name: ship_mir_report_v3_job
  minimum_interval_seconds: 5
  default_status: running
EOF
ok "wrote approval_watcher/defs.yaml (FilesystemMonitorSensor)"

mkdir -p "src/$PKG/defs/ship_report_v3_job"
cat > "src/$PKG/defs/ship_report_v3_job/defs.yaml" <<EOF
type: dagster_community_components.AssetJobComponent
attributes:
  job_name: ship_mir_report_v3_job
  asset_keys: [mir_report_approval]
EOF
ok "wrote ship_report_v3_job/defs.yaml (AssetJobComponent — sensor target)"

# --- 9. validate ----------------------------------------------------------
info "dg check defs…"
uv run dagster definitions validate 2>&1 | tail -5 || fail "definitions failed to load"

# --- 10. materialize (config-driven via launcher) -------------------------
DM="${PKG}.definitions"

info "launching triage for issue #${DAGSTER_ISSUE_NUM} via config-driven launcher…"
cat > /tmp/mir_v3_launch_${DAGSTER_ISSUE_NUM}.yaml <<CFG
ops:
  launch_mir_triage_v3_op:
    config:
      owner: dagster-io
      repo: dagster
      issue_number: ${DAGSTER_ISSUE_NUM}
CFG
uv run dagster job execute -m "$DM" -j launch_mir_triage_v3 --config /tmp/mir_v3_launch_${DAGSTER_ISSUE_NUM}.yaml 2>&1 | tail -5 || fail "triage failed"

REPORT_FILE="$REPORT_DIR/investigation_dagster-io_dagster_${DAGSTER_ISSUE_NUM}.md"
ok "triage complete — see $REPORT_FILE"

echo
info "─── report preview (first 40 lines) ───"
head -40 "$REPORT_FILE" 2>/dev/null || echo "(no draft yet)"
echo "───────────────────────────────────────"
echo

# --- 11. summary ----------------------------------------------------------
ok "MIR-v3 demo complete."
cat <<EOF

Every new primitive shipped in v0.10.69–v0.10.73 is running end-to-end
against a real GitHub issue:

  ✓ PartitionedAssetLauncherJobComponent — config-driven entry
  ✓ AgenticPipelineComponent with per_step_ops=true
    ✓ fastmcp transport in mir_intake
    ✓ tool_use_loop op in mir_repo_evidence
    ✓ handoff op → LangGraph in mir_reproduction
    ✓ debate op (3 skeptics + arbitrator) in mir_skeptic_debate
    ✓ critique_loop op (drafter + critic, ≤ 2 iters, early-stop @ score≥85) in mir_report
    ✓ synthesize op with typed inputs in mir_preliminary + mir_decision
  ✓ Dynamic partitions + composite partition_key_parser
$([ -n "$SLACK_BOT_TOKEN" ] && echo "  ✓ SlackApprovalGateComponent — Slack quorum HITL")
  ✓ HumanApprovalGateComponent — asset check gate
  ✓ FilesystemMonitorSensorComponent — auto-progression
  ✓ AssetJobComponent — sensor target

Open dg dev to see the graph + Runs page (11 ops per triage — one per step):
  cd $PROJECT_ABS
  DAGSTER_HOME=$DAGSTER_HOME uv run dg dev

Try another issue via the launcher:
  cat > /tmp/mir_v3_launch.yaml <<CFG
  ops:
    launch_mir_triage_v3_op:
      config:
        owner: prefecthq
        repo: prefect
        issue_number: 12345
  CFG
  DAGSTER_HOME=$DAGSTER_HOME uv run dagster job execute -m $DM -j launch_mir_triage_v3 --config /tmp/mir_v3_launch.yaml
EOF
