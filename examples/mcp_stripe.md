# Stripe MCP — materialize Stripe operations as Dagster assets
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

Wire `MCPToolCallComponent` at Stripe's official MCP server (`https://mcp.stripe.com/`). Every tool Stripe MCP exposes — customers, charges, balances, payment links, subscriptions — becomes a first-class Dagster asset with lineage, materialization history, and freshness.

**Setup:**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_mcp_stripe_demo.sh \
  -o setup_mcp_stripe_demo.sh
bash setup_mcp_stripe_demo.sh
```

Requirements: [uv](https://docs.astral.sh/uv/) + a Stripe API key (a **restricted** key is strongly recommended). Cost: $0 for read-only tools; whatever Stripe charges for the operations you invoke.

## What gets validated

| Component | Role |
|---|---|
| `MCPToolCallComponent` | Deterministic single-shot MCP call — no LLM, just Dagster → Stripe MCP tool → asset |

`dg check defs` validates the YAML shape locally. End-to-end materialization requires the auth env var below.

## Auth — one header, restricted-key recommended

| HTTP Header | Value |
|---|---|
| `Authorization` | `Bearer <your-stripe-key>` — restricted key from [dashboard.stripe.com/apikeys](https://dashboard.stripe.com/apikeys) |

Because the header value includes the literal `Bearer ` prefix, the setup uses one env var for the whole thing:

```bash
export STRIPE_MCP_AUTHORIZATION_HEADER="Bearer rk_live_..."
```

**Use a restricted key**, not your `sk_live_...` secret key — grant only the scopes each demo needs (e.g. read-only on customers + charges). Restricted keys are per-scope revocable; if the key leaks, blast radius is bounded.

## The YAML — one asset per Stripe MCP tool

```yaml
type: dagster_community_components.MCPToolCallComponent
attributes:
  asset_name: stripe_recent_customers
  server:
    name: stripe
    type: http
    url: "https://mcp.stripe.com/"
    headers_env:
      Authorization: STRIPE_MCP_AUTHORIZATION_HEADER
  tool_name: list_customers      # swap for a tool Stripe MCP exposes
  tool_args:
    limit: 25
  parse_as: auto
```

Each `MCPToolCallComponent` = one Dagster asset = one Stripe MCP call. Add more of them (one per YAML file) for each Stripe operation you want catalogued.

## Discovering what tools Stripe MCP exposes

Stripe MCP's tool surface evolves. Before hardcoding `tool_name` values, inspect what's available:

```bash
uv run python -c "
import asyncio, os
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
async def main():
    headers = {'Authorization': os.environ['STRIPE_MCP_AUTHORIZATION_HEADER']}
    async with streamablehttp_client('https://mcp.stripe.com/', headers=headers) as (r, w, _):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            tools = await sess.list_tools()
            for t in tools.tools:
                print(f'  {t.name:30}  {t.description}')
asyncio.run(main())
"
```

Any name that shows up is a valid `tool_name`. The `inputSchema` tells you what `tool_args` to pass.

## Stripe MCP vs the existing Stripe components

We ship four dedicated Stripe components — `StripeResource`, `StripeIngestion`, `StripeEventSensor`, `StripeWorkspace` — that hit Stripe's REST API directly via the `stripe` Python SDK. Pick which pattern fits:

| You want… | Use |
|---|---|
| Read-only ingest of specific object types (customers, charges, invoices) on a schedule | **`StripeIngestion`** — purpose-built, paginates automatically, DataFrame output |
| Event-driven ingest triggered by Stripe webhooks | **`StripeEventSensor`** |
| Auto-catalog every Stripe object type as an external asset | **`StripeWorkspace`** |
| Any tool Stripe MCP exposes (including MCP-only capabilities like natural-language search + Stripe Sigma queries) — same UX as any other MCP integration | **`MCPToolCallComponent`** (this walkthrough) |

The MCP surface is often **broader** than what the vendor SDK exposes — Stripe MCP includes composite ops and natural-language surfaces the raw REST client doesn't. Use MCP for anything that doesn't map cleanly to a REST-shaped ingest.

## Common Stripe MCP use cases

- **Daily balance snapshots**: materialize `retrieve_balance` on a `daily` partition → warehouse sink → CFO dashboard
- **Payment link generation from prices catalog**: pair `create_payment_link` with an upstream `dataframe_to_stripe`-style asset that reads your product prices from Postgres
- **Failed-charge triage**: `list_charges` filtered on `status=failed` → feed into a `DataQualityAgentComponent` for LLM-generated per-customer remediation notes
- **Subscription cohort analysis**: `list_subscriptions` → `filter` → `summarize` → BI board

## Teardown

Nothing to clean up — no containers, no local state beyond the scaffolded project directory.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_mcp_stripe_demo.sh \
  -o setup_mcp_stripe_demo.sh
bash setup_mcp_stripe_demo.sh
```

## See also

- [`mcp_dagster_plus.md`](mcp_dagster_plus.md) — sibling walkthrough for Dagster+'s own MCP server (same primitive, two headers instead of one)
- [`mcp_tool_picker.md`](mcp_tool_picker.md) — LLM picks *which* Stripe MCP tools to call (bounded action space, agent shape)
- Component registry: `StripeResource`, `StripeIngestion`, `StripeEventSensor`, `StripeWorkspace` — dedicated Stripe components for REST-shaped workloads
