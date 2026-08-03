# Per-File Processor Job demo

Inbox-style fan-out: list files in a directory matching a glob, process each
in parallel, archive the originals on success. The demo runs in `local`
storage mode — no S3 / GCS / ADLS creds — but the same component handles all
four backends.

```
@dg.job
  ├─ _discover (DynamicOut)        ← list files matching pattern
  ├─ _process[file_1] (parallel)
  ├─ _process[file_2] ...           ← user's parser per file; archive on success
  └─ _summary
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `per_file_processor_job` | infrastructure | DynamicOut + per-file callable + archive |

## Demo flow

The script writes 5 synthetic CSVs (`order_1.csv`..`order_5.csv`) into
`/tmp/per_file_demo/incoming/` then schedules a parser callable that totals
each file's `value` column.

```yaml
storage: local
local_directory: /tmp/per_file_demo/incoming
pattern: "*.csv"
process_callable_path: "<pkg>.file_callables:parse_csv"
archive_prefix: "archive"   # moves processed files to /tmp/per_file_demo/incoming/archive/
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_per_file_processor_demo.sh | bash
cd per-file-demo
uv run dg launch --job process_inbox_csvs
```

## Expected output

5 ops in parallel (`_process[order_1_csv]` through `_process[order_5_csv]`).
After success:

```
/tmp/per_file_demo/incoming/         (empty)
/tmp/per_file_demo/incoming/archive/
    order_1.csv  order_2.csv  order_3.csv  order_4.csv  order_5.csv
```

Each file processed independently — one bad CSV doesn't fail the others
(when `retry_max_retries` runs out the bad file fails its own op while siblings
succeed).

## Storage modes

`storage: local | s3 | gcs | adls`. Cloud modes need the corresponding
credentials and bucket/container config. See the component's
[example.yaml](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/jobs/per_file_processor_job/example.yaml)
for the cloud variants.

## See also

<!-- TODO: link related walkthroughs -->
