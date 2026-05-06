# Catalog Lineage Sync demo

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
| `lineage_to_datahub` | sink | DataHub Rest.li ingestProposal |
| `lineage_to_openlineage` | sink | Marquez, Atlan, Astronomer Observe (OL spec) |
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

## Run the demo

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_lineage_demo.sh | bash
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

## Swap to Microsoft Purview

```yaml
# Replace lineage_to_file/defs.yaml:
type: dagster_component_templates.LineageToPurviewComponent
attributes:
  asset_name: lineage_in_purview
  upstream_asset_key: lineage_graph
  catalog_url: https://my-account.purview.azure.com
  api_token_env: PURVIEW_ACCESS_TOKEN
  only_push_on_change: true
```

Get a Purview access token:

```bash
export PURVIEW_ACCESS_TOKEN=$(az account get-access-token --resource https://purview.azure.net --query accessToken -o tsv)
```

The component POSTs to `/datamap/api/atlas/v2/entity/bulk` — assets land
as DataSet entities, lineage edges as Process entities (Purview renders
lineage from Process inputs/outputs).

## Swap to DataHub

```yaml
type: dagster_component_templates.LineageToDataHubComponent
attributes:
  asset_name: lineage_in_datahub
  upstream_asset_key: lineage_graph
  catalog_url: https://datahub.example.com/api/gms
  api_token_env: DATAHUB_API_TOKEN
```

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
