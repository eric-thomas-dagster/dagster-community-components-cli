#!/usr/bin/env bash
# pipeline_incident_triage — agentic_pipeline's tool_use_loop op, with THREE
# REAL tools: a live vendor status-page check, a real `git log`, a real
# runbook search. One agent investigates an incident freely (not a fixed
# script) until it has enough evidence, then a second step forces its
# free-text diagnosis into clean {root_cause, action, reasoning} JSON.
#
# Inspired by a real cross-stack pipeline-incident-triage tool: when
# something breaks, synthesize vendor status + institutional knowledge +
# recent deploys into a root cause AND a decision -- critically, "wait,
# don't debug" vs. actually troubleshoot, since debugging a vendor outage
# wastes engineering time the vendor is already spending.
#
# Shows off:
#   - tool_use_loop: ONE agent, THREE MCP tools, iterating freely (not a
#     fixed call sequence) until it calls `finalize`.
#   - All three tools do REAL work: check_vendor_status hits the actual
#     public Statuspage.io-backed status API for github/fivetran/dbt/
#     snowflake (the same endpoints real uptime-monitoring integrations
#     use); get_recent_commits runs a real `git log` against this very
#     project; search_runbooks does a real keyword search over a small
#     institutional-knowledge fixture.
#   - extract: turns the agent's labeled free text into clean structured
#     JSON (root_cause / action / reasoning) -- composing two ops.
#   - Full tool-call trajectory (which tools, what args, what came back)
#     lands in the investigate asset's metadata -- the audit trail a real
#     incident-triage tool needs.
#
# Total cost: ~$0.001/run (gpt-4o-mini, ~4 LLM calls across both steps).

set -eo pipefail

PROJECT_DIR="${1:-pipeline-incident-triage-demo}"

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
uv add -q 'mcp>=1.0.0,<2'  # mcp>=2.0 renamed the server-side API this demo's
                           # fixture MCP server uses (Server/@list_tools()) --
                           # pin below 2 or the fixture server breaks on import.
uv add -q 'yarl<1.24'      # workaround: yarl 1.24.0 only ships cp310 wheels
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing component: agentic_pipeline"
$CLI add agentic_pipeline --auto-install >/dev/null 2>&1

# The CLI drops a copy of agentic_pipeline's own example.yaml at
# src/$PKG/defs/agentic_pipeline/defs.yaml. We're writing our own below.
rm -rf "src/$PKG/defs/agentic_pipeline"

echo ">>> Writing the dispatch MCP server (three real tools)"
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

echo ">>> Writing incident_triage/defs.yaml (tool_use_loop + extract)"
cat > "src/$PKG/defs/incident_triage/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: incident_triage
  group_name: incident_triage
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

  outputs:
    assets: [investigate, diagnosis]
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev                                     # open UI at http://localhost:3000

Or materialize headlessly (~\$0.001):

  uv run dg launch --assets '*'

Then in the UI:
  - Click incident_triage_investigate → metadata shows tool_call_trace
    (every tool call: which one, what args, what came back, latency) plus
    n_llm_calls / cost_usd / latency_ms.
  - Click incident_triage_diagnosis → clean {root_cause, action, reasoning}
    JSON, forced-structured from investigate's free-text answer.
  - Edit source.text in defs/incident_triage/defs.yaml to try a different
    incident -- e.g. mention GitHub Actions instead of Snowflake, or a
    dbt compilation error instead of a timeout, and watch which tools the
    agent reaches for and what it concludes.

Vendor status is checked LIVE against the real public status pages at
run time -- if you re-run this later, the diagnosis may genuinely differ
depending on real-world vendor status, not because anything was changed.
MSG
