#!/usr/bin/env bash
# pr_review_bot — fetches a REAL pull request's diff from GitHub's public
# API (no token needed, public repo), then three reviewer-persona agents
# (security / style / test-coverage) each review it from their own lens,
# synthesized into one polished review comment.
#
# Shows off:
#   - mcp_call: a direct, deterministic (no LLM) MCP tool call -- the
#     first showcase of this op in this examples set. Real GitHub API
#     call, not simulated.
#   - delegate with required_capabilities forcing a SPECIFIC reviewer
#     each time (3 separate calls, same registry, each narrowed to
#     exactly 1 agent by capability) -- a different flavor than semantic
#     picking (see route_to_specialist.md): here, the step already KNOWS
#     which lens it wants, so forcing by tag is the right tool.
#   - synthesize: fan-in three independent reviews into one comment.
#
# The default PR (dagster-io/dagster#31999, "Drop last distutils usages
# in favor of shutil.which") is real, small (3 files, 25 lines), and
# genuinely benign -- a good test that the reviewers don't invent issues
# where none exist, while still catching the real, legitimate
# test-coverage gap (no new tests for the behavior change).
#
# Total cost: ~$0.002/run (gpt-4o-mini, ~6 LLM calls: 3 picks + 3 reviews
# + 1 synthesize).

set -eo pipefail

PROJECT_DIR="${1:-pr-review-bot-demo}"

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

echo ">>> Writing the GitHub PR-fetch MCP server (real, unauthenticated public API)"
mkdir -p "src/$PKG/defs/pr_review"
cat > "src/$PKG/defs/pr_review/github_pr_server.py" <<'PYEOF'
#!/usr/bin/env python3
"""Minimal local MCP stdio server exposing one REAL tool: fetch_pr_diff.
Hits GitHub's real, public, unauthenticated REST API -- no token needed
for public repos."""
import asyncio, json
import requests
import mcp.server.stdio
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

server = Server("github-pr-mcp")


def _fetch_pr_diff(owner: str, repo: str, pr_number: int) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    resp = requests.get(url, params={"per_page": 100}, timeout=15, headers={"Accept": "application/vnd.github+json"})
    resp.raise_for_status()
    files = resp.json()
    patches = []
    for f in files:
        patch = f.get("patch", "(binary or too large to diff)")
        patches.append(f"FILE: {f['filename']} (+{f['additions']}/-{f['deletions']})\n{patch}")
    return {"pr": f"{owner}/{repo}#{pr_number}", "file_count": len(files), "diff": "\n\n".join(patches)}


@server.list_tools()
async def list_tools():
    return [Tool(
        name="fetch_pr_diff",
        description="Fetch a real pull request's file-level diffs from GitHub's public API.",
        inputSchema={"type": "object", "properties": {
            "owner": {"type": "string"}, "repo": {"type": "string"}, "pr_number": {"type": "integer"},
        }, "required": ["owner", "repo", "pr_number"]},
    )]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "fetch_pr_diff":
        raise ValueError(f"unknown tool {name!r}")
    result = _fetch_pr_diff(arguments["owner"], arguments["repo"], arguments["pr_number"])
    return [TextContent(type="text", text=json.dumps(result))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name="github-pr-mcp", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
        ))


if __name__ == "__main__":
    asyncio.run(main())
PYEOF

echo ">>> Writing the three reviewer agents (one script, three personas)"
mkdir -p "src/$PKG/defs/security_reviewer" "src/$PKG/defs/style_reviewer" "src/$PKG/defs/test_coverage_reviewer"

for dir_persona in "security_reviewer security" "style_reviewer style" "test_coverage_reviewer test_coverage"; do
  set -- $dir_persona
  dirname="$1"; persona="$2"
  cat > "src/$PKG/defs/${dirname}/reviewer_agent_server.py" <<'PYEOF'
#!/usr/bin/env python3
"""One script, three reviewer personas -- selected by argv[1]."""
import asyncio, json, os, sys
import mcp.server.stdio
from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.types import Tool, TextContent

PERSONAS = {
    "security": "You are a senior security reviewer. Given a pull request's file-level diffs, identify any security concerns: injected secrets, unsafe deserialization, missing input validation, path traversal, unsafe subprocess/shell usage, etc. If you find none, say so explicitly and briefly explain why the change is low-risk. 3-5 bullet points.",
    "style": "You are a code style reviewer. Given a pull request's file-level diffs, comment on naming, clarity, and consistency with typical Python conventions. 3-5 bullet points.",
    "test_coverage": "You are a test-coverage reviewer. Given a pull request's file-level diffs, assess whether the change is adequately tested and flag any risky untested paths. 3-5 bullet points.",
}
persona_name = sys.argv[1] if len(sys.argv) > 1 else "style"
SYSTEM_PROMPT = PERSONAS[persona_name]
server = Server(f"reviewer-{persona_name}-mcp")


def _run_llm(context_text: str) -> str:
    import litellm
    resp = litellm.completion(model="gpt-4o-mini", api_key=os.environ["OPENAI_API_KEY"],
                               messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": context_text}], temperature=0.1)
    return resp.choices[0].message.content


@server.list_tools()
async def list_tools():
    return [Tool(name="review_diff", description=f"Review a PR diff from a {persona_name} perspective.",
                 inputSchema={"type": "object", "properties": {"diff_context": {"type": "string"}}, "required": ["diff_context"]})]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name != "review_diff":
        raise ValueError(f"unknown tool {name!r}")
    response = _run_llm(arguments.get("diff_context", ""))
    return [TextContent(type="text", text=json.dumps({"review": response, "reviewer": persona_name}))]


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name=f"reviewer-{persona_name}-mcp", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
        ))


if __name__ == "__main__":
    asyncio.run(main())
PYEOF
  sed -i.bak "s/sys.argv\[1\] if len(sys.argv) > 1 else \"style\"/sys.argv[1] if len(sys.argv) > 1 else \"${persona}\"/" "src/$PKG/defs/${dirname}/reviewer_agent_server.py"
  rm -f "src/$PKG/defs/${dirname}/reviewer_agent_server.py.bak"
done

cat > "src/$PKG/defs/security_reviewer/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: security_reviewer
  name: "Security Reviewer"
  description: "Reviews PR diffs for security concerns."
  skills:
    - id: review_security
      name: "Review (security)"
      description: "Reviews PR diffs for security concerns."
      tags: [security]
  capabilities: [security]
  invocation:
    mcp_server:
      name: reviewer-security-mcp
      type: stdio
      command: [python, "src/$PKG/defs/security_reviewer/reviewer_agent_server.py", security]
    tool_name: review_diff
    tool_args_template: {diff_context: "{extra.src_text}"}
EOF

cat > "src/$PKG/defs/style_reviewer/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: style_reviewer
  name: "Style Reviewer"
  description: "Reviews PR diffs for code style and clarity."
  skills:
    - id: review_style
      name: "Review (style)"
      description: "Reviews PR diffs for code style and clarity."
      tags: [style]
  capabilities: [style]
  invocation:
    mcp_server:
      name: reviewer-style-mcp
      type: stdio
      command: [python, "src/$PKG/defs/style_reviewer/reviewer_agent_server.py", style]
    tool_name: review_diff
    tool_args_template: {diff_context: "{extra.src_text}"}
EOF

cat > "src/$PKG/defs/test_coverage_reviewer/defs.yaml" <<EOF
type: $PKG.components.agent_card.component.AgentCardComponent
attributes:
  agent_id: test_coverage_reviewer
  name: "Test Coverage Reviewer"
  description: "Reviews PR diffs for test coverage gaps."
  skills:
    - id: review_test_coverage
      name: "Review (test coverage)"
      description: "Reviews PR diffs for test coverage gaps."
      tags: [test_coverage]
  capabilities: [test_coverage]
  invocation:
    mcp_server:
      name: reviewer-test_coverage-mcp
      type: stdio
      command: [python, "src/$PKG/defs/test_coverage_reviewer/reviewer_agent_server.py", test_coverage]
    tool_name: review_diff
    tool_args_template: {diff_context: "{extra.src_text}"}
EOF

echo ">>> Writing pipeline.yaml (mcp_call -> 3x delegate -> synthesize)"
cat > "src/$PKG/defs/pr_review/defs.yaml" <<EOF
type: $PKG.components.agentic_pipeline.component.AgenticPipelineComponent
attributes:
  asset_name_prefix: pr_review
  group_name: pr_review
  source:
    kind: literal
    text: ""

  steps:
    - id: fetch_diff
      op: mcp_call
      source: source
      server:
        name: github-pr
        type: stdio
        command: [python, "src/$PKG/defs/pr_review/github_pr_server.py"]
      mcp_tool_name: fetch_pr_diff
      tool_args:
        owner: dagster-io
        repo: dagster
        pr_number: 31999
      parse_as: auto

    - id: security_review
      op: delegate
      source: fetch_diff
      task: "Review this pull request's diff from a security perspective."
      required_capabilities: [security]
      picker: {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY}

    - id: style_review
      op: delegate
      source: fetch_diff
      task: "Review this pull request's diff from a code style perspective."
      required_capabilities: [style]
      picker: {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY}

    - id: test_review
      op: delegate
      source: fetch_diff
      task: "Review this pull request's diff from a test-coverage perspective."
      required_capabilities: [test_coverage]
      picker: {model: gpt-4o-mini, api_key_env_var: OPENAI_API_KEY}

    - id: final_review
      op: synthesize
      sources: [security_review, style_review, test_review]
      model: gpt-4o-mini
      api_key_env_var: OPENAI_API_KEY
      system_prompt: "Combine these three specialist reviews (security, style, test coverage) into one polished PR review comment, with one section per lens."
      max_tokens: 600

  outputs:
    assets: [fetch_diff, security_review, style_review, test_review, final_review]
EOF

cat <<MSG

>>> Setup complete. Next:

  cd $PROJECT_DIR
  uv run dg dev                                     # open UI at http://localhost:3000

Or materialize headlessly (~\$0.002):

  uv run dg launch --assets '*'

Then in the UI:
  - Click pr_review_fetch_diff → the real diff fetched from GitHub's
    public API for dagster-io/dagster#31999 (no token needed).
  - Click pr_review_security_review / _style_review / _test_review →
    each picked its one matching registered reviewer (3 registered
    agents, each forced by required_capabilities -- no ambiguity).
  - Click pr_review_final_review → one polished review combining all
    three lenses.
  - Edit tool_args in src/$PKG/defs/pr_review/defs.yaml to point at a
    different PR (owner/repo/pr_number) -- any real, public GitHub PR
    works, no token required.
MSG
