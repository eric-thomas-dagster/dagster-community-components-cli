#!/usr/bin/env bash
# route_to_specialist — three registered specialist agents (billing/
# technical/general), delegate with NO required_capabilities set -- proves
# the picker LLM does genuine semantic matching (reads each candidate's
# skills/description and judges fit from the question's actual content),
# not just tag filtering.
#
# Total cost: ~$0.0005/run (gpt-4o-mini, 2 LLM calls).

set -eo pipefail

PROJECT_DIR="${1:-route-to-specialist-demo}"

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

echo ">>> Writing the specialist agent server (one script, three personas)"
mkdir -p "src/$PKG/defs/billing_specialist" "src/$PKG/defs/technical_specialist" "src/$PKG/defs/general_specialist"
for persona in billing technical general; do
  cp_target="src/$PKG/defs/${persona}_specialist/specialist_agent_server.py"
  cat > "$cp_target" <<'PYEOF'
#!/usr/bin/env python3
"""One script, three personas -- selected by argv[1]. Each persona is
registered as its own AgentCardComponent pointing at this same script
with a different argv."""
import asyncio, json, os, sys
import mcp.server.stdio
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

PERSONAS = {
    "billing": "You are a billing support specialist. Answer questions about charges, invoices, refunds, and subscriptions clearly and helpfully, in 2-4 sentences.",
    "technical": "You are a technical support specialist. Answer questions about product bugs, integrations, APIs, and technical errors clearly and helpfully, in 2-4 sentences.",
    "general": "You are a general support specialist. Answer questions that don't fit billing or technical support clearly and helpfully, in 2-4 sentences.",
}
persona_name = sys.argv[1] if len(sys.argv) > 1 else "general"
SYSTEM_PROMPT = PERSONAS[persona_name]
server = Server(f"specialist-{persona_name}-mcp")


def _run_llm(question: str) -> str:
    import litellm
    resp = litellm.completion(model="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"],
                               messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}], temperature=0.2)
    return resp.choices[0].message.content


@server.list_tools()
async def list_tools():
    return [Tool(name="draft_response", description=f"Draft a {persona_name} support response.",
                 inputSchema={"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]})]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "draft_response":
        raise ValueError(f"unknown tool {name!r}")
    response = _run_llm(arguments.get("question", ""))
    return [TextContent(type="text", text=json.dumps({"response": response, "specialist": persona_name}))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name=f"specialist-{persona_name}-mcp", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
        ))


if __name__ == "__main__":
    asyncio.run(main())
PYEOF
done

cat > "src/$PKG/defs/billing_specialist/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: billing_specialist
  name: "Billing Specialist"
  description: "Handles questions about charges, invoices, refunds, and subscriptions."
  skills:
    - id: draft_billing_response
      name: "Draft billing response"
      description: "Given a customer question about billing/payments/invoices, drafts a helpful response."
      tags: [billing]
  invocation:
    mcp_server:
      name: specialist-billing-mcp
      type: stdio
      command: [python, "src/$PKG/defs/billing_specialist/specialist_agent_server.py", billing]
    tool_name: draft_response
    tool_args_template: {question: "{extra.src_text}"}
EOF

cat > "src/$PKG/defs/technical_specialist/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: technical_specialist
  name: "Technical Specialist"
  description: "Handles questions about bugs, integrations, APIs, and technical errors."
  skills:
    - id: draft_technical_response
      name: "Draft technical response"
      description: "Given a customer question about a bug/integration/API/technical error, drafts a helpful response."
      tags: [technical]
  invocation:
    mcp_server:
      name: specialist-technical-mcp
      type: stdio
      command: [python, "src/$PKG/defs/technical_specialist/specialist_agent_server.py", technical]
    tool_name: draft_response
    tool_args_template: {question: "{extra.src_text}"}
EOF

cat > "src/$PKG/defs/general_specialist/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: general_specialist
  name: "General Specialist"
  description: "Handles general questions that aren't specifically about billing or technical issues."
  skills:
    - id: draft_general_response
      name: "Draft general response"
      description: "Given a general customer question, drafts a helpful response."
      tags: [general]
  invocation:
    mcp_server:
      name: specialist-general-mcp
      type: stdio
      command: [python, "src/$PKG/defs/general_specialist/specialist_agent_server.py", general]
    tool_name: draft_response
    tool_args_template: {question: "{extra.src_text}"}
EOF

echo ">>> Writing pipeline.yaml (no required_capabilities -- pure semantic picking)"
mkdir -p "src/$PKG/defs/specialist_routing"
cat > "src/$PKG/defs/specialist_routing/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: specialist_routing
  group_name: specialist_routing
  source:
    kind: literal
    text: "I was charged twice for my subscription this month, can you help me get a refund for the extra charge?"

  steps:
    - id: routed
      op: delegate
      task: "Route this customer question to the right specialist and have them draft a response."
      picker:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY

  outputs:
    assets: [routed]
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev                                     # open UI at http://localhost:3000

Or materialize headlessly (~\$0.0005):

  uv run dg launch --assets '*'

Then in the UI, click specialist_routing_routed → picked_agent_id +
picker_reasoning (should pick billing_specialist for the default billing
question). Edit source.text in
src/$PKG/defs/specialist_routing/defs.yaml to try a technical or general
question instead and watch the pick change.
MSG
