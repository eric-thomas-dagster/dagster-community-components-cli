#!/usr/bin/env bash
# oncall_escalation_simulator — extends pipeline_incident_triage one stage
# further: after investigating with real tools and landing on a diagnosis,
# route the escalation to whichever ON-CALL TEAM actually owns it (infra /
# data / payments), with a team-appropriate drafted Slack message.
#
# Reuses pipeline_incident_triage's exact real tools (live vendor status
# check, real git log, real runbook search) -- this demo is "what happens
# after the diagnosis," not a different investigation mechanism.
#
# Shows off:
#   - tool_use_loop + extract (same composition as pipeline_incident_triage)
#   - A THIRD stage: delegate with NO required_capabilities, picking among
#     three on-call teams based on genuine semantic judgment about WHO
#     actually owns this kind of incident -- not always obvious (a dbt
#     model timeout caused by a Snowflake outage could reasonably be
#     "infra" or "data"; the picker's actual reasoning is shown, not
#     hidden, because that's the honest point of semantic picking over
#     a fixed owner-mapping table).
#
# Total cost: ~$0.002/run (gpt-4o-mini, ~5 LLM calls).

set -eo pipefail

PROJECT_DIR="${1:-oncall-escalation-simulator-demo}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi
if [ -z "$OPENAI_API_KEY" ]; then
  echo "✗ OPENAI_API_KEY not set — the pipeline calls OpenAI and will error at materialize time."
  echo "  Get a key at: https://platform.openai.com/api-keys"
  exit 1
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null 2>&1
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q litellm requests
uv add -q 'mcp>=1.0.0,<2'
uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components: agentic_pipeline + agent_card"
$CLI add agentic_pipeline --auto-install >/dev/null 2>&1
$CLI add agent_card --auto-install >/dev/null 2>&1
rm -rf "src/$PKG/defs/agentic_pipeline" "src/$PKG/defs/agent_card"

echo ">>> Writing the dispatch MCP server (same 3 real tools as pipeline_incident_triage)"
mkdir -p "src/$PKG/defs/incident_triage"
cat > "src/$PKG/defs/incident_triage/runbooks.json" <<'RUNBOOKS'
[
  {
    "id": "vendor-degradation-wait",
    "title": "Vendor status shows degraded/outage",
    "keywords": ["degraded", "outage", "down", "partial", "incident", "vendor", "snowflake", "fivetran", "dbt", "github"],
    "guidance": "If the vendor's own status page shows anything other than fully operational, WAIT rather than debug. Do not spend engineering time root-causing what is almost certainly a vendor-side issue. Re-check status every 15 minutes; escalate internally only if the vendor incident exceeds 2 hours or there's no vendor-acknowledged incident despite clear symptoms."
  },
  {
    "id": "dbt-compilation-error",
    "title": "dbt compilation error: missing or renamed column",
    "keywords": ["compilation", "column", "not found", "dbt", "model", "schema"],
    "guidance": "Usually caused by an upstream schema change that hasn't been reflected in the model yet. Check recent commits/PRs for migrations or source-table changes touching the model in question before assuming a vendor issue."
  },
  {
    "id": "fivetran-sync-failure",
    "title": "Fivetran sync failure",
    "keywords": ["fivetran", "sync", "connector", "extract"],
    "guidance": "Check Fivetran's own status page first. Syncs auto-retry on most vendor-side issues within 15 minutes; manual intervention is rarely needed unless the connector itself shows a broken/reauth-required state in the Fivetran dashboard."
  },
  {
    "id": "ci-deploy-failure",
    "title": "CI/CD deploy failure blocking pipeline updates",
    "keywords": ["github actions", "ci", "deploy", "workflow", "build failed"],
    "guidance": "Check GitHub's status page first. If GitHub is fully operational, the failure is almost certainly in the workflow itself -- inspect the specific job's logs rather than assuming infrastructure."
  }
]
RUNBOOKS

cat > "src/$PKG/defs/incident_triage/dispatch_mcp_server.py" <<'PYEOF'
#!/usr/bin/env python3
"""Minimal local MCP stdio server with three REAL tools for pipeline
incident triage. check_vendor_status hits the real public status API;
get_recent_commits runs a real `git log`; search_runbooks does a real
keyword search over runbooks.json. Nothing here is simulated."""
import asyncio
import json
import os
import subprocess

import requests

import mcp.server.stdio
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
RUNBOOKS_PATH = os.path.join(THIS_DIR, "runbooks.json")

VENDOR_STATUS_URLS = {
    "github": "https://www.githubstatus.com/api/v2/status.json",
    "fivetran": "https://status.fivetran.com/api/v2/status.json",
    "dbt": "https://status.getdbt.com/api/v2/status.json",
    "snowflake": "https://status.snowflake.com/api/v2/status.json",
}

server = Server("dispatch-mcp")


def _check_vendor_status(vendor: str) -> dict:
    url = VENDOR_STATUS_URLS.get(vendor.lower())
    if not url:
        return {"vendor": vendor, "status": "unknown", "description": f"No status endpoint configured for {vendor!r}."}
    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        return {"vendor": vendor, "status": data["status"]["indicator"], "description": data["status"]["description"], "source_url": url}
    except Exception as e:
        return {"vendor": vendor, "status": "error", "description": f"Could not reach status page: {e}"}


def _search_runbooks(query: str) -> dict:
    with open(RUNBOOKS_PATH) as f:
        runbooks = json.load(f)
    q_lower = query.lower()
    scored = [(sum(1 for kw in rb["keywords"] if kw in q_lower), rb) for rb in runbooks]
    scored = [(s, rb) for s, rb in scored if s > 0]
    scored.sort(key=lambda x: -x[0])
    matches = [rb for _, rb in scored[:2]]
    return {"query": query, "matches": matches if matches else "no matching runbook found"}


def _get_recent_commits(n: int, repo_path: str = None) -> dict:
    path = repo_path or THIS_DIR
    try:
        result = subprocess.run(["git", "log", f"-{n}", "--oneline", "--no-decorate"], cwd=path, capture_output=True, text=True, timeout=10, check=True)
        return {"repo_path": path, "commits": [ln for ln in result.stdout.splitlines() if ln.strip()]}
    except Exception as e:
        return {"repo_path": path, "error": str(e)}


@server.list_tools()
async def list_tools():
    return [
        Tool(name="check_vendor_status", description="Check a vendor's REAL live public status page (github, fivetran, dbt, snowflake).",
             inputSchema={"type": "object", "properties": {"vendor": {"type": "string", "enum": list(VENDOR_STATUS_URLS)}}, "required": ["vendor"]}),
        Tool(name="search_runbooks", description="Search internal runbooks/institutional knowledge for guidance matching an error description.",
             inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
        Tool(name="get_recent_commits", description="Get the N most recent git commits, to check for a recent deploy that might explain the failure.",
             inputSchema={"type": "object", "properties": {"n": {"type": "integer"}, "repo_path": {"type": "string"}}, "required": ["n"]}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "check_vendor_status":
        result = _check_vendor_status(arguments["vendor"])
    elif name == "search_runbooks":
        result = _search_runbooks(arguments["query"])
    elif name == "get_recent_commits":
        result = _get_recent_commits(arguments.get("n", 10), arguments.get("repo_path"))
    else:
        raise ValueError(f"unknown tool {name!r}")
    return [TextContent(type="text", text=json.dumps(result, default=str))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name="dispatch-mcp", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
        ))


if __name__ == "__main__":
    asyncio.run(main())
PYEOF

echo ">>> Writing the three on-call team agents (one script, three personas)"
mkdir -p "src/$PKG/defs/infra_oncall" "src/$PKG/defs/data_oncall" "src/$PKG/defs/payments_oncall"

for dir_persona in "infra_oncall infra" "data_oncall data" "payments_oncall payments"; do
  set -- $dir_persona
  dirname="$1"; persona="$2"
  cat > "src/$PKG/defs/${dirname}/oncall_agent_server.py" <<'PYEOF'
#!/usr/bin/env python3
"""One script, three on-call team personas -- selected by argv[1]."""
import asyncio, json, os, sys
import mcp.server.stdio
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

PERSONAS = {
    "infra": "You are the infrastructure on-call engineer. Given an incident diagnosis (root cause, recommended action, reasoning), draft a concise Slack-style escalation message for the infra on-call channel: what's happening, whether action is needed now or this can wait, and the immediate next step. 3-5 sentences.",
    "data": "You are the data/analytics on-call engineer. Given an incident diagnosis, draft a concise Slack-style escalation message for the data on-call channel, focused on which models/dashboards are affected and what downstream consumers should be told. 3-5 sentences.",
    "payments": "You are the payments on-call engineer. Given an incident diagnosis, draft a concise Slack-style escalation message for the payments on-call channel, focused on customer/revenue impact and urgency. 3-5 sentences.",
}
persona_name = sys.argv[1] if len(sys.argv) > 1 else "infra"
SYSTEM_PROMPT = PERSONAS[persona_name]
server = Server(f"oncall-{persona_name}-mcp")


def _run_llm(context_text: str) -> str:
    import litellm
    resp = litellm.completion(model="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"],
                               messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": context_text}], temperature=0.2)
    return resp.choices[0].message.content


@server.list_tools()
async def list_tools():
    return [Tool(name="draft_escalation", description=f"Draft an escalation message for the {persona_name} on-call team.",
                 inputSchema={"type": "object", "properties": {"diagnosis_context": {"type": "string"}}, "required": ["diagnosis_context"]})]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "draft_escalation":
        raise ValueError(f"unknown tool {name!r}")
    response = _run_llm(arguments.get("diagnosis_context", ""))
    return [TextContent(type="text", text=json.dumps({"message": response, "team": persona_name}))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name=f"oncall-{persona_name}-mcp", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
        ))


if __name__ == "__main__":
    asyncio.run(main())
PYEOF
  sed -i.bak "s/sys.argv\[1\] if len(sys.argv) > 1 else \"infra\"/sys.argv[1] if len(sys.argv) > 1 else \"${persona}\"/" "src/$PKG/defs/${dirname}/oncall_agent_server.py"
  rm -f "src/$PKG/defs/${dirname}/oncall_agent_server.py.bak"
done

cat > "src/$PKG/defs/infra_oncall/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: infra_oncall
  name: "Infra On-Call"
  description: "Handles infrastructure/warehouse/vendor-outage incidents."
  skills:
    - id: escalate_infra
      name: "Escalate (infra)"
      description: "Drafts an escalation message for infrastructure/vendor-outage incidents."
      tags: [infra]
  capabilities: [infra]
  invocation:
    mcp_server:
      name: oncall-infra-mcp
      type: stdio
      command: [python, "src/$PKG/defs/infra_oncall/oncall_agent_server.py", infra]
    tool_name: draft_escalation
    tool_args_template: {diagnosis_context: "{extra.src_text}"}
EOF

cat > "src/$PKG/defs/data_oncall/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: data_oncall
  name: "Data On-Call"
  description: "Handles data model/pipeline/dbt incidents."
  skills:
    - id: escalate_data
      name: "Escalate (data)"
      description: "Drafts an escalation message for data model/pipeline incidents."
      tags: [data]
  capabilities: [data]
  invocation:
    mcp_server:
      name: oncall-data-mcp
      type: stdio
      command: [python, "src/$PKG/defs/data_oncall/oncall_agent_server.py", data]
    tool_name: draft_escalation
    tool_args_template: {diagnosis_context: "{extra.src_text}"}
EOF

cat > "src/$PKG/defs/payments_oncall/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: payments_oncall
  name: "Payments On-Call"
  description: "Handles payment processing/checkout incidents."
  skills:
    - id: escalate_payments
      name: "Escalate (payments)"
      description: "Drafts an escalation message for payment processing/checkout incidents."
      tags: [payments]
  capabilities: [payments]
  invocation:
    mcp_server:
      name: oncall-payments-mcp
      type: stdio
      command: [python, "src/$PKG/defs/payments_oncall/oncall_agent_server.py", payments]
    tool_name: draft_escalation
    tool_args_template: {diagnosis_context: "{extra.src_text}"}
EOF

echo ">>> Writing incident_triage/defs.yaml (tool_use_loop + extract + delegate)"
cat > "src/$PKG/defs/incident_triage/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: oncall_sim
  group_name: oncall_sim
  source:
    kind: literal
    text: >
      Asset \`stg_orders\` failed to materialize: the dbt model's query
      against Snowflake timed out / connection reset. This started about
      20 minutes ago and is affecting multiple downstream assets. No
      recent code changes are suspected, but please confirm.

  steps:
    - id: investigate
      op: tool_use_loop
      system_prompt: >
        You are a pipeline incident triage agent. Given an incident report,
        gather evidence using your tools before concluding -- always check
        the relevant vendor's live status page if the incident could
        plausibly be vendor-side, search runbooks for institutional
        guidance, and check recent commits if a code/schema change could be
        the real cause. When you have enough evidence, call finalize with
        your diagnosis in exactly this format:

        ROOT_CAUSE: <one sentence>
        ACTION: wait or debug
        REASONING: <why, citing the specific evidence you gathered>
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      mcp_servers:
        - name: dispatch
          type: stdio
          command: [python, "src/$PKG/defs/incident_triage/dispatch_mcp_server.py"]
      max_iterations: 8

    - id: diagnosis
      op: extract
      source: investigate
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      output_schema:
        type: object
        properties:
          root_cause: {type: string}
          action: {type: string, enum: [wait, debug]}
          reasoning: {type: string}
        required: [root_cause, action, reasoning]

    - id: escalate
      op: delegate
      source: diagnosis
      task: "Given this incident diagnosis, draft an escalation message for the right on-call team."
      picker:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY

  outputs:
    assets: [investigate, diagnosis, escalate]
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev                                     # open UI at http://localhost:3000

Or materialize headlessly (~\$0.002):

  uv run dg launch --assets '*'

Then in the UI:
  - Click oncall_sim_investigate / _diagnosis → same real tool_use_loop
    investigation as the pipeline_incident_triage demo.
  - Click oncall_sim_escalate → picked_agent_id (one of infra_oncall /
    data_oncall / payments_oncall, genuinely semantically picked -- no
    forced mapping table) + picker_reasoning (why that team) + a
    team-appropriate drafted Slack message.
  - Edit source.text in src/$PKG/defs/incident_triage/defs.yaml to try
    a different incident and watch which team gets picked.
MSG
