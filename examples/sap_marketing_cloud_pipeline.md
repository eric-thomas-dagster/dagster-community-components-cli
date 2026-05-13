# SAP Marketing Cloud / Emarsys → Dagster

Pull customer engagement data from **SAP Marketing Cloud** (and its sibling **Emarsys**, acquired by SAP in 2020) into Dagster via OData.

Same `odata_ingestion` component as the rest of the SAP family.

## Architecture

```
   ┌──────────────────────────────────────────────────┐
   │ SAP Marketing Cloud                              │
   │   https://my-tenant.marketingcloud.sap           │
   │   /sap/opu/odata/sap/CUAN_*_SRV/...              │
   └──────────────────────┬───────────────────────────┘
                          │ OData v2 + basic auth
                          ▼
   ┌──────────────────────────────────────────────────┐
   │ odata_ingestion (asset) → DataFrame              │
   └──────────────────────────────────────────────────┘
```

## defs.yaml — pull a contact list

```yaml
type: dagster_community_components.ODataIngestionComponent
attributes:
  asset_name: marketing_contacts
  service_url: https://my-tenant.marketingcloud.sap/sap/opu/odata/sap/CUAN_INTERACTION_CONTACT_SRV
  entity_set: InteractionContacts
  odata_version: v2
  auth_type: basic
  auth_username_env_var: SAP_MC_USER
  auth_password_env_var: SAP_MC_PASSWORD
  select: ContactID,FirstName,LastName,Email,LastChangedDate,Country
  filter: LastChangedDate ge datetime'{partition_key}T00:00:00'
  partition_type: daily
  partition_start: '2024-01-01'
  group_name: sap_marketing
  kinds: [sap, marketing-cloud, odata]
```

## Common Marketing Cloud OData services

| Service | What it exposes |
|---|---|
| `CUAN_INTERACTION_CONTACT_SRV` | Contacts (people / organizations) |
| `CUAN_INTERACTION_SRV` | Interactions (every touchpoint) |
| `CUAN_CAMPAIGN_SRV` | Campaigns + members |
| `CUAN_MARKETING_LOCATION_SRV` | Locations (stores, etc.) |
| `CUAN_BUSINESS_DOC_SRV` | Business documents (orders, quotes) |
| `CUAN_OFFER_SRV` | Offers + interactions |
| `CUAN_CONTACT_CONSENT_SRV` | Consent + opt-out records |
| `CUAN_PRODUCT_SRV` | Marketing products |

## Emarsys (acquired by SAP)

Emarsys has a separate REST API (not OData) at `https://api.emarsys.net/api/v2/`. Use `oauth_rest_ingestion` with API-key auth:

```yaml
type: dagster_community_components.OAuthRestIngestionComponent
attributes:
  asset_name: emarsys_contacts
  api_url: https://api.emarsys.net/api/v2/contact/query
  # Emarsys uses WSSE auth (not standard OAuth) — set bearer-style header manually
  auth_token_env_var: EMARSYS_WSSE_HEADER
  extra_headers:
    Content-Type: application/json
  pagination: page
  page_param: offset
  records_path: data.result
```

(Emarsys WSSE = `X-WSSE: UsernameToken Username="apiuser", PasswordDigest="...", Nonce="...", Created="..."` — generate the header in a sidecar script and pass via env var.)

## Common patterns

| Pattern | Approach |
|---|---|
| **Sync Marketing Contact to Snowflake** | `odata_ingestion` → `dataframe_to_snowflake` daily, partitioned on `LastChangedDate` |
| **Push customer 360 attributes into MC** | `dataframe_to_odata` writes back via `CUAN_INTERACTION_CONTACT_SRV` |
| **ML lead scoring → MC campaign target list** | Score in Dagster → write Score attribute back |
| **Cross-channel attribution** | Pull interactions + orders, join, push final attribution model back |

## Consent + GDPR

Use the `CUAN_CONTACT_CONSENT_SRV` service to read opt-out / consent records BEFORE downstream marketing-related processing. Tag downstream assets `pii: true` + `gdpr: true`.

## Trade-offs & gotchas

- **Volume.** Contact bases can be millions. Always partition + filter by `LastChangedDate`. `$top` caps at 5000.
- **Auth: basic on Cloud.** Set up Communication Arrangements with the `Marketing_Communication_*` scenarios.
- **Time zones.** All MC timestamps are UTC in OData responses. Watch for client-side TZ assumptions.
- **Emarsys is a different stack.** Even though SAP owns both, the APIs don't share auth or shape. Treat them as separate integrations.

## See also

- [`sap_s4hana_pipeline.md`](sap_s4hana_pipeline.md) — same OData, different SAP product
- [`odata_pipeline.md`](odata_pipeline.md) — protocol-level walkthrough
- [SAP Marketing Cloud API docs](https://api.sap.com/package/SAPCXMarketing/all)
