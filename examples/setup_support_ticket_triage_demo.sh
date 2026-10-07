#!/usr/bin/env bash
# support_ticket_triage — agent categorizes tickets by severity (structured
# output), a REAL existing FilterComponent deterministically keeps only the
# severe ones (via invoke_component -- no reimplementation, no new lineage
# node), then delegate dynamically routes to whichever registered agent is
# actually tagged capabilities: [triage] -- with a second, deliberately
# wrong-capability agent registered alongside it to prove the filtering
# really works.
#
# Shows off:
#   - map with output_schema + output_join: records -- forced structured
#     per-item output (category + reason), not hopeful free text.
#   - invoke_component -- calls dagster_community_components.FilterComponent's
#     REAL asset compute function directly, in-process, against this step's
#     data. No new Dagster asset/lineage node for the filter itself.
#   - agent_card + delegate -- TWO registered AgentCardComponent instances
#     (triage_agent: capabilities [triage], billing_agent: capabilities
#     [lookup]); delegate's required_capabilities excludes billing_agent
#     BEFORE the picker LLM even runs.
#   - The "agent" is just an LLM call behind an MCP tool with its own
#     persona -- no external service required, just an API key.
#
# Total cost: ~$0.002/run (gpt-4o-mini, ~8 LLM calls for 6 tickets + triage).

set -eo pipefail

PROJECT_DIR="${1:-support-ticket-triage-demo}"

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
                           # fixture MCP server uses -- pin below 2.
uv add -q 'yarl<1.24'
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components: agentic_pipeline + agent_card + filter"
$CLI add agentic_pipeline --auto-install >/dev/null 2>&1
$CLI add agent_card --auto-install >/dev/null 2>&1
$CLI add filter --auto-install >/dev/null 2>&1
rm -rf "src/$PKG/defs/agentic_pipeline" "src/$PKG/defs/agent_card" "src/$PKG/defs/filter"

echo ">>> Writing ticket data (frozen snapshot from SyntheticDataGeneratorComponent's support_tickets schema)"
mkdir -p "src/$PKG/defs/ticket_triage"
cat > "src/$PKG/defs/ticket_triage/tickets.json" <<'TICKETS'
[
  {"ticket_id": "T00001", "customer_id": "CUST6527", "channel": "email", "priority": "medium", "ticket_text": "Can I get an enterprise quote? Contact: alice.johnson@enterprise.org or 555-4300."},
  {"ticket_id": "T00002", "customer_id": "CUST8282", "channel": "web", "priority": "low", "ticket_text": "Site is down for me -- getting 502 errors since 9am EST."},
  {"ticket_id": "T00003", "customer_id": "CUST6829", "channel": "email", "priority": "high", "ticket_text": "Site is down for me -- getting 502 errors since 9am EST."},
  {"ticket_id": "T00004", "customer_id": "CUST7885", "channel": "email", "priority": "high", "ticket_text": "Bug report: search results show duplicates when filtering by date range."},
  {"ticket_id": "T00005", "customer_id": "CUST9328", "channel": "web", "priority": "medium", "ticket_text": "My order #11451 hasn't arrived. Can you check status?"},
  {"ticket_id": "T00006", "customer_id": "CUST2310", "channel": "email", "priority": "urgent", "ticket_text": "Account security alert -- got an email about login from Russia, was that real?"}
]
TICKETS

echo ">>> Writing the triage agent (LLM call behind an MCP tool, with its own persona)"
cat > "src/$PKG/defs/ticket_triage/triage_mcp_server.py" <<'PYEOF'
#!/usr/bin/env python3
"""The triage agent: an LLM call wrapped behind an MCP tool with its own
system prompt -- no external service required, just an API key."""
import asyncio, json, os
import mcp.server.stdio
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

TRIAGE_SYSTEM_PROMPT = (
    "You are a senior support triage specialist. Given severe support "
    "ticket context, decide which engineering team should own it and how "
    "urgent it is. Call the triage_decision tool with your decision. "
    "Teams: infra, payments, data. Urgency: P0 (drop everything), P1 "
    "(today), P2 (this week)."
)
TRIAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "triage_decision",
        "description": "Record the triage decision.",
        "parameters": {"type": "object", "properties": {
            "team": {"type": "string", "enum": ["infra", "payments", "data"]},
            "urgency": {"type": "string", "enum": ["P0", "P1", "P2"]},
            "reasoning": {"type": "string"},
        }, "required": ["team", "urgency", "reasoning"]},
    },
}
server = Server("triage-mcp")


def _run_triage_llm(ticket_context: str) -> dict:
    import litellm
    resp = litellm.completion(
        model="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"],
        messages=[{"role": "system", "content": TRIAGE_SYSTEM_PROMPT}, {"role": "user", "content": ticket_context}],
        tools=[TRIAGE_TOOL], tool_choice="required", temperature=0.0,
    )
    return json.loads(resp.choices[0].message.tool_calls[0].function.arguments)


@server.list_tools()
async def list_tools():
    return [Tool(name="triage_ticket", description="Assign owning team + urgency to severe tickets.",
                 inputSchema={"type": "object", "properties": {"ticket_context": {"type": "string"}}, "required": ["ticket_context"]})]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "triage_ticket":
        raise ValueError(f"unknown tool {name!r}")
    return [TextContent(type="text", text=json.dumps(_run_triage_llm(arguments.get("ticket_context", ""))))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name="triage-mcp", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
        ))


if __name__ == "__main__":
    asyncio.run(main())
PYEOF

echo ">>> Writing agent cards (triage_agent + a deliberately wrong-capability billing_agent)"
# IMPORTANT: Dagster's component scanner only loads a component from a file
# literally named defs.yaml, one per directory -- each AgentCardComponent
# needs its OWN sibling directory under defs/, not just another YAML file
# dropped next to the pipeline's own defs.yaml. delegate's sibling-discovery
# scans the shared PARENT directory (defs/), so both agents need to live
# under defs/ alongside (not inside) defs/ticket_triage/.
mkdir -p "src/$PKG/defs/triage_agent" "src/$PKG/defs/billing_agent"

cat > "src/$PKG/defs/triage_agent/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: triage_agent
  name: "Ticket Triage Agent"
  description: "Assigns an owning team and urgency to severe support tickets."
  skills:
    - id: triage_ticket
      name: "Triage ticket"
      description: "Given ticket context, returns owning team + urgency."
      tags: [support]
  capabilities: [triage]
  invocation:
    mcp_server:
      name: triage-mcp
      type: stdio
      command: [python, "src/$PKG/defs/ticket_triage/triage_mcp_server.py"]
    tool_name: triage_ticket
    tool_args_template: {ticket_context: "{extra.src_text}"}
EOF

cat > "src/$PKG/defs/billing_agent/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: billing_lookup_agent
  name: "Billing Lookup Agent"
  description: "Looks up a customer's billing history and invoices."
  skills:
    - id: lookup_invoice
      name: "Lookup invoice"
      description: "Given a customer id, returns invoice history."
      tags: [billing]
  capabilities: [lookup]
  invocation:
    http:
      url: https://internal.example.com/agents/billing
EOF

echo ">>> Writing pipeline.yaml (map → invoke_component → delegate)"
cat > "src/$PKG/defs/ticket_triage/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: ticket_triage
  group_name: ticket_triage
  source:
    kind: file
    path: src/$PKG/defs/ticket_triage/tickets.json

  steps:
    - id: categorize
      op: map
      source: source
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      prompt_template: "Classify this support ticket's severity.\n\n{item}"
      output_schema:
        type: object
        properties:
          category: {type: string, enum: [severe, moderate, minor]}
          reason: {type: string}
        required: [category, reason]
      output_join: records
      max_concurrent: 4

    - id: filter_severe
      op: invoke_component
      source: categorize
      component_type: $PKG.components.filter.component.FilterComponent
      attributes:
        asset_name: placeholder
        upstream_asset_key: placeholder/upstream
        condition: 'category == "severe"'

    - id: triaged
      op: delegate
      source: filter_severe
      task: "Triage this batch of severe support tickets: assign owning team + urgency."
      required_capabilities: [triage]
      picker:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY

  outputs:
    assets: [categorize, filter_severe, triaged]
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev                                     # open UI at http://localhost:3000

Or materialize headlessly (~\$0.002):

  uv run dg launch --assets '*'

Then in the UI:
  - Click ticket_triage_categorize → metadata shows every ticket's forced
    structured {category, reason}.
  - Click ticket_triage_filter_severe → only the severe tickets survived,
    via a REAL FilterComponent call (invoke_component), no new lineage node.
  - Click ticket_triage_triaged → picked_agent_id (always triage_agent --
    billing_lookup_agent is excluded by required_capabilities before the
    picker LLM even runs) + the real MCP-backed agent's decision.
MSG
