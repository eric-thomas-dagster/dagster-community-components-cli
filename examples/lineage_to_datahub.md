# Lineage → DataHub demo
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

Surface your Dagster asset graph (nodes + edges) in **DataHub OSS** running
locally in Docker. End-to-end live-validated: the demo pushes 8 datasets
to DataHub and confirms upstream/downstream lineage edges via DataHub's
GraphQL API.

```
@dg.asset    raw_orders     raw_customers
                  │              │
                  ▼              ▼
@dg.asset    orders_clean → customer_360
                  │              │
                  ▼              ▼
@dg.asset    daily_revenue   top_customers   (the asset graph being tracked)
                       │
                       ▼
                lineage_graph_extractor (canonical {nodes, edges, hash})
                       │
                       ▼
                lineage_to_datahub
                       │
                       ▼ (HTTP POST /aspects?action=ingestProposal)
                  DataHub GMS
```

## Components used

| Component | Role |
|---|---|
| `lineage_graph_extractor` | Walks the live Dagster asset graph at materialization time, emits canonical `{nodes, edges, payload_hash}` |
| `lineage_to_datahub` | Pushes one `datasetProperties` + one `upstreamLineage` aspect per Dagster asset to DataHub's Rest.li `ingestProposal` endpoint |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_lineage_to_datahub_demo.sh | bash
```

The script will:

1. Fetch DataHub's published quickstart compose file (`v1.3.0`)
2. Bring up the storage layer (MySQL, OpenSearch, Kafka)
3. Pre-load DataHub's `init.sql` into MySQL **(workaround for a v1.3.0 quickstart bug)**
4. Bring up the rest (system-update, gms, frontend, actions)
5. Mint a personal-access-token via `getAccessToken` GraphQL query
6. Scaffold a Dagster project + install `lineage_graph_extractor` + `lineage_to_datahub` via `dagster-component add`
7. Drop in a 6-asset ETL-shape graph (raw → transform → mart)
8. Materialize everything (`dg launch --assets '*'`)
9. Query DataHub's GraphQL `search` to confirm all 8 datasets landed
10. Print the lineage edges to look for in the UI

First run takes 5–10 min: pulls ~3.5 GB across 8 DataHub containers.

## Validated end-to-end

| Step | Result |
|---|---|
| `lineage_graph` materializes | Emitted canonical payload with 8 nodes, 7 edges |
| `lineage_to_datahub` materializes | `DataHub: ingested 13 aspect proposals` |
| DataHub GraphQL `search` | `Total assets on platform=dagster: 8` |
| Lineage edges for `orders_clean` | Upstream: `raw_orders` (1). Downstream: `customer_360`, `daily_revenue` (2) |

## Why a separate component, not the official `dagster-openlineage`?

`dagster-openlineage` (official Dagster integration) emits **run events** —
START/COMPLETE per asset, per run, in real-time, intended for systems that
care about job execution telemetry (Marquez, Astro Observe).

`lineage_to_datahub` does something different: at materialization time it
extracts the **structural lineage graph** as a snapshot and pushes the
dataset + upstream-lineage aspects. The target is catalog sync, not run
telemetry — DataHub gets a clean point-in-time view of "here's what
Dagster knows about, here are the dependencies."

The two are complementary and not redundant.

## Verify in the DataHub UI

```
http://localhost:9002   (login: datahub / datahub)
```

- **Browse → Datasets → dagster** — see all 8 ingested assets grouped by Dagster group
- Click `orders_clean` → **Lineage** tab → see `raw_orders` upstream and `customer_360`, `daily_revenue` downstream

## Quickstart-profile gotcha (DataHub v1.3.0)

The published `docker-compose.quickstart-profile.yml` sets
`DATAHUB_SQL_SETUP_ENABLED=true` on the `system-update` job — but
v1.3.0's system-update process tries to **read** `metadata_aspect_v2`
before it creates the table, throwing
`Table 'datahub.metadata_aspect_v2' doesn't exist` and never completing.
GMS then refuses to start because `service_completed_successfully`
never fires.

The demo script works around this by pre-loading
`https://raw.githubusercontent.com/datahub-project/datahub/v1.3.0/docker/mysql-setup/init.sql`
into MySQL *before* bringing up the rest of the stack. Newer DataHub
releases may fix this; the script is parameterized on `DATAHUB_VERSION`
if you want to try a different one.

## Teardown

```bash
docker compose -f /tmp/datahub-quickstart-compose.yml --profile quickstart down -v
rm -rf lineage-to-datahub-demo
```

## See also

<!-- TODO: link related walkthroughs -->
