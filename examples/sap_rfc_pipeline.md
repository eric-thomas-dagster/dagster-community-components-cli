# SAP RFC → Dagster pipeline blueprint

Pull data from on-prem **SAP R/3 / ECC / S/4HANA** systems via RFC (Remote Function Call) — the classic SAP integration protocol that long predates OData. For customers stuck on ECC or any on-prem SAP install without a Gateway OData layer, **this is the only path**.

## When this is the right path

| Path | Customer profile |
|---|---|
| **`sap_rfc_*`** (this walkthrough) | On-prem R/3 / ECC / S/4HANA. No OData exposed. ABAP integration shop |
| [`odata_ingestion`](https://dagster-community-components-cli.vercel.app/c/odata_ingestion) | S/4HANA Cloud or modern on-prem with Gateway OData |
| [`sap_hana_ingestion`](https://dagster-community-components-cli.vercel.app/c/sap_hana_ingestion) | Direct HANA SQL (when customer allows DB access) |

Most ECC customers — and that's a huge chunk of enterprise SAP — only have RFC.

## Architecture

```
   ┌─────────────────────────────────────────────────────┐
   │ SAP system (R/3 / ECC / on-prem S/4HANA)            │
   │   - BAPIs, Z-RFCs, RFC_READ_TABLE                   │
   │   - SAP NW RFC SDK over port 3300 (gateway)         │
   └─────────────────────────────┬───────────────────────┘
                                 │ pyrfc + NW RFC SDK
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ sap_rfc_resource (connection)                       │
   │   ashost OR mshost (load-balanced)                  │
   │   client + user + password + lang + SNC             │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ sap_rfc_ingestion (asset)                           │
   │   mode: read_table → RFC_READ_TABLE                 │
   │   mode: bapi → arbitrary BAPI / Z-RFC               │
   │   → pandas DataFrame                                │
   └─────────────────────────────┬───────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────┐
   │ Downstream transforms + sinks                       │
   └─────────────────────────────────────────────────────┘
```

## The SAP NW RFC SDK install (the hard part)

`pyrfc` is the official SAP-supplied Python binding to a closed-source C library — the **SAP NetWeaver RFC SDK**. Free for SAP-licensed customers; closed to non-customers.

### Install sequence

1. **Download** SAP NW RFC SDK 7.50 from https://launchpad.support.sap.com → Software Downloads → SAP NW RFC SDK 7.50 (requires SAP customer login)
2. **Extract** to a stable path:
   ```bash
   mkdir -p /opt/sap
   tar -xzf nwrfcsdk-linux64.tgz -C /opt/sap/   # produces /opt/sap/nwrfcsdk
   ```
3. **Set env vars** before `pip install pyrfc`:
   ```bash
   export SAPNWRFC_HOME=/opt/sap/nwrfcsdk
   export LD_LIBRARY_PATH=$SAPNWRFC_HOME/lib:$LD_LIBRARY_PATH       # Linux
   export DYLD_LIBRARY_PATH=$SAPNWRFC_HOME/lib:$DYLD_LIBRARY_PATH   # macOS
   ```
4. **Install** the Python binding:
   ```bash
   pip install pyrfc
   ```

For Docker / k8s: bake the SDK into the image and set env in the Dockerfile.

```dockerfile
FROM python:3.11-slim
COPY nwrfcsdk-linux64.tgz /tmp/
RUN mkdir -p /opt/sap \
 && tar -xzf /tmp/nwrfcsdk-linux64.tgz -C /opt/sap/ \
 && rm /tmp/nwrfcsdk-linux64.tgz
ENV SAPNWRFC_HOME=/opt/sap/nwrfcsdk
ENV LD_LIBRARY_PATH=$SAPNWRFC_HOME/lib
RUN pip install pyrfc dagster dagster-community-components
```

## Components used

| Component | Source | Role |
|---|---|---|
| [`sap_rfc_resource`](https://dagster-component-ui.vercel.app/c/sap_rfc_resource) | community | Holds connection params; opens pyrfc Connection per call |
| [`sap_rfc_ingestion`](https://dagster-component-ui.vercel.app/c/sap_rfc_ingestion) | community | Executes RFC_READ_TABLE or BAPI → DataFrame |

## defs.yaml — resource

```yaml
# resources/sap_ecc.yaml
type: dagster_community_components.SapRFCResourceComponent
attributes:
  resource_key: sap_ecc
  ashost: sapecc01.acme.com   # for load balancing: use mshost + sysid + group
  sysnr: "00"
  client: "100"
  user: DAGSTER_RFC
  passwd_env_var: SAP_RFC_PASSWORD
  lang: EN
  # SNC for prod (set by SAP basis team):
  # snc_qop: "3"
  # snc_myname: "CN=dagster-rfc,O=Acme"
  # snc_partnername: "CN=sapecc01,O=Acme"
  # snc_lib: /usr/sap/sapcryptolib/libsapcrypto.so
```

## defs.yaml — read MARA (material master) via RFC_READ_TABLE

```yaml
# defs/material_master/defs.yaml
type: dagster_community_components.SapRFCIngestionComponent
attributes:
  asset_name: material_master
  sap_rfc_resource_key: sap_ecc
  mode: read_table
  table_name: MARA
  fields: [MATNR, MTART, MATKL, MEINS, ERSDA, ERNAM]   # always project — tables are wide
  where_clause: "MTART = 'FERT' AND ERSDA >= '{partition_key}'"
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: sap_ecc
```

## defs.yaml — call BAPI_USER_GETLIST

```yaml
# defs/sap_users/defs.yaml
type: dagster_community_components.SapRFCIngestionComponent
attributes:
  asset_name: sap_users
  sap_rfc_resource_key: sap_ecc
  mode: bapi
  rfc_name: BAPI_USER_GETLIST
  import_params:
    MAX_ROWS: 5000
  result_table: USERLIST    # which result table to flatten to a DataFrame
  group_name: sap_ecc
```

## Common tables for `read_table` mode

| Table | Domain | What it holds |
|---|---|---|
| `MARA` | MM | Material master (general) |
| `MARC` | MM | Material per plant |
| `MBEW` | MM | Material valuation |
| `KNA1` | SD | Customer master |
| `LFA1` | MM | Vendor master |
| `VBAK` / `VBAP` | SD | Sales order header / items |
| `EKKO` / `EKPO` | MM | PO header / items |
| `BKPF` / `BSEG` | FI | Accounting doc header / items |
| `MKPF` / `MSEG` | MM | Material doc header / items |
| `PA0001` / `PA0002` | HCM | Employee org assignment / personal data |
| `T001` / `T001W` | Foundation | Company codes / plants |

## Common BAPIs

| BAPI | Returns |
|---|---|
| `BAPI_MATERIAL_GET_LIST` | Materials with filter |
| `BAPI_MATERIAL_GET_DETAIL` | Single material detail |
| `BAPI_CUSTOMER_GETLIST` | Customers |
| `BAPI_SALESORDER_GETLIST` | Sales orders |
| `BAPI_PO_GETITEMS` | PO line items |
| `BAPI_USER_GETLIST` | SAP users |
| `BAPI_COMPANYCODE_GETLIST` | Company codes |
| `BAPI_GL_ACC_GETLIST` | GL accounts |

In SAP GUI: `SE37` → enter the BAPI name → see import/export signature. Test it with the green ▶ button before wiring into Dagster.

## RFC_READ_TABLE limits

SAP server enforces:
- Row width ≤ 512 chars after concatenation — split via `fields:`
- Result size ~50K rows per call — use `rowcount` + `rowskips` to paginate, OR submit a custom Z-RFC server-side that pages internally
- `WHERE` clauses ≤ 75 chars each (the component splits longer ones automatically — but keep total length reasonable)

For wide / huge tables, the right answer is often:
1. A custom **Z-RFC** that pages server-side
2. **BW Open Hub Destination** + read its output table via `sap_hana_ingestion` (see [`sap_bw_pipeline.md`](sap_bw_pipeline.md))
3. **SAP Datasphere** in front of ECC

## Asset check — RFC_PING

Test connectivity before the real assets run:

```python
@asset_check(asset=AssetKey(["material_master"]))
def sap_ecc_reachable(context, sap_ecc):
    sap_ecc.ping()
    return AssetCheckResult(passed=True)
```

## Trade-offs & gotchas

- **The SDK is closed-source.** Customers download it under their SAP license. Without a license, you can't run this. Document that clearly.
- **RFC_READ_TABLE is not Production-Grade for high-volume.** It's per-row delimiter-decoded; for >50K rows or wide tables, write a custom Z-RFC.
- **Connection authorization.** The RFC user needs SAP authorizations for the tables/BAPIs you're calling. Often easier than DB-level access; basis teams approve more readily.
- **SNC is mandatory in production.** Coordinate with the SAP basis team — they'll provide the right `snc_*` values. Without SNC, network traffic is in clear-text.
- **Heartbeat / connection drops.** Long-running RFC calls can drop on idle networks. The component opens a fresh `Connection()` per asset run — short-lived, no idle issues.

## See also

- [`sap_rfc_resource`](https://dagster-component-ui.vercel.app/c/sap_rfc_resource) / [`sap_rfc_ingestion`](https://dagster-component-ui.vercel.app/c/sap_rfc_ingestion)
- [`sap_s4hana_pipeline.md`](sap_s4hana_pipeline.md) — OData alternative for S/4HANA Cloud
- [`sap_hana_pipeline.md`](sap_hana_pipeline.md) — HANA SQL alternative
- [`sap_bw_pipeline.md`](sap_bw_pipeline.md) — BW Open Hub for high-volume extraction
- [pyrfc docs](https://sap.github.io/PyRFC/)
