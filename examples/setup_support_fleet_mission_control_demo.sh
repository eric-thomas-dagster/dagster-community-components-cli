#!/usr/bin/env bash
# support_fleet_mission_control — the "at scale" story, finally exercised
# for real: categorize a mixed batch of tickets, deterministically split by
# category, then route EACH category to the right specialist out of a
# SIX-agent fleet loaded from one external manifest via
# AgentCardWorkspaceComponent (built earlier, never actually run end-to-end
# until this demo) -- two of the six agents (legal, enterprise) are never
# touched, proving required_capabilities really does pick the right one out
# of a bigger registry, not just the only one available. A final synthesize
# step rolls all four specialists' response plans into one daily digest.
#
# Shows off, combined into one pipeline for the first time:
#   - map (structured output) for per-ticket categorization
#   - invoke_component (real FilterComponent, 4x) for deterministic splits
#   - AgentCardWorkspaceComponent -- bulk-loads a 6-agent fleet from ONE
#     manifest.json instead of 6 hand-written AgentCardComponent YAMLs
#   - delegate with required_capabilities, 4 separate routing decisions
#     each correctly narrowed to exactly 1 of the 6 registered agents
#   - synthesize -- fan-in all 4 specialist responses into one digest
#
# Total cost: ~$0.01/run (gpt-4o-mini, ~17 LLM calls: 12 categorize + 4
# picker + 4 specialist + 1 digest -- picks run cheap since each category
# narrows to exactly 1 candidate before any reasoning is needed).

set -eo pipefail

PROJECT_DIR="${1:-support-fleet-mission-control-demo}"

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

echo ">>> Installing components: agentic_pipeline + agent_card_workspace + filter"
$CLI add agentic_pipeline --auto-install >/dev/null 2>&1
$CLI add agent_card_workspace --auto-install >/dev/null 2>&1
$CLI add filter --auto-install >/dev/null 2>&1
rm -rf "src/$PKG/defs/agentic_pipeline" "src/$PKG/defs/agent_card_workspace" "src/$PKG/defs/filter"

echo ">>> Writing the fleet (one manifest, six specialist agents, one shared MCP server)"
mkdir -p "src/$PKG/defs/fleet"
cat > "src/$PKG/defs/fleet/fleet_agent_server.py" <<'PYEOF'
#!/usr/bin/env python3
"""One script, six specialist personas -- selected by argv[1]. Each is
registered as its own card in the fleet manifest.json, all pointing at
this same script with a different argv. Real LLM calls, no canned
responses."""
import asyncio, json, os, sys
import mcp.server.stdio
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

PERSONAS = {
    "billing": "You are a billing support specialist. Given a batch of billing-related support tickets, draft a brief, helpful response plan for each one, in a short numbered list.",
    "technical": "You are a technical support specialist. Given a batch of technical/bug-report support tickets, draft a brief, helpful response plan for each one, in a short numbered list.",
    "security": "You are a security support specialist. Given a batch of security-related support tickets (account access, suspicious activity), draft a brief, helpful response plan for each one, in a short numbered list.",
    "refunds": "You are a refunds specialist. Given a batch of refund-request support tickets, draft a brief, helpful response plan for each one, in a short numbered list.",
    "legal": "You are a legal/compliance specialist. Given a batch of legal or compliance-related support tickets, draft a brief response plan for each one, in a short numbered list.",
    "enterprise": "You are an enterprise accounts specialist. Given a batch of enterprise-customer support tickets, draft a brief response plan for each one, in a short numbered list.",
}
persona_name = sys.argv[1] if len(sys.argv) > 1 else "technical"
SYSTEM_PROMPT = PERSONAS[persona_name]
server = Server(f"fleet-{persona_name}-mcp")


def _run_llm(context_text: str) -> str:
    import litellm
    resp = litellm.completion(model="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"],
                               messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": context_text}], temperature=0.2)
    return resp.choices[0].message.content


@server.list_tools()
async def list_tools():
    return [Tool(name="handle_tickets", description=f"Draft a {persona_name} response plan for a batch of tickets.",
                 inputSchema={"type": "object", "properties": {"tickets_context": {"type": "string"}}, "required": ["tickets_context"]})]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "handle_tickets":
        raise ValueError(f"unknown tool {name!r}")
    response = _run_llm(arguments.get("tickets_context", ""))
    return [TextContent(type="text", text=json.dumps({"response": response, "specialist": persona_name}))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name=f"fleet-{persona_name}-mcp", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
        ))


if __name__ == "__main__":
    asyncio.run(main())
PYEOF

cat > "src/$PKG/defs/fleet/manifest.json" <<EOF
[
  {"agent_id": "billing_specialist", "name": "Billing Specialist", "description": "Handles billing, invoicing, and subscription-charge support tickets.", "skills": [{"id": "handle_billing", "name": "Handle billing tickets", "description": "Drafts response plans for billing-related tickets.", "tags": ["billing"]}], "capabilities": ["billing"], "invocation": {"mcp_server": {"name": "fleet-billing-mcp", "type": "stdio", "command": ["python", "src/$PKG/defs/fleet/fleet_agent_server.py", "billing"]}, "tool_name": "handle_tickets", "tool_args_template": {"tickets_context": "{extra.src_text}"}}},
  {"agent_id": "technical_specialist", "name": "Technical Specialist", "description": "Handles bug reports, integration issues, and technical errors.", "skills": [{"id": "handle_technical", "name": "Handle technical tickets", "description": "Drafts response plans for technical/bug-report tickets.", "tags": ["technical"]}], "capabilities": ["technical"], "invocation": {"mcp_server": {"name": "fleet-technical-mcp", "type": "stdio", "command": ["python", "src/$PKG/defs/fleet/fleet_agent_server.py", "technical"]}, "tool_name": "handle_tickets", "tool_args_template": {"tickets_context": "{extra.src_text}"}}},
  {"agent_id": "security_specialist", "name": "Security Specialist", "description": "Handles account security, suspicious activity, and access issues.", "skills": [{"id": "handle_security", "name": "Handle security tickets", "description": "Drafts response plans for security-related tickets.", "tags": ["security"]}], "capabilities": ["security"], "invocation": {"mcp_server": {"name": "fleet-security-mcp", "type": "stdio", "command": ["python", "src/$PKG/defs/fleet/fleet_agent_server.py", "security"]}, "tool_name": "handle_tickets", "tool_args_template": {"tickets_context": "{extra.src_text}"}}},
  {"agent_id": "refunds_specialist", "name": "Refunds Specialist", "description": "Handles refund requests and payment disputes.", "skills": [{"id": "handle_refunds", "name": "Handle refund tickets", "description": "Drafts response plans for refund-request tickets.", "tags": ["refunds"]}], "capabilities": ["refunds"], "invocation": {"mcp_server": {"name": "fleet-refunds-mcp", "type": "stdio", "command": ["python", "src/$PKG/defs/fleet/fleet_agent_server.py", "refunds"]}, "tool_name": "handle_tickets", "tool_args_template": {"tickets_context": "{extra.src_text}"}}},
  {"agent_id": "legal_specialist", "name": "Legal/Compliance Specialist", "description": "Handles legal, compliance, and regulatory support tickets. Not used by this demo's 4 routed categories -- present to prove the fleet can be larger than what any one pipeline actually routes to.", "skills": [{"id": "handle_legal", "name": "Handle legal tickets", "description": "Drafts response plans for legal/compliance tickets.", "tags": ["legal"]}], "capabilities": ["legal"], "invocation": {"mcp_server": {"name": "fleet-legal-mcp", "type": "stdio", "command": ["python", "src/$PKG/defs/fleet/fleet_agent_server.py", "legal"]}, "tool_name": "handle_tickets", "tool_args_template": {"tickets_context": "{extra.src_text}"}}},
  {"agent_id": "enterprise_specialist", "name": "Enterprise Accounts Specialist", "description": "Handles enterprise-tier customer support tickets. Not used by this demo's 4 routed categories.", "skills": [{"id": "handle_enterprise", "name": "Handle enterprise tickets", "description": "Drafts response plans for enterprise-customer tickets.", "tags": ["enterprise"]}], "capabilities": ["enterprise"], "invocation": {"mcp_server": {"name": "fleet-enterprise-mcp", "type": "stdio", "command": ["python", "src/$PKG/defs/fleet/fleet_agent_server.py", "enterprise"]}, "tool_name": "handle_tickets", "tool_args_template": {"tickets_context": "{extra.src_text}"}}}
]
EOF

cat > "src/$PKG/defs/fleet/defs.yaml" <<EOF
type: $PKG.components.agent_card_workspace.component.AgentCardWorkspaceComponent
attributes:
  manifest_path: src/$PKG/defs/fleet/manifest.json
  group_name: fleet
EOF

echo ">>> Writing ticket batch + the 11-step mission_control pipeline"
mkdir -p "src/$PKG/defs/mission_control"
cat > "src/$PKG/defs/mission_control/tickets.json" <<'TICKETS'
[
  {"ticket_id": "T001", "text": "I was charged twice for my subscription this month, can you refund the extra charge?"},
  {"ticket_id": "T002", "text": "My invoice shows a different amount than what's listed on the pricing page."},
  {"ticket_id": "T003", "text": "Can you explain why my credit card was declined during renewal?"},
  {"ticket_id": "T004", "text": "The API keeps returning a 500 error when I POST to /v2/orders with more than 10 items."},
  {"ticket_id": "T005", "text": "Search results show duplicate rows when filtering by date range -- looks like a bug."},
  {"ticket_id": "T006", "text": "Our webhook integration stopped firing after yesterday's deploy, nothing changed on our end."},
  {"ticket_id": "T007", "text": "I got an email about a login from a new country, was that really me or should I be worried?"},
  {"ticket_id": "T008", "text": "Someone tried to reset my password three times today and I didn't request it."},
  {"ticket_id": "T009", "text": "The product I received arrived damaged, I'd like a refund for order #38291."},
  {"ticket_id": "T010", "text": "I was charged for a plan upgrade I never approved -- please reverse this charge."},
  {"ticket_id": "T011", "text": "What's the difference between the free plan and the pro plan?"},
  {"ticket_id": "T012", "text": "Do you have a dark mode option in the mobile app?"}
]
TICKETS

cat > "src/$PKG/defs/mission_control/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: mission_control
  group_name: mission_control
  source:
    kind: file
    path: src/$PKG/defs/mission_control/tickets.json

  steps:
    - id: categorize
      op: map
      source: source
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      prompt_template: "Classify this support ticket's category.\n\n{item}"
      output_schema:
        type: object
        properties:
          category: {type: string, enum: [billing, technical, security, refunds, other]}
          reason: {type: string}
        required: [category, reason]
      output_join: records
      max_concurrent: 4

    - id: filter_billing
      op: invoke_component
      source: categorize
      component_type: $PKG.components.filter.component.FilterComponent
      attributes: {asset_name: placeholder, upstream_asset_key: placeholder/upstream, condition: 'category == "billing"'}

    - id: filter_technical
      op: invoke_component
      source: categorize
      component_type: $PKG.components.filter.component.FilterComponent
      attributes: {asset_name: placeholder, upstream_asset_key: placeholder/upstream, condition: 'category == "technical"'}

    - id: filter_security
      op: invoke_component
      source: categorize
      component_type: $PKG.components.filter.component.FilterComponent
      attributes: {asset_name: placeholder, upstream_asset_key: placeholder/upstream, condition: 'category == "security"'}

    - id: filter_refunds
      op: invoke_component
      source: categorize
      component_type: $PKG.components.filter.component.FilterComponent
      attributes: {asset_name: placeholder, upstream_asset_key: placeholder/upstream, condition: 'category == "refunds"'}

    - id: route_billing
      op: delegate
      source: filter_billing
      task: "Handle this batch of billing support tickets."
      required_capabilities: [billing]
      picker: {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY}

    - id: route_technical
      op: delegate
      source: filter_technical
      task: "Handle this batch of technical support tickets."
      required_capabilities: [technical]
      picker: {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY}

    - id: route_security
      op: delegate
      source: filter_security
      task: "Handle this batch of security support tickets."
      required_capabilities: [security]
      picker: {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY}

    - id: route_refunds
      op: delegate
      source: filter_refunds
      task: "Handle this batch of refund support tickets."
      required_capabilities: [refunds]
      picker: {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY}

    - id: digest
      op: synthesize
      sources: [route_billing, route_technical, route_security, route_refunds]
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: "Combine these four specialists' response plans into one short daily support digest, with one section per specialist."
      max_tokens: 600

  outputs:
    assets: [categorize, filter_billing, filter_technical, filter_security, filter_refunds, route_billing, route_technical, route_security, route_refunds, digest]
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev                                     # open UI at http://localhost:3000

Or materialize headlessly (~\$0.01):

  uv run dg launch --assets '*'

Then in the UI:
  - Click mission_control_categorize → 12 tickets, each with a forced
    {category, reason}.
  - Click mission_control_filter_billing / _technical / _security /
    _refunds → each a real FilterComponent call, no new lineage node.
  - Click mission_control_route_billing (etc.) → picked_agent_id is
    ALWAYS the matching specialist -- legal_specialist and
    enterprise_specialist (2 of the 6 registered agents) are never
    picked, because required_capabilities excludes them before the
    picker LLM even runs.
  - Click mission_control_digest → one daily digest synthesized from
    all four specialists' response plans.
  - Click any asset under group "fleet" → six registered agents, loaded
    from ONE manifest.json via AgentCardWorkspaceComponent -- add a
    seventh specialist by adding one more entry to manifest.json, zero
    pipeline edits.
MSG
