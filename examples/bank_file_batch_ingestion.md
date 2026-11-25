# Bank file batch-ingestion — file-level visibility without a partition blowup

**Validated end-to-end.** Scaffolded via `setup_bank_file_batch_ingestion_demo.sh`,
run against a real `dg dev` daemon and `dg launch`, $0 cost — 100% local,
no cloud credentials.

```
incoming/<bank>/*.enc  (simulated SFTP/S3 drop, Fernet-"encrypted")
       │
       ▼
bank_file_sensor            every 20s: scan for files not yet seen,
                             group by bank, one RunRequest PER BANK
       │                    (not per file) targeting (bank, today)
       ▼
bank_batch_staged            ONE callable: decrypt → transform →
  partitioned on              re-encrypt → append rows to DuckDB,
  (bank, batch_day)            stamped with source_file + dagster_run_id
       │
       ▼
bank_staging.duckdb::bank_transactions_staging
  (stand-in for a real Snowflake staging table)
```

## The problem this solves

A natural first instinct for "100 banks drop ~100 files each every 5 minutes,
decrypt → transform → re-encrypt → stage into Snowflake, file-level
visibility, one op not three" is: use a file-arrival sensor
(this registry's [`s3_monitor`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/sensors/s3_monitor/README.md))
in `dynamic_partition` mode, registering each new file as its own Dagster
dynamic partition on one shared asset.

That breaks at this volume. Dagster's own docs recommend capping an asset at
**100,000 partitions** (a maintainer has said Dagster+ tolerates up to
~250,000) — a soft limit tied to UI responsiveness, not a hard wall, but real
slowdowns show up well before it
([dagster-io/dagster#17809](https://github.com/dagster-io/dagster/discussions/17809),
[partitioning-assets.md](https://github.com/dagster-io/dagster/blob/master/docs/docs/guides/build/partitions-and-backfills/partitioning-assets.md)).
At 100 banks × 100 files × 12 ticks/hour × 24 hours, a shared asset with one
dynamic partition per file would add **288,000 partitions in a single day**.
`s3_monitor` does have a `dynamic_partition_keep_days` pruning option, but
pruning only removes partitions *older* than the retention window — it
doesn't help same-day accumulation at this rate, and production-grade
re-runnability usually means keeping more than a few days of history anyway.

## The fix: partition on (bank, batch_day), not on the file

[`src/<pkg>/defs/bank_file_ingestion/__init__.py`](#) defines ONE shared
asset, `bank_batch_staged`, on a `MultiPartitionsDefinition` combining:

- **`bank`** — a `StaticPartitionsDefinition` (6 fake bank codes in this
  demo; ~100 real codes in production). This is the "one reusable pipeline
  across all banks, same destination" shape — a dimension of the partition
  key, not 100 separate assets.
- **`batch_day`** — a `DailyPartitionsDefinition`. Each `(bank, day)`
  partition gets **re-materialized once per sensor tick** that finds new
  files for that bank — Dagster has no cap on materialization *events* per
  partition, only on partition *keys*, so this is where file-level
  visibility actually lives (see below), not in the partition count.

### Why daily, not hourly

| Cadence | Partitions/year (100 banks) | Headroom |
|---|---|---|
| **Daily** (this demo) | 100 × 365 = **36,500** | ~7 years before the 250k ceiling |
| Hourly | 100 × 24 × 365 = **876,000** | blows past the limit in **under 4 months** |

Daily batching needs zero pruning maintenance — unlike dynamic partitions,
time-window partition keys aren't individually stored or registered, so
there's nothing to delete. (If you ever do want to retire old daily
partitions from view, that's done by advancing `start_date` forward, not by
an instance API call — there's no dynamic-partition-style prune hook for
time-window dimensions.)

### The `end_offset=1` gotcha (a real bug this demo caught)

```python
"batch_day": dg.DailyPartitionsDefinition(start_date="2025-01-01", end_offset=1),
```

Dagster's default (`end_offset=0`) means a daily partition set only ever
includes **completed** days — "today" isn't a valid partition key until
tomorrow. This demo (like the real use case) needs to re-materialize
*today's* partition intraday, so `end_offset=1` is required, not cosmetic.
Without it, the sensor's very first same-day `RunRequest` raises — this was
caught by actually running the demo, not just reasoning about the shape.

## File-level visibility: metadata + row lineage, not a bespoke audit table

Every tick's `RunRequest.run_config` lists **exactly the files that tick
found** — not the whole day's accumulated list:

```python
run_requests.append(
    dg.RunRequest(
        run_key=f"{bank}-{today}-{tick_id}",
        partition_key=dg.MultiPartitionKey({"bank": bank, "batch_day": today}),
        run_config={"ops": {"bank_batch_staged": {"config": {"files": files}}}},
    )
)
```

Dagster **persists that run_config with the run permanently.** So:

- Every materialization's metadata records `files_processed`, `file_count`,
  `row_count`, and `bytes_processed` for that specific tick — browsable per
  partition in the Dagster UI, no separate audit system.
- Every row written to the staging table is stamped with `bank`,
  `batch_day`, `source_file`, and `dagster_run_id` — so file-level lineage
  survives in the data itself, independent of how long you keep Dagster's
  own event log around.

## The re-run story

"What run ingested file X" is answerable today via the run's own persisted
`run_config` and tags — Dagster Plus's API (`list_runs` / `get_run_logs`,
filterable by partition or tag) already supports this. It's just not fast
for ad hoc search across millions of files, which is the actual gap an
external log/analytics sink closes — a search index, not a system of
record. The system of record stays Dagster's own run history.

Re-running is then a direct **re-execution of that specific run_id** — not
a partition backfill. It replays the exact file list that run processed,
nothing more. This demo's headless recipe below is that exact recipe:
point `--config-json` at the file list you want reprocessed and launch the
same partition again.

## Run it

```bash
curl -sL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_bank_file_batch_ingestion_demo.sh | bash -s my-bank-demo
cd my-bank-demo && uv run dg dev
```

Open `http://localhost:3000` — the sensor is `RUNNING` by default and picks
up the seeded files within ~20 seconds (one run per bank with new files, six
total on first tick). Seed more while it's running to see a **second**
materialization land on the *same* `(bank, day)` partition instead of a new
one:

```bash
uv run python seed_bank_files.py 2 5
```

Confirmed live during validation: a follow-up seed of 2 files for
`bank_alpha` alone produced exactly one new `RunRequest` (`bank_alpha=2`),
and the staging table showed the same partition going from 1 run_id / 3
files / 24 rows to 2 run_ids / 5 files / 26 rows — the partition count
stayed at 6 the entire time.

### Headless / re-run recipe

```bash
cd my-bank-demo
TODAY="$(date +%Y-%m-%d)"
FILE="$(ls incoming/bank_alpha/*.enc | head -1)"
uv run dg launch --assets bank_batch_staged \
    --partition "bank_alpha|$TODAY" \
    --config-json "{\"ops\": {\"bank_batch_staged\": {\"config\": {\"files\": [\"$FILE\"]}}}}"
```

Note the `MultiPartitionKey` string format: dimensions are joined with `|`
in **alphabetical order by dimension name** (`bank` before `batch_day`), so
it's `bank_alpha|2026-11-25`, not the other way around — confirmed against
Dagster's own `MultiPartitionKey.__new__` source
([multi.py](https://github.com/dagster-io/dagster/blob/master/python_modules/dagster/dagster/_core/definitions/partitions/utils/multi.py)),
not assumed. Also note `--config-json` (inline JSON), not `--config` (which
takes a *file path* in the currently installed `dg` CLI) — confirmed by
running `dg launch --help`, since a few docs/skills still show the older
inline-`--config` form.

Inspect what landed:

```bash
uv run python -c "import duckdb; print(duckdb.connect('data/bank_staging.duckdb').execute('select bank, batch_day, count(*) rows, count(distinct source_file) files, count(distinct dagster_run_id) runs from bank_transactions_staging group by 1,2 order by 1').fetchall())"
```

## What's hand-rolled vs. a registry component

Nothing in the registry today does "group new files by a business-key
dimension, batch under a time window, one run per `(key, window)` with that
tick's exact file list" — `s3_monitor` only supports plain `run_config`
(one `RunRequest` per file, no partitioning) or `dynamic_partition` mode
(one dynamic partition **per file**, exactly the pattern this demo avoids).
So `bank_file_sensor` and `bank_batch_staged` are hand-rolled with raw
`@dg.sensor` / `@dg.asset` / `MultiPartitionsDefinition` — not a wrapped
registry component. If this shape proves broadly useful, it's a reasonable
candidate for a future `s3_monitor` partition mode (e.g.
`partition_mode: business_key_batch`) rather than staying one-off — out of
scope for this demo, noted here honestly rather than implied otherwise.

## Swap in production destinations

The pattern (sensor shape, partition shape, lineage columns) doesn't change
going from local files + DuckDB to real S3 + Snowflake — only the I/O does:

- **Source**: swap the `incoming/<bank>/*.enc` directory scan for the same
  registry's `s3_monitor` in plain `run_config` mode (no partitioning on
  the sensor side — this demo's own sensor already does the per-bank
  grouping and partition targeting that `s3_monitor` doesn't).
- **Destination**: have `bank_batch_staged` return a DataFrame instead of
  writing DuckDB directly, then chain the registry's
  [`DataFrame to Snowflake`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/sinks/dataframe_to_snowflake/README.md)
  component downstream — it already has first-class support for this
  exact `(static × date)` partition shape:

  ```yaml
  type: dagster_community_components.DataframeToSnowflakeComponent
  attributes:
    asset_name: bank_transactions_staged_sf
    upstream_asset_key: bank_batch_staged
    table: bank_transactions_staging
    if_exists: append
    partition_type: multi
    partition_start: "2025-01-01"
    partition_static_dim: bank
    partition_static_column: bank
    partition_date_column: batch_day
    account_env_var: SNOWFLAKE_ACCOUNT
    user_env_var: SNOWFLAKE_USER
    password_env_var: SNOWFLAKE_PASSWORD
  ```

  Same partition math, same file-level visibility story, same re-run
  recipe — just a real warehouse underneath.

## Components used

| Component | Source | Role |
|---|---|---|
| `bank_file_sensor` | hand-rolled in this demo (not a registry component — see above) | Groups new files by bank, emits one `RunRequest` per bank targeting `(bank, today)` |
| `bank_batch_staged` | hand-rolled in this demo | Decrypt → transform → re-encrypt → stage, one callable, partitioned on `(bank, batch_day)` |
| `dataframe_to_snowflake` | registry | Production swap-in for the local DuckDB staging step — see above |
| `s3_monitor` | registry | Production swap-in for the local directory scan (plain `run_config` mode) |
