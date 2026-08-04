# CRM Reconciliation — HubSpot + Salesforce Unified Customer View
> ✅ **Dagster+ Serverless:** deploys as-is via `dagster-cloud serverless deploy-docker`.

**Components:**
- `SyntheticDataGeneratorComponent` (`assets/ai/synthetic_data_generator`) — synth data for the local demo
- `HubSpotIngestionComponent` (`assets/ingestion/hubspot_ingestion`) — production drop-in
- `SalesforceIngestionComponent` (`assets/ingestion/salesforce_ingestion`) — production drop-in
- `DataframeJoin` (`assets/transforms/dataframe_join`) — the reconciliation step

**Script:** [`setup_crm_reconciliation_demo.sh`](./setup_crm_reconciliation_demo.sh)
**Cost:** **$0** — synthetic data + local pandas. Zero API calls.
**Validated:** 2026-07-06 — RUN_SUCCESS end-to-end.

## Why this exists

Every RevOps team with both HubSpot AND Salesforce eventually asks the same question: *"How do we reconcile them?"* Companies land marketing leads in HubSpot, promote them to sales in Salesforce, and get:
- Contacts that live in HubSpot only (top-of-funnel)
- Contacts that live in Salesforce only (sales-owned, not yet synced back)
- Contacts in both (candidates for enrichment / dedup / golden-record work)

This demo scaffolds the shape end-to-end with synthetic data (runs live with no auth) and swaps to production HubSpot + Salesforce with a two-line change to each defs.yaml.

```
hubspot_contacts (dagster asset)     salesforce_contacts (dagster asset)
    ↓                                      ↓
    └──────────────┬───────────────────────┘
                   ↓
         DataframeJoin (outer, on: email)
                   ↓
         unified_customer_view
           ↓         ↓         ↓
        HS-only    both     SF-only
```

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`

No API keys, no cloud, no accounts. The demo runs on synthetic data.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_crm_reconciliation_demo.sh -o setup_crm_reconciliation_demo.sh
chmod +x setup_crm_reconciliation_demo.sh
./setup_crm_reconciliation_demo.sh
```

## The three defs.yaml files

**HubSpot side** (demo uses synth; swap for real in prod):

```yaml
type: dagster_community_components.SyntheticDataGeneratorComponent   # ← swap for HubSpotIngestionComponent in prod
attributes:
  asset_name: hubspot_contacts
  schema_type: customers
  row_count: 200
  random_state: 100
```

**Production swap** — same asset_key, different type:

```yaml
type: dagster_community_components.HubSpotIngestionComponent
attributes:
  asset_name: hubspot_contacts
  api_key: "{{ env('HUBSPOT_API_KEY') }}"
  resources: [contacts]
```

**Salesforce side** — mirror shape.

**The reconciliation** — this defs.yaml doesn't change between demo + prod:

```yaml
type: dagster_community_components.DataframeJoin
attributes:
  asset_name: unified_customer_view
  left_asset_key: hubspot_contacts
  right_asset_key: salesforce_contacts
  how: outer                              # ← every row from either side
  "on":                                   # ← quoted; YAML 1.1 makes `on:` a boolean
    - email
  suffixes: [_hubspot, _salesforce]
```

## Reading the unified table

After materialization, `unified_customer_view` has columns from both sides with `_hubspot` / `_salesforce` suffixes on collisions. Downstream logic:

```sql
-- HubSpot-only contacts
SELECT * FROM unified_customer_view WHERE customer_id_salesforce IS NULL;

-- Salesforce-only contacts
SELECT * FROM unified_customer_view WHERE customer_id_hubspot IS NULL;

-- Cross-system matches (dedup / enrichment candidates)
SELECT * FROM unified_customer_view
WHERE customer_id_hubspot IS NOT NULL AND customer_id_salesforce IS NOT NULL;
```

## Extension patterns

- **Fuzzy match on name.** Add a `precision_match` or `text_similarity` component before the join to catch contacts with typo'd emails but matching first_name + last_name.
- **LLM enrichment.** Pipe unified rows through `langchain_chain_asset` — for each cross-system match with conflicting company descriptions, ask an LLM to reconcile into one canonical string.
- **Reverse-ETL back.** Add a downstream sink (`hubspot_sync` or a custom `dataframe_to_salesforce`) to push the golden record back to whichever system loses the merge.
- **Ownership metadata.** Add columns like `source_of_truth = 'salesforce'` per row based on business rules (e.g., "if a contact is a Salesforce Opportunity, SF wins the address field").

## See also

- [dbt + ML + dbt (mid-DAG Python)](./dbt_ml_pipeline.md) — same pattern (Python between structured layers), different problem shape.
- `hubspot_ingestion` + `salesforce_ingestion` in the [components UI](https://dagster-component-ui.vercel.app/) — both use `dlt` under the hood for schema-aware incremental sync.
