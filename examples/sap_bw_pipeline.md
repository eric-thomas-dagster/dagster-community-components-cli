# SAP BW (Business Warehouse) → Dagster

Extract data from **SAP BW / BW/4HANA** into Dagster via the canonical **Open Hub Destination** pattern. Still the production extraction path for the massive BW customer base.

## Why Open Hub

SAP BW is a planning + reporting data warehouse sitting on top of HANA (BW/4HANA) or any DB (older BW 7.x). For programmatic extraction, customers don't query BW's InfoProviders directly — they configure an **Open Hub Destination** that materializes the data into a flat table (DB) or file (filesystem). That table/file is then readable like any other source.

```
   ┌──────────────────────────────────────────────────┐
   │ BW / BW/4HANA — DataStore Objects, InfoCubes     │
   │   Customers run DTP (Data Transfer Process)      │
   │   to a configured Open Hub Destination           │
   └──────────────────────┬───────────────────────────┘
                          │ DTP writes to ↓
   ┌──────────────────────▼───────────────────────────┐
   │ Open Hub target (one of)                         │
   │   • DB table on the BW HANA (or DB) tenant       │
   │   • Flat file on a CIFS / SFTP share             │
   │   • Third-party DB via DB Connect                │
   └──────────────────────┬───────────────────────────┘
                          │
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ sap_hana_ingestion OR file_ingestion             │
   │   reads the Open Hub output → DataFrame          │
   └──────────────────────────────────────────────────┘
```

## Components used

| Component | Source | Role |
|---|---|---|
| `sap_hana_resource` | community | Register HANA connection (Approach A) |
| `sap_hana_ingestion` | community | Read Open Hub destination table via HANA SQL (Approach A) |
| `file_ingestion` | community | Read Open Hub file output — CSV / fixed-width (Approach B) |
| `odata_ingestion` | community | BW/4HANA OData services for direct query (Approach C) |
| `summarize`, `dataframe_to_*` | community | Downstream transforms + sinks |

## Approach A — Open Hub → DB table → `sap_hana_ingestion`

The cleanest pattern for BW-on-HANA. The Open Hub Destination writes to a HANA table (typically named `/BIC/OH<dest_id>`). Read it as a regular SQL table:

```yaml
# resources/sap_bw_hana.yaml
type: dagster_community_components.SapHanaResourceComponent
attributes:
  resource_key: sap_bw
  host: bw.acme.com
  port: 30015
  user: DAGSTER_BW
  passwd_env_var: BW_PASSWORD
  encrypt: true
```

```yaml
# defs/bw_revenue_extract/defs.yaml
type: dagster_community_components.SapHanaIngestionComponent
attributes:
  asset_name: bw_revenue_extract
  host: bw.acme.com
  port: 30015
  user: DAGSTER_BW
  password_env_var: BW_PASSWORD
  encrypt: true
  query: |
    SELECT FISCYEAR, PERIOD, COSTCENTER, AMOUNT, CURRENCY
    FROM "SAPABAP1"."/BIC/OH_REVENUE_HUB"
    WHERE FISCYEAR_PERIOD = '{partition_key}'
  partition_type: monthly
  partition_start: '2024-01-01'
  group_name: sap_bw
  kinds: [sap, bw, hana, sql]
```

## Approach B — Open Hub → file → `file_ingestion`

When BW writes to a flat file (CSV / fixed-width):

```yaml
type: dagster_community_components.FileIngestionComponent
attributes:
  asset_name: bw_revenue_extract
  format: csv
  file_path: "s3://my-bucket/bw-openhub/revenue_{partition_key}.csv"
  partition_type: monthly
  partition_start: '2024-01-01'
  group_name: sap_bw
  kinds: [sap, bw, file]
```

Coordinate with the BW basis team to write to S3-mounted CIFS / SFTP. Or have the customer's existing job sync the file to S3 — Dagster reads from there.

## BW Open Hub setup (the SAP side)

For your BW admin/basis team. Once configured, it runs on a BW DTP schedule.

1. **Open BW Modeling Tools** (Eclipse) or RSA1 in SAP GUI
2. Create an **Open Hub Destination**:
   - Target type: Database Table (recommended) or File
   - Source: an InfoProvider (DataStore Object / InfoCube)
   - Naming: typically `OH<n>_<purpose>` (e.g. `OH_REVENUE_HUB`)
   - Generated table: `/BIC/OH<n>_<purpose>` (auto-prefixed)
3. Build a **DTP** (Data Transfer Process) from the InfoProvider to the Open Hub Destination
4. Schedule via BW Process Chain (RSPC) — daily, weekly, after-load, etc.

The table that BW generates is structurally identical to the InfoProvider's column layout — readable as plain SQL.

## Approach C — BW/4HANA OData services

BW/4HANA (newer) exposes some OData services for direct query of InfoProviders. Less common in production but possible:

```yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: bw_revenue_via_odata
  service_url: https://bw.acme.com:8200/sap/opu/odata/sap/BW4_QUERY_SRV
  entity_set: ZREV_BY_PERIOD
  odata_version: v2
  auth_type: basic
  auth_username_env_var: BW_USER
  auth_password_env_var: BW_PASSWORD
  extra_headers:
    sap-client: "100"
  group_name: sap_bw
```

This bypasses Open Hub but ties Dagster to BW's query runtime — slower than reading a materialized Open Hub table.

## When to use BW vs Datasphere

| | Stay on BW | Move to Datasphere |
|---|---|---|
| **Customer has existing BW** | yes — Open Hub is fine | future modernization |
| **Greenfield analytics** | no | yes |
| **HANA-native** | BW/4HANA OK | Datasphere preferred |
| **Cloud-first** | no | yes |

For Dagster's read pattern, both work the same way: BW via SQL, Datasphere via OData consumption API.

## Trade-offs & gotchas

- **Schedule coupling.** Open Hub DTPs run on BW's schedule. Dagster's daily partition assumes the upstream DTP completed first. Either: (a) align Dagster's cron to fire after the DTP, or (b) build a `sql_monitor` sensor that watches for new rows in the Open Hub table.
- **Multi-fact partition.** BW Open Hub tables often have composite keys — partition Dagster by the BW request ID or fiscal-period column.
- **Table naming.** `/BIC/OH...` requires double-quotes in SQL: `FROM "SAPABAP1"."/BIC/OH_REVENUE_HUB"`. Forgetting this gives confusing parse errors.
- **Cleanup.** Open Hub tables grow unboundedly. Coordinate with the BW basis team on retention (DTP delete + reload, or partition pruning).
- **Customer governance.** Direct HANA access is often blocked — you may need to go via Open Hub-to-file → S3 instead.

## See also

- [`sap_hana_pipeline.md`](sap_hana_pipeline.md) — HANA SQL details (most BW is on HANA)
- [`sap_datasphere_pipeline.md`](sap_datasphere_pipeline.md) — modern alternative
- [`s3_pipeline.md`](s3_pipeline.md) — for file-based Open Hub output
- [SAP BW Open Hub docs](https://help.sap.com/docs/SAP_BW4HANA)
