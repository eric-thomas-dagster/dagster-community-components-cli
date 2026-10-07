#!/usr/bin/env bash
# document_summarization — one delegate step, one registered agent_card,
# forced structured output (summary + action_items) instead of free text.
# The simplest agent_card + delegate shape -- good starting point before
# support_ticket_triage's 3-step version.
#
# Total cost: ~$0.0005/run (gpt-4o-mini, 2 LLM calls).

set -eo pipefail

PROJECT_DIR="${1:-document-summarization-demo}"

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

echo ">>> Writing sample document"
mkdir -p "src/$PKG/defs/doc_summary"
cat > "src/$PKG/defs/doc_summary/document.txt" <<'DOCEOF'
Incident Postmortem: Checkout Latency Regression (2026-10-02)

Summary of events: Starting at approximately 14:20 UTC, p99 latency on the
checkout service rose from a baseline of ~180ms to over 4 seconds for
roughly 50 minutes. Customer-facing impact was a visible spinner during
payment confirmation; no payments were lost, but an estimated 3% of
checkout sessions were abandoned during the window.

Root cause: A configuration change deployed at 14:15 UTC increased the
connection pool size for the inventory-reservation service without a
corresponding increase in the downstream database's max_connections
setting. Once pool saturation hit, new requests queued instead of
failing fast, which compounded under load rather than shedding it.

Resolution: The connection pool change was rolled back at 15:04 UTC;
latency returned to baseline within 90 seconds of rollback completing.

Action items:
1. Require performance review sign-off for any change that touches
   connection pool sizing, not just schema or code changes.
2. Reduce the checkout-latency alerting window from 10 minutes to 3
   minutes, with a secondary fast-trip alert on p99 > 1s for 60 seconds.
3. Add a circuit breaker to the inventory-reservation client so pool
   saturation fails fast instead of queuing indefinitely.
4. Schedule a load test of the new pool size against the downstream
   database's actual max_connections before the next capacity change.
DOCEOF

echo ">>> Writing the summarizer agent (own sibling directory -- required for delegate's sibling-discovery to find it)"
mkdir -p "src/$PKG/defs/summarizer_agent"
cat > "src/$PKG/defs/summarizer_agent/summarizer_agent_server.py" <<'PYEOF'
#!/usr/bin/env python3
"""The summarizer agent: an LLM call behind an MCP tool, forced structured
output (summary + action_items), not free text."""
import asyncio, json, os
import mcp.server.stdio
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

SYSTEM_PROMPT = (
    "You are an executive assistant. Given a document, produce a short "
    "executive summary (2-4 sentences) and a list of concrete action items "
    "extracted from it. Call the summarize tool with your result."
)
SUMMARIZE_TOOL = {
    "type": "function",
    "function": {
        "name": "summarize",
        "description": "Record the executive summary and action items.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"},
            "action_items": {"type": "array", "items": {"type": "string"}},
        }, "required": ["summary", "action_items"]},
    },
}
server = Server("summarizer-mcp")


def _run_llm(document_text: str) -> dict:
    import litellm
    resp = litellm.completion(
        model="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"],
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": document_text}],
        tools=[SUMMARIZE_TOOL], tool_choice="required", temperature=0.0,
    )
    return json.loads(resp.choices[0].message.tool_calls[0].function.arguments)


@server.list_tools()
async def list_tools():
    return [Tool(name="summarize_document", description="Produce an executive summary + action items for a document.",
                 inputSchema={"type": "object", "properties": {"document_text": {"type": "string"}}, "required": ["document_text"]})]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "summarize_document":
        raise ValueError(f"unknown tool {name!r}")
    return [TextContent(type="text", text=json.dumps(_run_llm(arguments.get("document_text", ""))))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name="summarizer-mcp", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
        ))


if __name__ == "__main__":
    asyncio.run(main())
PYEOF

cat > "src/$PKG/defs/summarizer_agent/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: summarizer_agent
  name: "Document Summarizer Agent"
  description: "Produces an executive summary and extracts action items from a document."
  skills:
    - id: summarize_document
      name: "Summarize document"
      description: "Given a document's text, returns a short executive summary + a list of action items."
      tags: [documents]
  capabilities: [summarize]
  invocation:
    mcp_server:
      name: summarizer-mcp
      type: stdio
      command: [python, "src/$PKG/defs/summarizer_agent/summarizer_agent_server.py"]
    tool_name: summarize_document
    tool_args_template: {document_text: "{extra.src_text}"}
EOF

echo ">>> Writing pipeline.yaml"
cat > "src/$PKG/defs/doc_summary/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: doc_summary
  group_name: doc_summary
  source:
    kind: file
    path: src/$PKG/defs/doc_summary/document.txt

  steps:
    - id: summarized
      op: delegate
      task: "Summarize this document into a short executive summary and extract key action items."
      required_capabilities: [summarize]
      picker:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY

  outputs:
    assets: [summarized]
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev                                     # open UI at http://localhost:3000

Or materialize headlessly (~\$0.0005):

  uv run dg launch --assets '*'

Then in the UI, click doc_summary_summarized → {summary, action_items}
forced-structured JSON, extracted from the real sample incident postmortem.
Edit src/$PKG/defs/doc_summary/document.txt to try your own document.
MSG
