# Airtable — DataFrame → Airtable Records Upsert

**Components:**
- `AirtableResourceComponent` (`resources/airtable_resource`)
- `AirtableRecordUpsertComponent` (`assets/sinks/airtable_record_upsert`)
- `InlineDataframeComponent` (`assets/sources/inline_dataframe`)

**Script:** [`setup_airtable_demo.sh`](./setup_airtable_demo.sh)
**Cost:** $0 (Airtable's free tier — 1,200 records / base)
**Duration:** ~20 seconds from cold to green
**Validated:** 2026-08-16 (RUN_SUCCESS end-to-end. 2 back-to-back runs both `0 created, 5 updated` on run 2 — Airtable's `performUpsert` is server-side and immediately consistent, so idempotency is trivially clean.)

> ✅ **Dagster+ Serverless:** deploys as-is (Airtable REST is HTTP-based)

## What it demonstrates

Same resource + sink pattern as [Notion](./notion.md), [GitHub](./github.md), [Jira](./jira.md), [PagerDuty](./pagerduty.md), and [Stripe](./stripe.md), applied to Airtable — and probably the simplest of the six because Airtable has **native server-side upsert**.

## Pipeline

```
┌────────────────────────┐
│  tasks_seed            │  InlineDataframeComponent
│  (5 rows in defs.yaml) │
└──────────┬─────────────┘
           │
           ▼
┌────────────────────────────────┐         ┌───────────────────────────────────┐
│  airtable_tasks_mirror         │ ──────▶ │  Airtable records in <table>      │
│  AirtableRecordUpsertComponent │         │  (5 records, matched on `Name`    │
│                                │         │   via server-side performUpsert)  │
└────────────────────────────────┘         └───────────────────────────────────┘
```

## The sink component

```yaml
type: dagster_community_components.AirtableRecordUpsertComponent
attributes:
  asset_name: airtable_tasks_mirror
  upstream_asset_key: tasks_seed
  resource_key: airtable
  base_id: appXXXXXXXX
  table: "Table 1"
  key_fields: [Name]         # Airtable matches on these server-side
  fields_map:
    name: Name
    description: Notes
  typecast: true             # coerce strings into typed fields (dates, selects, etc.)
```

The sink calls Airtable's `PATCH /v0/{baseId}/{table}?performUpsert[fieldsToMergeOn][]=Name` under the hood. Airtable does the create-or-update matching server-side — same record wire twice yields one record. No search-then-write dance, no eventual-consistency window, no idempotency-key gymnastics. This is the cleanest upsert story of the six vendors we've reshaped.

**One thing to know:** all `key_fields` must be present in `fields_map` values. If you match on `Name` but don't write to `Name`, the sink can't tell Airtable what to match. The component validates this at runtime and fails fast with a clear message.

## `AirtableResource` convenience methods (~10 total)

**Reads:** `list_bases`, `get_base_schema`, `get_record`, `list_records` / `iter_records` (with `filter_by_formula` + `view` support).

**Writes:** `create_records`, `update_records`, `upsert_records` (native), `delete_records`. All auto-chunk at Airtable's 10-per-request limit.

**Escape hatch:** `get_client()` returns an authenticated `requests.Session` pointed at `api.airtable.com/v0`.

## Requirements

- **`AIRTABLE_API_KEY`** — Personal Access Token from [airtable.com/create/tokens](https://airtable.com/create/tokens). Scopes needed: `data.records:read`, `data.records:write`, and `schema.bases:read`. Grant access to the specific base you're using.
- **`AIRTABLE_BASE_ID`** — starts with `app`, from the base's URL (or the API docs page for the base).
- **`AIRTABLE_TABLE`** — defaults to `"Table 1"` (Airtable's default new-base first table). Override if your table is named differently.
- The table must have a `Name` field (a `singleLineText` — the default first field of any new Airtable table) and a `Notes` field (default second field). If either is missing, adjust `fields_map` in the demo defs.yaml.
- `uv` / `uvx`, Python 3.12+.

## Running

```bash
export AIRTABLE_API_KEY=patXXX...
export AIRTABLE_BASE_ID=appXXX...
export AIRTABLE_TABLE="Table 1"     # optional
./setup_airtable_demo.sh
```

## Auth notes

Airtable Personal Access Tokens are strictly scoped:
- **Base access:** the token has explicit access to a list of bases you pick at creation. Adding new bases requires updating the token.
- **Scopes:** granular — `data.records:read`, `data.records:write`, `schema.bases:read`, etc. Each maps to specific endpoint groups.

There's no "workspace" or "account" level token — everything is per-token with an explicit base list.

## Cleanup

The setup script's final step deletes every record whose `Name` starts with `dagster-demo:`. Airtable records deleted via the API are permanently gone (not soft-deleted).
