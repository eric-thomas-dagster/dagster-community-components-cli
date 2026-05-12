# Firestore Round-trip — write + read with filter

**Validated end-to-end against real APIs** (servicepulse-490502, devices collection).
5 device docs written, 4 active devices read back with a `WHERE status == active` filter.

```
sensor_readings        ← synthetic_data_generator (sensors schema)
       │
       └── sensors_written     ← firestore_writer_asset
                                 (collection: sensors, id from sensor_id)
                │
                └── sensors_normal   ← firestore_reader_asset
                                       (where status == "normal")
```

## Components covered (3)

| Component | What it does |
|---|---|
| `synthetic_data_generator` | Synthetic upstream. `schema_type: sensors` produces `(sensor_id, timestamp, sensor_type, location, value, unit, status)` — sensor_id maps cleanly to a Firestore doc id, status produces a queryable filter. |
| `firestore_writer_asset` | One DataFrame row → one Firestore doc. `id_column` for doc id, `write_mode` (set/merge/create), batched via Firestore batch commits. |
| `firestore_reader_asset` | Collection or collection-group query → DataFrame. Supports `where` filters, `order_by`, `limit`, sub-collection paths. |

## Live run output

- **Writer**: 5 docs written to `devices` collection. Mode `set` (creates or overwrites).
- **Reader**: filter `status == "active"` → 4 docs (dev-001, dev-002, dev-004, dev-005). dev-003 (inactive) correctly excluded.

## Patterns

| Goal | Setup |
|---|---|
| Idempotent upsert by id | `write_mode: set`, `drop_id_column_from_body: true` |
| Partial update (preserve other fields) | `write_mode: merge` |
| Create-only (error on existing) | `write_mode: create` |
| Time-series-style log appends | `id_column` omitted → auto-id |
| Read all docs across nested collections | `collection_group: true` |

## Required setup (one-time)

```bash
# 1. Enable Firestore API
# https://console.cloud.google.com/apis/library/firestore.googleapis.com

# 2. Create the database (Native mode is the modern choice)
gcloud firestore databases create --location=us-central1 \
  --type=firestore-native --project=$GCP_PROJECT_ID

# 3. IAM
gcloud projects add-iam-policy-binding $GCP_PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" --role="roles/datastore.user"
```

## Cost

**Free at this scale.** Firestore Native free tier: 50K document reads, 20K writes, 20K deletes per day. 1 GB storage. Beyond that: $0.06/100K reads, $0.18/100K writes.

## When to pick Firestore vs. peers

| You need | Use |
|---|---|
| Document/JSON CRUD with strong consistency | Firestore Native |
| Wide-column at petabyte scale | Bigtable (`bigtable_reader_asset`) |
| Transactional RDBMS at global scale | Spanner (`spanner_query_asset`) |
| Analytic scans | BigQuery (`bigquery_query_asset`) |

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_firestore_roundtrip_demo.sh | bash
cd firestore-roundtrip-demo
uv run dg launch --assets '*'
```
