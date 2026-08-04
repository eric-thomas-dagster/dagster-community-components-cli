# Dagster+ MCP — materialize Dagster+ operations as Dagster assets
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.


Wire `MCPToolCallComponent` at the **Dagster+ MCP server** (`https://mcp.agent.dagster.cloud/mcp/`). Every tool exposed by your org's MCP endpoint becomes a Dagster asset — recent runs, asset lineage, deployment health, run insights — all catalogued alongside your normal pipelines.

**Setup:**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_mcp_dagster_plus_demo.sh \
  -o setup_mcp_dagster_plus_demo.sh
bash setup_mcp_dagster_plus_demo.sh
```

Requirements: [uv](https://docs.astral.sh/uv/) + a Dagster+ user token. Cost: $0 (included in your Dagster+ subscription).

## What gets validated

| Component | Role |
|---|---|
| `MCPToolCallComponent` | Deterministic single-shot MCP call — no LLM, just Dagster → MCP tool → asset |

`dg check defs` validates the YAML shape locally. End-to-end materialization requires the two env vars below.

## The two headers Dagster+ MCP requires

| HTTP Header | Value |
|---|---|
| `Authorization` | `Bearer <your-user-token>` — get one from Dagster+ **Admin → tokens** |
| `Dagster-Cloud-Organization` | Your org slug (the `<org>` in `https://<org>.dagster.cloud`) |

Because `Authorization` has to include the literal `Bearer ` prefix, the setup script uses `DAGSTER_CLOUD_TOKEN_HEADER` for the full header value:

```bash
export DAGSTER_CLOUD_TOKEN_HEADER="Bearer sk_live_..."
export DAGSTER_CLOUD_ORG="your-org-slug"
```

## The YAML — one asset per MCP tool call

```yaml
type: dagster_community_components.MCPToolCallComponent
attributes:
  asset_name: dagster_plus_recent_runs
  server:
    name: dagster_plus
    type: http
    url: "https://mcp.agent.dagster.cloud/mcp/"
    headers_env:
      Authorization: DAGSTER_CLOUD_TOKEN_HEADER
      Dagster-Cloud-Organization: DAGSTER_CLOUD_ORG
  tool_name: list_runs      # swap for a tool your org's MCP exposes
  tool_args:
    limit: 25
  parse_as: auto
```

Each `MCPToolCallComponent` = one Dagster asset = one MCP call. Add more of them (one per YAML file) for each Dagster+ operation you want catalogued.

## Discovering what tools your MCP server exposes

Dagster+ MCP's tool surface evolves. Before hardcoding `tool_name` values, inspect what's actually available:

```bash
uv run python -c "
import asyncio, os
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
async def main():
    headers = {
        'Authorization': os.environ['DAGSTER_CLOUD_TOKEN_HEADER'],
        'Dagster-Cloud-Organization': os.environ['DAGSTER_CLOUD_ORG'],
    }
    async with streamablehttp_client('https://mcp.agent.dagster.cloud/mcp/', headers=headers) as (r, w, _):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = await sess.list_tools()
            for t in tools.tools:
                print(f'  {t.name:30}  {t.description}')
asyncio.run(main())
"
```

Any name that shows up is a valid `tool_name`. The `inputSchema` on each tool tells you what `tool_args` to pass.

## Why this pattern (vs a Python script hitting the same endpoint)

- **First-class asset.** MCP responses land in the Dagster catalog with lineage, materialization history, freshness policies, asset checks, and column-lineage — same as any DataFrame asset.
- **Composable.** Pair with `AutomationConditionApplicatorComponent` to auto-refresh; pair with `dataframe_to_csv` / warehouse sinks to persist the responses; pair with the AI components (`OpenAIAgentComponent`, `MCPToolPickerComponent`) to LLM-analyze what the MCP returned.
- **Auditable.** Every call's response is a materialization event with metadata. If Dagster+ MCP starts returning different shapes, the asset's column-schema check catches it.

## Common Dagster+ MCP use cases

- **Auto-catalog runs**: materialize `list_runs` daily → warehouse sink → BI dashboard tracking pipeline SLAs
- **Cross-org observability**: run this from Dagster OSS *outside* your Dagster+ deployment to pull operational data into a lakehouse
- **Incident retro**: query historical runs via MCP + LangGraph/OpenAI agent to write natural-language postmortems
- **Deployment health**: fetch active code-location status + materialize as an external asset with a freshness policy → alert when stale

## Teardown

Nothing to clean up — no containers, no local state beyond the scaffolded project directory.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_mcp_dagster_plus_demo.sh \
  -o setup_mcp_dagster_plus_demo.sh
bash setup_mcp_dagster_plus_demo.sh
```

## See also

- [`mcp_stripe.md`](mcp_stripe.md) — same primitive against Stripe's MCP server (swap URL + one header)
- [`mcp_tool_picker.md`](mcp_tool_picker.md) — LLM picks *which* MCP tools to call (bounded action space)
- [`agent_family.md`](agent_family.md) — pair MCP tool calls with an LLM agent + judge
