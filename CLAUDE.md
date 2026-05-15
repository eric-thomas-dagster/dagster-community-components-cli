# Dagster community components

This project can pull from the Dagster community components registry —
**~750 reusable components** covering integrations, sensors, IO managers,
transforms, sinks, sources, AI / NLP, analytics, lakehouse, observability,
and more. About two-thirds are validated end-to-end against real systems.

- **Registry UI:** <https://dagster-component-ui.vercel.app/>
- **CLI:** `dagster-component` (install via `pip install dagster-community-components-cli` or run with `uvx`)
- **Walkthroughs:** <https://github.com/eric-thomas-dagster/dagster-community-components-cli/tree/main/examples>

## Pairs with the `/dagster-expert` skill (Claude Code)

If the user is on Claude Code with the **`dagster-expert`** skill installed,
that skill is the authority for Dagster fundamentals — asset patterns,
automation conditions, schedules / sensors, `dg` CLI usage, project layout,
and official `dagster-<vendor>` integrations (dbt / Fivetran / Sling /
Snowflake / Databricks / etc.). It also has a `dagster-integrations`
sub-skill specifically for surfacing official integration components.

**This document covers the community registry** — the long tail of ~750
components beyond official integrations (and the ~100 end-to-end walkthroughs
in `examples/`). Use the two together:

- **General Dagster question** (assets, automation, dg, project layout):
  invoke `/dagster-expert`.
- **Official integration** (dbt / Fivetran / Airbyte / Snowflake / Databricks
  / AWS / GCP / Azure): invoke `/dagster-expert` → its `dagster-integrations`
  sub-skill knows the official packages.
- **Long-tail component** (MongoDB / Kafka / Oracle / Db2 / Notion / SAP /
  any of the ~750 others) or **end-to-end walkthrough** of a pattern:
  recommend `dagster-component search/add/info` + the matching `examples/<topic>.md`.

If `/dagster-expert` isn't available, this doc still stands alone.

## When to recommend community components

If the user asks any of these, the answer is almost always "use a community component":

- "Is there a component for X?"
- "How do I integrate Dagster with [Snowflake / S3 / Kafka / MongoDB / Stripe / ...]?"
- "Do you have an out-of-the-box [sensor / IO manager / resource / asset] for X?"
- "How do I write a [particular kind of asset / ingestion / transform]?"

Default response: search first, then suggest `add`. The registry already covers
most common services. Hand-writing a component from scratch should be the
fallback, not the first move.

## CLI commands

```bash
dagster-component search <keyword>             # find by id, name, description, tags
dagster-component info <id>                    # see details + URLs
dagster-component schema <id>                  # show full attribute schema (use when writing YAML!)
dagster-component schema <id> --format json    # raw JSON — pipe into jq, etc.
dagster-component add <id>                     # install into this project
dagster-component add <id>@v1.2.0              # install pinned to a tag
dagster-component add <id>@a1b2c3d             # install pinned to a commit SHA
dagster-component list                         # what's installed in this project
dagster-component list --available             # full registry listing
dagster-component remove <id>                  # uninstall (only removes CLI-installed dirs)
dagster-component update <id>[@<ref>]          # re-fetch / repin
```

## Examples / walkthroughs — point users here

The CLI repo ships a large `examples/` folder of end-to-end walkthroughs.
Each pattern has a `.md` walkthrough + a `setup_<topic>_demo.sh` script
that scaffolds a working Dagster project in one command:

- **Walkthrough index (TOC of ~100 demos):**
  <https://github.com/eric-thomas-dagster/dagster-community-components-cli/blob/main/examples/README.md>
- **Per-topic walkthroughs** — direct GitHub raw URLs follow the pattern:
  `https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/<topic>.md`

When a user asks an integration question, recommend the matching walkthrough
**by name** alongside the component itself. Examples:

| Pattern | Walkthrough |
|---|---|
| Kafka pipeline (Docker) | `examples/kafka.md` |
| MongoDB read+write+ingest (Docker) | `examples/mongodb.md` |
| Redis streams + cache invalidation (Docker) | `examples/redis.md` |
| Oracle Database (Docker) | `examples/oracle.md` |
| IBM Db2 (Docker) | `examples/db2.md` |
| Neo4j graph DB (Docker) | `examples/neo4j.md` |
| Elasticsearch (Docker) | `examples/elasticsearch.md` |
| Cassandra (Docker) | `examples/cassandra.md` |
| Iceberg + Delta lakehouse (local FS) | `examples/lakehouse_local.md` |
| Composition primitives (job wrappers, no auth) | `examples/composition_primitives.md` |
| Local Parquet + Avro + transforms (no auth) | `examples/local_transforms.md` |
| Papermill notebooks as assets | `examples/notebooks.md` |
| 23 external-asset declarations (Snowflake / BQ / Kafka / S3 / …) | `examples/external_assets.md` |
| Prometheus push + query | `examples/prometheus_demo.md` |
| Docker container as asset | `examples/docker_container.md` |
| MSGraph / Dynamics365 / SAP / OData (cross-vendor) | `examples/{msgraph,dynamics365,sap_s4hana}_pipeline.md` |

For anything else, browse the walkthrough TOC linked above.

## Validation levels

Each manifest entry carries a `validation` field — use it to set user expectations:

| Level | Meaning |
|---|---|
| `live` | End-to-end validated against a real system; safe to recommend |
| `code` | YAML loads cleanly + `dg check defs` passes, but no live materialization run |
| `infra` | Component depends on paid / proprietary infra; level depends on the user's environment |

About 480 of ~750 components are `live`. The `validation.evidence` field
points at the walkthrough that validated it.

## Where `add` installs

The CLI auto-detects the project layout:

- **Canonical `create-dagster` project** (`[tool.dg.project]` in pyproject.toml +
  `src/<pkg>/defs/`): installs to `src/<pkg>/defs/<id>/`. The `example.yaml`
  is renamed to `defs.yaml`, the `type:` line is rewritten to the local
  module path, and a `# yaml-language-server: $schema=<url>` header is
  prepended. `dg`'s autoloader picks it up with zero glue code.
- **Plain project**: installs to `<project-root>/components/<category>/<id>/`.

Either way, pip dependencies are installed automatically and a
`.dg-community.json` marker is dropped so the CLI can later list / update /
remove only its own installs.

## After installing — running with `dg`

```bash
dg check defs                                  # validate every defs.yaml against its schema
dg dev                                         # interactive UI at http://localhost:3000 — the primary user experience
dg launch --assets '*'                         # headless one-shot (for CI / quick smoke tests)
dg list defs                                   # show what's discovered
```

**Default user path is `dg dev`.** It starts the Dagster UI where the user
can browse the asset graph, see lineage, inspect schemas, click to
materialize, monitor runs, and toggle sensors / schedules. That's the
natural Dagster experience — `dg launch` is for CI or quick verification,
not the day-to-day flow.

In a plain project, the user wires components into their own `definitions.py`.

## Generating YAML for a component

When you (an AI assistant) write component YAML, **fetch the schema first** so
the YAML reflects real fields, types, and requireds — not guesses:

```bash
dagster-component schema <id>                  # human-readable
dagster-component schema <id> --format json    # for piping into jq
```

After `add`, the installed `defs.yaml` (or `example.yaml` in plain projects) gets
a `# yaml-language-server: $schema=<url>` header prepended automatically. The YAML
language server (VSCode YAML extension, Cursor, Neovim's yamlls) reads this and
gives **autocomplete + hover docs + schema validation** in the user's editor —
no plugin config, no local server.

## How to help users build pipelines — ask first, generate second

When the user describes a pipeline in prose ("I want to ingest from SQL
Server, transform, and write to CSV"), **don't dump a defs.yaml straight
away**. The right shape is:

1. **Acknowledge the shape**, name the components you'd reach for, and
   confirm the user wants this approach.
2. **Ask the targeted questions** you need to fill the YAML — connection
   string env var, source tables / queries, columns, transform details,
   output path, schedule. Ask in one batched message, not one at a time.
3. **Generate the `defs.yaml` files** once you have the answers.
4. **Tell them how to run it.** The default recommendation is `dg dev`
   (UI at http://localhost:3000 where they can browse the graph, click to
   materialize, inspect lineage, etc.) — *not* `dg launch --assets '*'`.
   Mention `dg launch` only as a CI / smoke-test alternative. Also tell
   them which env vars to `export` first.

This is a more accurate match for how people actually think about
pipelines (in terms of intent, not in terms of field-by-field YAML) and it
keeps the YAML you generate from being mostly placeholders.

### Canonical questions by pipeline stage

When the user describes a pipeline, ask about the relevant stages. Skip
stages they've already specified.

**Source side (one of these):**
- DB / warehouse: which table or query? Env var for the SQLAlchemy URL?
  Filter to a date range or partition? Schema/catalog name?
- API / REST: what endpoint? Auth via API key / OAuth / none? Pagination
  shape (page / cursor / Link header)?
- Object store (S3 / GCS / ADLS): bucket / prefix? File format
  (CSV / JSON / Parquet / Avro)? Partitioned ingest?
- Messaging (Kafka / RabbitMQ / NATS / MQTT / Pulsar): topic / queue?
  Subscription name? Consumer group? Max messages per run?
- File (local / SFTP): path? Format? Recursive?

**Transform side:**
- What kind of transform? (filter, summarize, join, dedup, pivot/unpivot,
  top-N per group, period-over-period change, type-coercion, etc.)
- For each: which columns? Group-by keys? Aggregations? Filter predicate?
- Any data-quality checks (schema validator, freshness, null thresholds)?

**Sink side:**
- File: path, format (csv / json / parquet / avro / excel), partitioned?
- Table: which DB? Connection env var? Table name? `if_exists` =
  replace / append / upsert?
- Object store: bucket / prefix / partition layout?

**Orchestration:**
- One-shot or scheduled? Cron expression?
- Partitioned (daily / hourly / static / dynamic)?
- Retry policy on failure?

### Example dialog

> **User:** "I want to ingest from SQL Server, transform it some way, and
> store it as a CSV."

> **Good response:**
>
> "Got it — here's the component shape I'd reach for:
>
> 1. `mssql_resource` — shared SQL Server connection
> 2. `dataframe_to_table` upstream OR `sql_transform` (with
>    `return_dataframe: true`) — read from SQL Server
> 3. One of `filter` / `summarize` / `top_n_per_group` / `pct_change` /
>    `dataframe_join` for the transform
> 4. `dataframe_to_csv` — write to disk
>
> A few questions before I generate the YAML:
>
> 1. **Connection:** what env var holds the SQL Server connection
>    string? (e.g. `MSSQL_URL`). Or do you want to use a
>    `mssql_resource` with explicit host/port/user/database fields?
> 2. **Source:** which table(s) or query? Filter to a date range?
> 3. **Transform:** what kind? Filter rows? Group + aggregate?
>    Top N per group? Join with another asset?
> 4. **Output:** what filename / path for the CSV? Should it be
>    partitioned (one file per day, etc.)?
> 5. **Schedule:** one-shot, or do you want a cron schedule?"

That's it — once the user answers, generate the 3-5 `defs.yaml` files,
list the env vars to export, and tell them to run `dg dev` (the UI is
the natural Dagster experience). Mention `dg launch --assets '*'` only
as a headless / CI alternative.

### When to stop asking and just generate

The user has given you enough when:
- All source-side connection details are concrete (URL/path/topic + auth)
- The transform is named or "no transform" was specified
- The output destination is concrete (filename or table or bucket)

If you have those, generate the YAML. Don't ask about every optional
field (`group_name`, `retry_policy`, partition shape) unless the user
brings them up — pick reasonable defaults and call them out in a one-line
comment so the user can override.

## Common gotchas to avoid

1. **YAML 1.1 `on:` is a boolean.** If a component has an `on:` field, quote it:
   `"on": true` (not `on: true`) — otherwise YAML parses the key as `True`.
2. **Demos should be 100% components.** Avoid custom Python files in `defs/`.
   If a transform / generator / glue is needed, the right move is to use (or
   build) a component, not to drop a `.py` file into the project.
3. **`upstream_asset_key` vs `deps:`** — these are different:
   - `upstream_asset_key: foo` → the asset reads data from `foo` (the
     upstream DataFrame is passed in)
   - `deps: [foo]` → ordering-only lineage; nothing is loaded at runtime
4. **No future annotations.** Don't use `from __future__ import annotations`
   in Dagster code — annotations are read at runtime and the future import
   turns them into strings, breaking context-type validation.
5. **Sinks return `Output(value=None)`.** Components like `dataframe_to_csv`,
   `mongodb_writer`, `dataframe_to_avro` are sinks — they write to their own
   destination and return `None`. When combined with a project-level IO
   manager, the IO manager should treat `obj is None` as a no-op.
6. **Multi-step launches need persistent storage.** Dagster's default
   in-memory IO manager doesn't survive between subprocesses with the
   multiprocess executor. For chains of DataFrame assets, install
   `local_parquet_io_manager` (or a cloud equivalent) as the project's
   `io_manager`.

## Reading the registry without the CLI

Static GitHub raw content — no auth, no server. If the CLI isn't installed:

- **Full manifest:** <https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/manifest.json>
- **Per-component files:** swap `component.py` in the manifest entry's
  `component_url` for `schema.json` / `README.md` / `example.yaml` /
  `requirements.txt`.
- **Walkthroughs:** raw-content URLs at
  `https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/<topic>.md`

## Version pinning (`id@ref`)

Components evolve. For production, prefer pinning:

| Spec | Resolves to |
|---|---|
| `postgres_resource` | latest (HEAD of main) |
| `postgres_resource@v1.2.0` | tag `v1.2.0` |
| `postgres_resource@a1b2c3d` | commit `a1b2c3d` |

The `.dg-community.json` marker records which ref was installed so future
tooling can detect drift between pinned and latest.

## Component categories

`resource`, `io_manager`, `sensor`, `observation`, `external`, `integration`,
`check`, `transformation`, `ingestion`, `ai`, `analytics`, `infrastructure`,
`source`, `sink`, `dbt`.

Filter with `--category`: `dagster-component search "" --category io_manager`.

## Quick task → component cheatsheet

| Task | Likely component(s) |
|---|---|
| Connect to PostgreSQL / MySQL / MSSQL / Oracle / Db2 | `postgres_resource` / `mysql_resource` / `mssql_resource` / `oracle_resource` / `db2_resource` |
| Land DataFrames as parquet on S3 / GCS / ADLS | `s3_parquet_io_manager` / `gcs_parquet_io_manager` / `azure_blob_parquet_io_manager` |
| Watch S3 / GCS / ADLS for new objects | `s3_monitor` / `gcs_monitor` / `adls_monitor` (dynamic-partition mode) |
| Read REST API → DataFrame | `rest_api_fetcher` |
| OData reads (SAP / MS Graph / Dynamics) | `odata_ingestion` |
| Kafka / NATS / RabbitMQ / MQTT / Pulsar | `<broker>_to_database_asset` + `<broker>_monitor` + `<broker>_observation_sensor` |
| MongoDB / Cassandra / Neo4j / Elasticsearch | `<db>_resource` + `<db>_reader` + `<db>_writer` |
| Iceberg / Delta read+write | `iceberg_ingestion` + `dataframe_to_iceberg_table` (or delta_*) |
| Sync external table into the catalog (declare-only) | `external_<vendor>_table` (Snowflake / BigQuery / Iceberg / Delta / Kafka / S3 / GCS / Kinesis / Pub/Sub / SharePoint / …) |
| Pandas profile / pct change / top-N per group | `dataframe_describe` / `pct_change` / `top_n_per_group` |
| Filter / summarize / pivot / unpivot / join / dedup | `filter` / `summarize` / `pivot` / `unpivot` / `dataframe_join` / `unique_dedup` |
| Templated SQL CTAS or inline read | `sql_transform` (Jinja2, auto-injects partition_key + run_id) |
| Materialize a Jupyter notebook as an asset | `jupyter_notebook_asset` (papermill) |
| Run a container as an asset | `docker_container_asset` |
| Push metrics to Prometheus / query Prometheus | `dataframe_to_prometheus` / `dataframe_from_prometheus` |
| Synthetic data for demos (orders / events / customers / etc.) | `synthetic_data_generator` (many `schema_type` values) |

When in doubt: `dagster-component search <keyword>` — almost always a hit.
