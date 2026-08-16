# Notion — DataFrame → Notion DB + KPI Sync

**Components:**
- `NotionResourceComponent` (`resources/notion_resource`)
- `NotionDatabaseUpsertComponent` (`assets/sinks/notion_database_upsert`)
- `NotionPageSyncComponent` (`assets/sinks/notion_page_sync`)
- `InlineDataframeComponent` (`assets/sources/inline_dataframe`)

**Script:** [`setup_notion_demo.sh`](./setup_notion_demo.sh)
**Cost:** $0 (Notion API is free within rate limits)
**Duration:** ~15 seconds from cold to green
**Validated:** 2026-08-15 (RUN_SUCCESS end-to-end; 5 DB rows + 4 page props patched, verified via Notion API. Idempotency also verified — three consecutive runs produced no duplicates, second and third runs both showed `0 created, 5 updated`.)

> ✅ **Dagster+ Serverless:** deploys as-is (Notion API is HTTP-based, no local dependencies)

## What it demonstrates

The shape a SaaS API integration takes in the community registry when there is **no orchestration primitive** to trigger (i.e. no "run this workflow" or "refresh this dataset"). Instead of an enumeration-based `_workspace` component, we build:

1. **A rich `_resource`** — with read AND write convenience methods for the API. Consumers use it directly from their own assets (`context.resources.notion.query_database(...)`).
2. **Sink assets for common upsert patterns** — `_database_upsert` (multi-row DB mirror) and `_page_sync` (single-page property patch).
3. **Existing `_ingestion` component** — dlt-based bulk read into a warehouse (already covered by `notion_ingestion`).
4. **Existing `_sensor` component** — pollable trigger on DB changes (already covered by `notion_database_sensor`).

This replaces the old `notion_workspace` component, which was really just multi-DB ingestion in workspace clothing.

## Pipeline

```
┌────────────────────────┐
│  incidents_seed        │  InlineDataframeComponent
│  (5 rows in defs.yaml) │
└──────────┬─────────────┘
           │
           ▼
┌────────────────────────────────┐         ┌────────────────────────────┐
│  notion_incidents_mirror       │ ──────▶ │  Notion Incidents DB       │
│  NotionDatabaseUpsertComponent │         │  (5 pages, keyed by Name)  │
└──────────┬─────────────────────┘         └────────────────────────────┘
           │ deps:
           ▼
┌────────────────────────┐
│  kpi_rollup            │  InlineDataframeComponent
│  (1 row of aggregates) │  (Open=3, P0=1, Health=🔴 Critical)
└──────────┬─────────────┘
           │
           ▼
┌───────────────────────────────┐        ┌────────────────────────────┐
│  notion_kpi_page_sync         │ ─────▶ │  Notion "Weekly Summary"   │
│  NotionPageSyncComponent      │        │  row inside KPI Dashboard  │
└───────────────────────────────┘        └────────────────────────────┘
```

## The two sink components in detail

### `NotionDatabaseUpsertComponent`

Mirrors an upstream DataFrame into a Notion database. Each row is matched against existing pages by a `key_property`; matches are updated, misses are inserted. Optional `delete_missing: true` archives rows in Notion that are not present upstream (full-mirror mode; off by default).

```yaml
type: dagster_community_components.NotionDatabaseUpsertComponent
attributes:
  asset_name: notion_incidents_mirror
  upstream_asset_key: incidents_seed
  resource_key: notion_resource
  database_id: "abc123..."
  key_property: Name
  key_column: name
  properties_map:
    name: Name
    severity: Severity
    status: Status
    description: Description
    priority: Priority
```

Property values are auto-serialized based on the Notion DB schema — you don't build the nested API objects yourself. Supported: `title`, `rich_text`, `number`, `select`, `multi_select`, `checkbox`, `date`, `url`, `email`, `phone_number`, `status`.

### `NotionPageSyncComponent`

Patches a specific Notion page's properties from the first row of an upstream DataFrame. Common shape: nightly rollup asset → sync into a fixed dashboard page.

```yaml
type: dagster_community_components.NotionPageSyncComponent
attributes:
  asset_name: notion_kpi_page_sync
  upstream_asset_key: kpi_rollup
  resource_key: notion_resource
  page_id: "3be18b92..."
  properties_map:
    open_incidents: Open Incidents
    p0_count: P0 Count
    last_refresh: Last Refresh
    health: Health
```

Optional `markdown_column` also replaces the page body via `pages.update_markdown` — handy for narrative summaries.

## `NotionResource` convenience methods

Beyond the two sinks, `NotionResource` exposes 19 convenience methods so your own assets can talk to Notion without touching the raw client:

**Reads:** `search` / `iter_search`, `get_page`, `get_page_markdown`, `get_database`, `query_database` / `iter_query_database`, `get_block_children` / `iter_block_children`, `get_comments`, `list_users`, `whoami`.

**Writes:** `create_page`, `update_page`, `append_blocks`, `add_comment`, `create_database`, `upload_file`.

**Escape hatch:** `get_client()` returns the raw `notion_client.Client` for anything not covered.

The `query_database`, `iter_query_database`, and DB write methods transparently handle Notion's 2025 API change where queries moved from `databases.query()` to `data_sources.query()`.

## Requirements

- **Notion integration token** — create one at [notion.so/my-integrations](https://www.notion.so/my-integrations), then share the parent page (below) with the integration in the Notion UI.
- **A "sandbox" parent page ID** — the setup script creates its scratch databases as children of this page. Cleanup is one click (archive the two DBs in Notion after running).
- `uv` / `uvx`, Python 3.12+.

## Running

```bash
export NOTION_TOKEN=secret_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
export NOTION_PARENT_PAGE_ID=3b318b92e46280ed81fbe57953414122   # page shared with the integration
./setup_notion_demo.sh
```

The script scaffolds a Dagster project, creates two scratch DBs under your parent page, wires the four assets, materializes the pipeline, and verifies content actually landed in Notion via the API.

## Cleanup

The setup script prints the URL of each scratch database on completion. Archive them from the Notion UI (or via `c.databases.update(database_id=..., in_trash=True)` in the API).
