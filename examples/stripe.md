# Stripe — DataFrame → Stripe Customers Upsert

**Components:**
- `StripeResourceComponent` (`resources/stripe_resource`)
- `StripeCustomerUpsertComponent` (`assets/sinks/stripe_customer_upsert`)
- `InlineDataframeComponent` (`assets/sources/inline_dataframe`)

**Script:** [`setup_stripe_demo.sh`](./setup_stripe_demo.sh)
**Cost:** $0 (Stripe test mode — customers/products/payment intents all free)
**Duration:** ~40 seconds from cold to green (includes 15s wait for Search API indexing)
**Validated:** *pending live run*

> ✅ **Dagster+ Serverless:** deploys as-is (Stripe REST is HTTP-based)

## What it demonstrates

Same resource + sink pattern as [Notion](./notion.md), [GitHub](./github.md), [Jira](./jira.md), and [PagerDuty](./pagerduty.md), applied to Stripe: mirror a DataFrame of customers into your Stripe account with idempotent dedup via `metadata.dagster_key`.

Stripe is architecturally different from the others in one important way: **it doesn't have a native upsert endpoint** and **email is not unique** on customers (Stripe explicitly allows multiple customers per email). So we can't use HubSpot's `batch/upsert` trick or GitHub's HTML-comment marker in an issue body.

Instead we use Stripe's own recommended pattern:
1. Stamp a `metadata.dagster_key = <value>` field on every managed customer.
2. Before creating, search customers by that metadata field via Stripe's Search API.
3. Match → update. Miss → create (with `Idempotency-Key` set to a hash of the row key so within-run retries are safe).

## Pipeline

```
┌────────────────────────┐
│  customers_seed        │  InlineDataframeComponent
│  (5 rows in defs.yaml) │
└──────────┬─────────────┘
           │
           ▼
┌────────────────────────────────┐         ┌───────────────────────────────────┐
│  stripe_customers_mirror       │ ──────▶ │  Stripe customers                 │
│  StripeCustomerUpsertComponent │         │  (5 customers, keyed by           │
│                                │         │   metadata.dagster_key = CUST-*)  │
└────────────────────────────────┘         └───────────────────────────────────┘
```

## The sink component

```yaml
type: dagster_community_components.StripeCustomerUpsertComponent
attributes:
  asset_name: stripe_customers_mirror
  upstream_asset_key: customers_seed
  resource_key: stripe
  key_column: customer_id          # → metadata.dagster_key
  email_column: email
  name_column: full_name
  description_column: notes
  extra_metadata_columns: [plan_tier]  # more columns → metadata.<col>
```

Every customer this sink writes gets:
- The `dagster_key` metadata field with the row's `key_column` value (used to find them again).
- Whatever columns are named in `extra_metadata_columns` copied verbatim into the `metadata` dict — so `plan_tier: "enterprise"` in the DataFrame becomes `metadata.plan_tier: "enterprise"` on the Stripe customer.
- An `Idempotency-Key` header derived from a SHA-256 hash of the row key — Stripe returns the original result if the same key is retried.

## `StripeResource` convenience methods (~30 total)

**Reads:** `get_customer`, `list_customers` / `iter_customers`, `search_customers`, `list_charges` / `iter_charges`, `list_payment_intents` / `iter_payment_intents`, `list_invoices`, `list_subscriptions`, `list_products`, `list_prices`, `list_events`, `whoami`.

**Writes** (all accept `idempotency_key` for retry-safe writes):
`create_customer`, `update_customer`, `delete_customer`, `create_payment_intent`, `create_refund`, `create_invoice_item`, `create_invoice`, `create_product`, `create_price`.

**Escape hatch:** `get_client()` returns an authenticated `requests.Session` pointed at `api.stripe.com/v1`.

**Under the hood:** Stripe uses form-encoded requests (not JSON) and cursor-based pagination via `starting_after`. Both are handled internally — you pass Python dicts and get Python dicts back. Nested dicts (like `metadata`) are automatically flattened to Stripe's `metadata[key]=val` bracket notation.

## Requirements

- **`STRIPE_API_KEY`** — a Stripe secret key. **Use `sk_test_...` for the demo** — every action (create customer, delete customer, refund) hits Stripe's test mode, no real money moves. Get one from the Stripe Dashboard → **Developers → API Keys**.
- `uv` / `uvx`, Python 3.12+.

## Running

```bash
export STRIPE_API_KEY=sk_test_XXXXXXXXXXXXXXXXXXXX
./setup_stripe_demo.sh
```

The script auto-detects if you're using a live key (`sk_live_...`) and warns before continuing.

## Auth notes

Stripe has one auth mechanism for the REST API: a secret key, sent as `Authorization: Bearer <key>`. There's no separate "read" vs "write" scope on classic secret keys — the key can do anything your Stripe account can. **Restricted Keys** (also managed under Developers → API Keys) let you scope down to specific resources; the resource works with both.

## Cleanup

The setup script's final step deletes every customer whose `metadata.dagster_key` starts with `CUST-`. In test mode this is safe (no real customers exist). In live mode you'd want to be more careful — Stripe soft-deletes customers (they become inaccessible but retained for reporting).
