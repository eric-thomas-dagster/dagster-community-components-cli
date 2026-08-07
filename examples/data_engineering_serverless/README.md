# data_engineering_serverless — real data pipeline, one code location, zero API keys

**A straight data-engineering demo bundled as a single Dagster+ Serverless code location.** No LLMs, no vended-product API keys, no auth. Fetches from the public Hacker News API, transforms with pandas, sinks to CSVs + DuckDB. Everything meaningful in one `definitions.py`.

## What's in this project

```
data_engineering_serverless/
├── src/data_engineering/
│   ├── __init__.py                  (empty)
│   └── definitions.py               (5 assets, ~200 lines)
├── pyproject.toml                   (deps: dagster, dagster-cloud, pandas, requests, duckdb)
├── dagster_cloud.yaml               (4-line location manifest)
└── README.md                        (this)
```

Three files that matter + your code. Deploys to Dagster+ Serverless with one command.

## Pipeline shape

```
hn_top_story_ids ─► hn_stories ─┬─► hn_leaderboard    (top 20 by score → CSV)
                                 ├─► hn_domains        (top 20 domains → CSV)
                                 └─► hn_warehouse      (DuckDB fact table)
```

- **`hn_top_story_ids`** — fetches the current top-500 story IDs from `hacker-news.firebaseio.com` (public, no auth).
- **`hn_stories`** — fetches full metadata for the top 100 stories.
- **`hn_leaderboard`** — sorts by score, writes top 20 to `out/hn_leaderboard.csv`.
- **`hn_domains`** — extracts domain from URL, aggregates story counts + median score per domain, writes top 20 to `out/hn_domains.csv`.
- **`hn_warehouse`** — persists the 100-row fact table to a local DuckDB at `out/hn.duckdb`. Swap this for a Snowflake/BigQuery `table_sinks` component in production.

Every asset carries rich materialization metadata: row counts, fetch latency, top-scorer preview rendered inline (via `MetadataValue.md`), timestamps, sample rows. Nothing that requires a vendored service — just typed metadata that Dagster+ renders in the asset catalog.

## Local run

```bash
uv venv --python 3.12
uv pip install -e . dagster-webserver dagster-dg-cli
uv run dg dev                                # UI at http://localhost:3000
# or headless:
uv run dg launch --assets '*'
```

Materializes in ~20 seconds (dominated by the 100 HN item fetches over HTTP).

Outputs land in `out/`:
- `hn_leaderboard.csv` — top 20 stories by score
- `hn_domains.csv` — top 20 domains
- `hn.duckdb` — DuckDB with `fact_hn_stories` table (100 rows)

## Deploy to Dagster+ Serverless

**One-time**:

```bash
uvx --from dagster-cloud-cli dagster-cloud config setup     # if you haven't already
```

**Deploy**:

```bash
uvx --with pex --from dagster-cloud-cli dagster-cloud serverless deploy-python-executable . \
    --location-name data-engineering \
    --module-name data_engineering.definitions \
    --python-version 3.12
```

That's it. ~2 minutes end-to-end (pex build + upload + agent sync). No env vars, no location config, no vended-product accounts.

## Why this vs. the same thing in Prefect

Same pipeline in Prefect Managed would be one `.py` with `@flow` + `@task` decorators + `prefect deploy --from user/repo`. Same file count, same deploy command.

What Dagster+ gives you that Prefect Managed doesn't for this exact shape:

- **Every step is a browsable asset** — click `hn_leaderboard` in the catalog. See every prior materialization with the top-20 CSV rendered inline via `MetadataValue.md`. No log-grepping.
- **Rich typed metadata** — row counts, fetch latency, path to sink files, sample rows. Renders in the UI without extra code.
- **Rerun any single asset** — need to rebuild just `hn_domains` because you tweaked the aggregation? One click; upstream (`hn_stories`) not re-fetched.
- **Asset lineage graph** — Dagster+ shows the fetch → transform → sink shape as a graph. Prefect shows flows and tasks; less clean cross-asset lineage.
- **Partitions when you need them** — this demo is unpartitioned but adding a `daily` partition to a warehouse-backed variant is a `post_processing:` YAML block or a `partitions_def` kwarg. Time-travel to any day's materialization.
- **Insights on the numeric metadata** — promote `fetch_latency_ms` or `n_stories` to a Dagster+ Insights metric via UI clicks. Alert if fetch latency spikes.

None of these require a paid third-party API. All of them come from the assets model + typed metadata + the platform.

## Verified

- **Local**: `dg check defs` + `dg launch --assets '*'` → RUN_SUCCESS in ~20s. All 5 assets materialized. CSVs contain real HN top-story data. DuckDB fact table has 100 rows.
- **Serverless**: (deploy status pending — see the parent walkthrough for the deploy log)

## Related

- **[serverless_minimal/](../serverless_minimal/)** — the absolute-floor Serverless example (2 assets, no external services). Simpler shape.
- **[agentic_tour_serverless/](../agentic_tour_serverless/)** — same 3-file layout, but the pipeline logic makes LLM calls via `AgenticPipelineComponent` (requires `OPENAI_API_KEY`). Complementary "richer example" for AI workloads.
- **[single_file_serverless.md](../single_file_serverless.md)** — the meta walkthrough that indexes all Serverless examples + documents the `dg_deploy_one_file.sh` CLI wrapper.
- **[prefect_vs_dagster_single_file.md](../prefect_vs_dagster_single_file.md)** — honest side-by-side of the single-file story across both platforms.
