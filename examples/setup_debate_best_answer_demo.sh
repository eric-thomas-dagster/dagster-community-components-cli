#!/usr/bin/env bash
# debate_best_answer — two debater agents (shared capability tag,
# distinguished by the picker's semantic reading of FOR/AGAINST task
# phrasing) + one arbitrator (distinct tag, zero ambiguity), joined with
# typed inputs: ports into a final verdict that genuinely weighs both
# arguments. A registered-agent alternative to the native `debate` op --
# the personas here are a SHARED, reusable registry other pipelines in the
# same project could delegate to as well.
#
# Total cost: ~$0.001/run (gpt-4o-mini, 4 LLM calls: 2 picks + 2 debaters +
# 1 pick + 1 arbitrator -- actually 3 picker calls + 3 agent calls).

set -eo pipefail

PROJECT_DIR="${1:-debate-best-answer-demo}"

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

echo ">>> Writing the debate agent server (one script, three personas)"
mkdir -p "src/$PKG/defs/debater_advocate" "src/$PKG/defs/debater_skeptic" "src/$PKG/defs/arbitrator"

for dir_persona in "debater_advocate advocate" "debater_skeptic skeptic" "arbitrator arbitrator"; do
  set -- $dir_persona
  dirname="$1"; persona="$2"
  cat > "src/$PKG/defs/${dirname}/debate_agent_server.py" <<'PYEOF'
#!/usr/bin/env python3
"""One script, three personas -- selected by argv[1]."""
import asyncio, json, os, sys
import mcp.server.stdio
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

PERSONAS = {
    "advocate": "You are a debate agent arguing IN FAVOR of a proposal. Given a proposal, construct the strongest, most persuasive case FOR it, in 3-5 sentences.",
    "skeptic": "You are a debate agent arguing AGAINST a proposal. Given a proposal, construct the strongest, most critical case AGAINST it -- risks, flaws, what could go wrong -- in 3-5 sentences.",
    "arbitrator": "You are an impartial judge. Given a proposal and two arguments (one for, one against), weigh them honestly and render a clear, reasoned final verdict in 3-5 sentences -- don't just summarize both sides, actually decide.",
}
persona_name = sys.argv[1] if len(sys.argv) > 1 else "arbitrator"
SYSTEM_PROMPT = PERSONAS[persona_name]
server = Server(f"debate-{persona_name}-mcp")


def _run_llm(context_text: str) -> str:
    import litellm
    resp = litellm.completion(model="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"],
                               messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": context_text}], temperature=0.3)
    return resp.choices[0].message.content


@server.list_tools()
async def list_tools():
    return [Tool(name="respond", description=f"Produce this agent's ({persona_name}) response given context.",
                 inputSchema={"type": "object", "properties": {"context": {"type": "string"}}, "required": ["context"]})]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "respond":
        raise ValueError(f"unknown tool {name!r}")
    response = _run_llm(arguments.get("context", ""))
    return [TextContent(type="text", text=json.dumps({"response": response, "persona": persona_name}))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name=f"debate-{persona_name}-mcp", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
        ))


if __name__ == "__main__":
    asyncio.run(main())
PYEOF
  sed -i.bak "s/sys.argv\[1\] if len(sys.argv) > 1 else \"arbitrator\"/sys.argv[1] if len(sys.argv) > 1 else \"${persona}\"/" "src/$PKG/defs/${dirname}/debate_agent_server.py"
  rm -f "src/$PKG/defs/${dirname}/debate_agent_server.py.bak"
done

cat > "src/$PKG/defs/debater_advocate/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: debater_advocate
  name: "Debater (Advocate)"
  description: "Argues IN FAVOR of proposals -- constructs the strongest persuasive case for a given idea."
  skills:
    - id: argue_for
      name: "Argue for"
      description: "Given a proposal, builds the strongest case in favor of it."
      tags: [debate]
  capabilities: [debate]
  invocation:
    mcp_server:
      name: debate-advocate-mcp
      type: stdio
      command: [python, "src/$PKG/defs/debater_advocate/debate_agent_server.py", advocate]
    tool_name: respond
    tool_args_template: {context: "Task: {prompt}\n\nProposal:\n{extra.src_text}"}
EOF

cat > "src/$PKG/defs/debater_skeptic/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: debater_skeptic
  name: "Debater (Skeptic)"
  description: "Argues AGAINST proposals -- constructs the strongest critical case highlighting risks and flaws."
  skills:
    - id: argue_against
      name: "Argue against"
      description: "Given a proposal, builds the strongest case against it, surfacing risks and weaknesses."
      tags: [debate]
  capabilities: [debate]
  invocation:
    mcp_server:
      name: debate-skeptic-mcp
      type: stdio
      command: [python, "src/$PKG/defs/debater_skeptic/debate_agent_server.py", skeptic]
    tool_name: respond
    tool_args_template: {context: "Task: {prompt}\n\nProposal:\n{extra.src_text}"}
EOF

cat > "src/$PKG/defs/arbitrator/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: arbitrator
  name: "Arbitrator"
  description: "Given a proposal and two opposing arguments, weighs them and renders a final verdict."
  skills:
    - id: render_verdict
      name: "Render verdict"
      description: "Given a FOR argument and an AGAINST argument, decides the winner with reasoning."
      tags: [debate, arbitration]
  capabilities: [arbitrate]
  invocation:
    mcp_server:
      name: debate-arbitrator-mcp
      type: stdio
      command: [python, "src/$PKG/defs/arbitrator/debate_agent_server.py", arbitrator]
    tool_name: respond
    tool_args_template: {context: "{prompt}"}
EOF

echo ">>> Writing pipeline.yaml (3 delegate steps, inputs: ports join the last one)"
mkdir -p "src/$PKG/defs/debate"
cat > "src/$PKG/defs/debate/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: debate
  group_name: debate
  source:
    kind: literal
    text: "Proposal: our team should switch from a 2-week sprint cadence to continuous/weekly releases."

  steps:
    - id: for_argument
      op: delegate
      source: source
      task: "Argue FOR this proposal as persuasively as possible."
      required_capabilities: [debate]
      picker:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY

    - id: against_argument
      op: delegate
      source: source
      task: "Argue AGAINST this proposal, highlighting risks and weaknesses."
      required_capabilities: [debate]
      picker:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY

    - id: verdict
      op: delegate
      task: "Given the proposal and these two arguments, weigh them and render a final verdict.\n\nFOR:\n{for_arg}\n\nAGAINST:\n{against_arg}"
      inputs:
        for_arg: {from: for_argument}
        against_arg: {from: against_argument}
      required_capabilities: [arbitrate]
      picker:
        model: gpt-4o-mini
        api_key_env_var: OPENAI_API_KEY

  outputs:
    assets: [for_argument, against_argument, verdict]
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev                                     # open UI at http://localhost:3000

Or materialize headlessly (~\$0.001):

  uv run dg launch --assets '*'

Then in the UI:
  - Click debate_for_argument / debate_against_argument → picked_agent_id
    (advocate / skeptic respectively -- same capability tag, distinguished
    by the picker reading each step's FOR/AGAINST task phrasing).
  - Click debate_verdict → picked_agent_id=arbitrator (zero ambiguity, its
    own capability tag), and a verdict that genuinely weighs both
    arguments (joined via inputs: for_arg/against_arg ports).
  - Edit source.text in src/$PKG/defs/debate/defs.yaml to debate a
    different proposal.
MSG
