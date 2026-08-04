# Catalog Lineage Sync demo
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

Export the Dagster asset lineage graph to your data catalog, with
**lock-step fan-out across multiple catalogs** and **only-push-on-change**
guarding via payload hashing. Validated locally end-to-end with the file
sink — swap to any of the 6 supported catalogs by changing one YAML file.

```
synthetic_data_generator → orders_raw ──┐
dataframe_to_csv          → orders_csv ─┘   (the asset graph being tracked)
                                   │
                                   ▼
                       lineage_graph_extractor (lineage_graph asset)
                                   │
                ┌──────────────────┼──────────────────┐
                ▼                  ▼                  ▼
       lineage_to_file    lineage_to_purview   lineage_to_datahub  ...
       (/tmp/*.json)        (Atlas v2 API)       (Rest.li API)
```

## Components used

| Component | Category | Role |
|---|---|---|
| `lineage_graph_extractor` | source | Builds canonical lineage payload (nodes + edges + source-system identity), hashes it for change detection |
| `lineage_to_file` | sink | Writes JSON locally — for demos / debugging / audit trails |
| `lineage_to_alation` | sink | Alation Data Catalog REST API |
| `lineage_to_collibra` | sink | Collibra Import API |
| `lineage_to_datahub` | sink | DataHub Rest.li ingestProposal (see [lineage_to_datahub.md](lineage_to_datahub.md) for an end-to-end Docker walkthrough) |
| `lineage_to_openmetadata` | sink | OpenMetadata REST (service → database → schema → table hierarchy + lineage edges) |
| `lineage_to_purview` | sink | Microsoft Purview Data Map (Apache Atlas v2 entity bulk) |
| `lineage_to_webhook` | sink | Generic POST to any HTTP endpoint |

## Why this shape?

Earlier this lived as 7 monolithic sensor components, each rebuilding the
graph and maintaining its own change-detection cursor. Per the
[`OCSF`](ocsf_security_lake.md) modular pattern, this is now a fan-out
asset pipeline:

- **One source of truth** — `lineage_graph_extractor` builds the graph
  once per tick and stamps the payload with a `payload_hash` of the
  structural content (nodes + edges)
- **Lock-step fan-out** — multiple catalog sinks see the same snapshot
- **Per-sink change skip** — each sink stores `pushed_hash` in its asset
  metadata; subsequent runs that match the upstream `payload_hash` skip
  the catalog POST entirely
- **Standard Dagster patterns** — auto-materialize, schedules, sensors
  all work for triggering the chain

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_lineage_catalogs_demo.sh | bash
cd lineage-demo

# Persistent DAGSTER_HOME is required for change-detection to span runs
# (dg launch's ephemeral default home throws away materialization metadata)
export DAGSTER_HOME=/tmp/lineage_dagster_home
mkdir -p $DAGSTER_HOME
uv run dg launch --assets '*'
```

## Validated end-to-end

| Step | Result |
|---|---|
| Run 1: `lineage_graph_extractor` materializes | 4 nodes, 2 edges, hash=`ccd8b7c9db883f5e` |
| Run 1: `lineage_to_file` materializes | Wrote `/tmp/dagster_lineage.json` (2,476 bytes) |
| Run 2 (graph unchanged) | `Lineage unchanged (hash=ccd8b7c9), skipping push to file. Graph: 4 nodes, 2 edges.` |

## JSON payload shape (file sink)

```json
{
  "sync_metadata": {
    "synced_at": "2026-05-06T17:41:37Z",
    "source": "dagster_asset_graph",
    "total_nodes": 4,
    "total_edges": 2,
    "payload_hash": "ccd8b7c9db883f5e"
  },
  "nodes": [
    {"asset_key_string": "orders_raw", "group": "bronze", "kinds": ["python"], ...},
    {"asset_key_string": "orders_csv", "group": "silver", "kinds": ["file"], ...},
    {"asset_key_string": "lineage_graph", "group": "lineage", ...}
  ],
  "edges": [
    {"upstream": "orders_raw", "downstream": "orders_csv"},
    {"upstream": "lineage_graph", "downstream": "lineage_in_local_json"}
  ],
  "source_system": {
    "platform": "dagster",
    "deployment": "local",
    "organization": "DemoCorp"
  }
}
```

Each catalog-specific sink transforms this canonical payload to the
catalog's expected format — Atlas entities for Purview, ingestProposals
for DataHub, dataflow_objects for Alation, etc.

## Per-catalog examples

Each example shows the YAML, the auth flow, the catalog API endpoint
called, and what shows up in the catalog UI after a successful push.

### Microsoft Purview

```yaml
# defs/lineage_to_purview/defs.yaml
type: dagster_component_templates.LineageToPurviewComponent
attributes:
  asset_name: lineage_in_purview
  upstream_asset_key: lineage_graph
  catalog_url: https://my-account.purview.azure.com
  api_token_env: PURVIEW_ACCESS_TOKEN
  only_push_on_change: true
```

**Auth** (Microsoft Entra ID OAuth):

```bash
# Local development
export PURVIEW_ACCESS_TOKEN=$(az account get-access-token --resource https://purview.azure.net --query accessToken -o tsv)

# Production: bake into a sidecar that refreshes the token before each run,
# or use a Dagster resource to get the token via DefaultAzureCredential
```

**API:** `POST /datamap/api/atlas/v2/entity/bulk`

**What lands:** Each Dagster asset → `DataSet` Atlas entity. Each lineage
edge → `Process` entity with input + output. Purview renders the lineage
graph by following Process entities. Assets show up under the
`dagster://<deployment>/<key>` qualifiedName prefix.

### DataHub

```yaml
# defs/lineage_to_datahub/defs.yaml
type: dagster_component_templates.LineageToDataHubComponent
attributes:
  asset_name: lineage_in_datahub
  upstream_asset_key: lineage_graph
  catalog_url: https://datahub.example.com/api/gms   # GMS = the metadata service
  api_token_env: DATAHUB_API_TOKEN
  only_push_on_change: true
```

**Auth:** Personal Access Token from DataHub Settings → Access Tokens.
Set `DATAHUB_API_TOKEN=<token>`.

**API:** `POST /aspects?action=ingestProposal` (Rest.li v2 protocol).
Each asset emits two proposals: `datasetProperties` aspect (description,
custom properties) + `upstreamLineage` aspect (parent dataset URNs).

**What lands:** Datasets under
`urn:li:dataset:(urn:li:dataPlatform:dagster,<key>,PROD)`. Lineage shows
in DataHub's Lineage tab.

### Alation

```yaml
# defs/lineage_to_alation/defs.yaml
type: dagster_component_templates.LineageToAlationComponent
attributes:
  asset_name: lineage_in_alation
  upstream_asset_key: lineage_graph
  catalog_url: https://alation.example.com
  api_token_env: ALATION_API_TOKEN
```

**Auth:** Alation API token. Header is `TOKEN: <value>` (NOT `Bearer`).
Generate one in Alation: User Settings → Authentication.

**API:** `POST /integration/v2/lineage/`. Each asset becomes a
`dataflow` external object; each lineage edge becomes a path connecting
upstream/downstream segments. Alation processes asynchronously and
returns a job ID.

**What lands:** External objects browseable under your Dagster
deployment's external_id prefix. Lineage shows in the Lineage tab on
each object.

### Collibra

```yaml
# defs/lineage_to_collibra/defs.yaml
type: dagster_component_templates.LineageToCollibraComponent
attributes:
  asset_name: lineage_in_collibra
  upstream_asset_key: lineage_graph
  catalog_url: https://example.collibra.com
  api_token_env: COLLIBRA_API_TOKEN
```

**Auth:** Collibra Bearer token via Authorization header.

**API:** `POST /rest/2.0/import/json-job`. Each asset → `Asset` object
in a community/domain hierarchy (organization → "<org> Data Platform"
community → asset's group as the domain). Lineage edges → `Data Flow`
relations.

**What lands:** Assets in the configured community. Lineage relations
viewable via Collibra's Lineage Explorer.

### OpenMetadata

```yaml
# defs/lineage_to_openmetadata/defs.yaml
type: dagster_component_templates.LineageToOpenMetadataComponent
attributes:
  asset_name: lineage_in_openmetadata
  upstream_asset_key: lineage_graph
  catalog_url: https://openmetadata.example.com
  api_token_env: OPENMETADATA_API_TOKEN
  service_name: dagster
  database_name: default
```

**Auth:** `Authorization: Bearer <JWT>`. OpenMetadata bot/user tokens
work; mint via the Settings → Bots section in the UI.

**API:** Hierarchy upserts (`PUT /api/v1/services/databaseServices`,
`/api/v1/databases`, `/api/v1/databaseSchemas`, `/api/v1/tables`) then
lineage edges via `PUT /api/v1/lineage`.

**What lands:** Service → Database → Schema (one per Dagster group) →
Tables (one per Dagster asset). Each lineage edge maps a Dagster
upstream → downstream into an OpenMetadata lineage relation. Columns
land on the table when asset metadata exposes them.

### Generic HTTP webhook

```yaml
# defs/lineage_to_webhook/defs.yaml
type: dagster_component_templates.LineageToWebhookComponent
attributes:
  asset_name: lineage_in_n8n
  upstream_asset_key: lineage_graph
  catalog_url: https://n8n.example.com/webhook/lineage-update
  api_token_env: LINEAGE_WEBHOOK_TOKEN     # optional — Bearer if set
```

**Auth:** Optional `Bearer <token>` if `api_token_env` is set.

**API:** `POST <catalog_url>` with the raw canonical payload (no
catalog-specific transform). Use this for n8n / Zapier / Slack /
internal webhook endpoints that want the full lineage graph as JSON.

**What lands:** Whatever your webhook endpoint does with the payload.
Common pattern: post lineage diff to a Slack channel.

### Local JSON file (for demos / audit / debug)

```yaml
# defs/lineage_to_file/defs.yaml
type: dagster_component_templates.LineageToFileComponent
attributes:
  asset_name: lineage_audit_trail
  upstream_asset_key: lineage_graph
  catalog_url: data/lineage-history/lineage.json
  api_token_env: ""
```

**Auth:** None.

**Output:** Writes the canonical payload to the local path on every push
(skipped if `only_push_on_change: true` and the hash matches). Good for:
- Demo / debugging (run the demo with this sink, inspect the JSON before
  pointing at a real catalog)
- Compliance / audit logs (point at a mounted volume that's archived
  daily)
- Pre-production validation (payload diff in CI before catalog rollout)

## Fan-out to multiple catalogs

Add multiple sink components — they all consume the same upstream
`lineage_graph` asset:

```yaml
# All in the same defs/ tree:

# defs/lineage_to_purview/defs.yaml
type: dagster_component_templates.LineageToPurviewComponent
attributes:
  asset_name: lineage_in_purview
  upstream_asset_key: lineage_graph
  catalog_url: https://acct.purview.azure.com
  api_token_env: PURVIEW_ACCESS_TOKEN

# defs/lineage_to_datahub/defs.yaml
type: dagster_component_templates.LineageToDataHubComponent
attributes:
  asset_name: lineage_in_datahub
  upstream_asset_key: lineage_graph
  catalog_url: https://datahub.example.com/api/gms
  api_token_env: DATAHUB_API_TOKEN

# defs/lineage_to_file/defs.yaml — also keep file sink for audit log
type: dagster_component_templates.LineageToFileComponent
attributes:
  asset_name: lineage_audit_trail
  upstream_asset_key: lineage_graph
  catalog_url: data/lineage-history/{run_id}.json
```

When `lineage_graph` materializes, all three sinks fan out automatically.
Each independently decides whether to skip based on its last-pushed hash.

## Trigger cadence

Three common patterns:

1. **Auto-materialize on upstream change** (recommended): set an
   AutomationCondition on `lineage_graph` so it materializes whenever
   any other asset in the workspace changes.
2. **Schedule** (simple): set up a cron schedule for the asset job
   covering `lineage_graph` + sinks.
3. **Sensor** (manual change-detection): you can build a custom sensor
   that calls `lineage_core.build_lineage_payload(...)` + hashes it,
   only triggering materialization when the hash changes.

## Variations

- **Deployment-wide scope**: set `scope: deployment` on
  `lineage_graph_extractor` to query Dagster+ GraphQL for the full
  cross-code-location graph (requires `DAGSTER_PLUS_TOKEN`)
- **Asset-specific filtering**: write a custom downstream asset that
  takes the `lineage_graph` dict and filters to a subset before sinking
- **Rich source-system identity**: fill in `organization`, `dagster_ui_url`,
  `deployment_name` so the catalog renders direct deeplinks back to the
  Dagster UI from each asset entity

## See also

<!-- TODO: link related walkthroughs -->
