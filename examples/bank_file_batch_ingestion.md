# Bank file batch-ingestion — file-level visibility without a partition blowup

**Validated end-to-end**, twice: once hand-rolling the sensor, and again
after replacing it with a real registry component. Scaffolded via
`setup_bank_file_batch_ingestion_demo.sh`, run against a real `dg dev`
daemon, $0 cost — 100% local (moto's standalone S3 server, no Docker, no
real AWS credentials).

```
s3://bank-files/incoming/<bank>/*.enc  (moto-backed S3 -- Fernet-"encrypted")
       │
       ▼
bank_file_sensor             S3MonitorSensorComponent (REGISTRY component,
  partition_mode:             not hand-rolled), partition_mode:
  business_key_batch           business_key_batch -- every 20s: scan for
       │                      new objects, group by bank, one RunRequest
       │                      PER BANK (not per file) targeting (bank, today)
       ▼
bank_batch_staged            ONE callable (hand-rolled -- business logic,
  partitioned on              not a generic pattern): decrypt → transform →
  (bank, batch_day)            re-encrypt → append rows to DuckDB, stamped
       │                      with source_file + dagster_run_id
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

[`src/<pkg>/defs/bank_file_sensor/defs.yaml`](#) configures
[`S3MonitorSensorComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/sensors/s3_monitor/README.md)
in its `business_key_batch` partition mode — built into the registry
specifically so this pattern doesn't have to stay hand-rolled (see
"What's hand-rolled vs. a registry component" below). It groups each
tick's new objects by a business key (`business_key_template`, default
`{key_parts[0]}` — the path segment right after `prefix`) and emits one
`RunRequest` per key-with-new-objects-this-tick.

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

### The DuckDB single-writer gotcha (another real bug this demo caught)

The whole point of this pattern is that **multiple banks' runs legitimately
land in the same tick and get scheduled concurrently** by Dagster's
executor — and DuckDB allows exactly one writer on a file at a time, unlike
a real warehouse. Running this for real surfaced it directly: with 6 banks'
runs launched together, one would intermittently fail with
`IOException: Could not set lock on file "...bank_staging.duckdb"`.
`bank_batch_staged` retries the `duckdb.connect()` call with jittered
backoff (up to 10 attempts) rather than letting the run fail — purely a
demo-local-DuckDB concern; a real Snowflake/warehouse destination (see
"Swap in production destinations" below) doesn't have this limitation at
all, so the retry loop is not part of the pattern itself.

## File-level visibility: metadata + row lineage, not a bespoke audit table

Every tick's `RunRequest.run_config` lists **exactly the files that tick
found** — not the whole day's accumulated list. This is `S3MonitorSensorComponent`'s
own `business_key_batch` logic now (not hand-written here), but the shape
is worth seeing explicitly:

```python
run_requests.append(
    dg.RunRequest(
        run_key=f"{bucket_name}/{business_key}/{batch_value}-{tick_id}",
        partition_key=dg.MultiPartitionKey({"bank": business_key, "batch_day": batch_value}),
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

The setup script starts moto's standalone S3 server (`uvx --from
'moto[server]' moto_server`, no Docker, no real AWS) in the background —
deliberately left running after the script exits (same as this repo's
Minio-based `s3_pipeline` demo leaves its Docker container running), since
`dg dev`/`dg launch` need it available afterward. `AWS_ENDPOINT_URL` is
written to `.env` in the project root, which `dg dev` loads automatically.

Open `http://localhost:3000` — the sensor is `RUNNING` by default and picks
up the seeded files within ~20 seconds (one run per bank with new files, six
total on first tick). Seed more while it's running to see a **second**
materialization land on the *same* `(bank, day)` partition instead of a new
one:

```bash
uv run python seed_bank_files.py 2 5
```

Confirmed live during validation: the first tick produced exactly 6 runs
(one per bank, all `tick=1`), and the staging table showed `3 files / 24
rows / 1 run_id` per bank. A follow-up seed of 2 files × 5 rows for **every**
bank produced exactly 6 *more* runs — still only 6 distinct partition keys,
each now materialized twice — and the staging table went to `5 files / 34
rows / 2 run_ids` per bank. The partition count stayed at 6 throughout both
ticks.

Cleanup when you're done (stops the background moto server):

```bash
lsof -ti:5111 | xargs kill
```

### Headless / re-run recipe

Point `dg launch` at a prior run's exact file list to reprocess only those
files — this is the re-run recipe, not just a smoke test. File entries
match `S3MonitorSensorComponent`'s per-object config shape exactly
(`bucket`/`key`/`size`/`etag`/`last_modified`/`prefix`/`region`):

```bash
cd my-bank-demo
TODAY="$(date -u +%Y-%m-%d)"
uv run dg launch --assets bank_batch_staged \
    --partition "bank_alpha|$TODAY" \
    --config-json '{"ops": {"bank_batch_staged": {"config": {"files": [{"bucket": "bank-files", "key": "incoming/bank_alpha/<a-real-key-from-the-Runs-tab>.enc", "size": 0, "etag": "", "last_modified": "", "prefix": "incoming/", "region": ""}]}}}}'
```

(Only `bucket` and `key` actually matter for the re-run — the asset fetches
the file from S3 live; the other fields are carried through for parity with
the sensor's normal run_config shape but aren't read by the asset body.)

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

## Credit cost implications

Dagster+'s published pricing defines a credit as "the sum of asset
materializations and ops executed" — each materialized asset is 1 credit
and each executed op is 1 credit, **including** the op underlying a plain
`@asset`, so one single-op asset materialization costs 2 credits. This is
billed per materialization/op, not per run. Current published rates: Solo
$0.040/credit, Starter $0.035/credit, Pro custom; serverless compute is
billed separately at $0.010/min (Solo/Starter), no compute charge on
Hybrid. (Source: [dagster.io/pricing](https://dagster.io/pricing), checked
2026-11-25 — rates are subject to change.)

Using this README's own real-scale numbers (100 banks, ~100 files/bank
every 5 minutes, 288 ticks/day):

- **The rejected naive approach** — one dynamic partition per file, with
  decrypt/transform/re-encrypt as 3 separate ops (the "natural first
  instinct" this README opens by rejecting on partition-count grounds) —
  costs `num_files × (1 materialization + 3 ops)` = `num_files × 4` credits.
  At 100 banks × 100 files × 288 ticks/day = 2,880,000 files/day, that's
  **11,520,000 credits/day** — roughly $403,200/day at Starter rates.
  Untenable on cost grounds alone, well before the partition-count ceiling
  this README covers even becomes the binding constraint.
- **This demo's design** — one run per bank per tick with new files, a
  single op doing all three steps — costs `num_runs × (1 materialization +
  1 op)` = `num_runs × 2` credits. Worst case (every bank has new files on
  every tick, which real traffic patterns won't sustain continuously): 100
  banks × 288 ticks/day × 2 = **57,600 credits/day** (~$2,016/day at
  Starter rates). Real-world cost is typically lower, since not every bank
  necessarily has new files on every single 5-minute tick.
- **Net effect: roughly 200× fewer credits** (11,520,000 → 57,600 in the
  worst case), from two design choices compounding — batching N files into
  one run (fewer materializations) *and* collapsing 3 ops into 1 (fewer
  op-credits per run) — on top of, not instead of, the partition-count fix
  this README already covers.

## What's hand-rolled vs. a registry component

**Updated.** This demo originally hand-rolled the sensor with raw
`@dg.sensor` + `MultiPartitionKey`, because nothing in the registry
generalized "group new files by a business-key dimension, batch under a
time window, one run per `(key, window)` with that tick's exact file list"
— `s3_monitor` only supported plain `run_config` (one `RunRequest` per
file, no partitioning) or `dynamic_partition` mode (one dynamic partition
**per file**, exactly the anti-pattern this demo avoids). Rather than ship
an example that hand-rolls something a registry component should own,
`s3_monitor` gained a real `business_key_batch` partition mode (see its
[README](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/sensors/s3_monitor/README.md))
that does exactly this, and `bank_file_sensor` now configures it via plain
YAML — zero custom sensor code.

**What still stays hand-rolled, and why**: `bank_batch_staged` — the
decrypt → transform → re-encrypt → stage asset body. That's inherently
business-specific logic (your encryption scheme, your transform rules,
your destination schema), not a generic pattern a registry component
should own. The sensor (a generic "batch by business key" shape) and the
business asset (inherently custom) are different things, and only the
former makes sense as a reusable component.

## Swap in production destinations

The pattern (sensor shape, partition shape, lineage columns) doesn't change
going from moto + DuckDB to real S3 + Snowflake — only the endpoint and the
destination do:

- **Source**: this demo already uses the production component
  (`S3MonitorSensorComponent`, `partition_mode: business_key_batch`) — the
  swap to real AWS is just dropping `AWS_ENDPOINT_URL`/the fake
  credentials and pointing `bucket_name` at a real bucket. No component or
  YAML shape change.
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
| `bank_file_sensor` | registry (`S3MonitorSensorComponent`, `partition_mode: business_key_batch`) | Groups new objects by bank, emits one `RunRequest` per bank targeting `(bank, today)` — configured via plain YAML, zero custom sensor code |
| `bank_batch_staged` | hand-rolled in this demo | Decrypt → transform → re-encrypt → stage, one callable, partitioned on `(bank, batch_day)` — inherently business-specific, not a candidate for a generic component |
| `dataframe_to_snowflake` | registry | Production swap-in for the local DuckDB staging step — see above |
