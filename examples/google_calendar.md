# Google Calendar — events to CSV + BigQuery

**Validated end-to-end against a real Google Calendar.** RUN_SUCCESS pulling
60 events from `ethomasii@gmail.com`, lands them in both a local CSV and a
BigQuery table. Both sinks demonstrated so the same pipeline works on
local dev and on Dagster+ Cloud.

```
upcoming_events     ← google_calendar_ingestion (Calendar API)
       │
       ├── upcoming_events_csv  ← dataframe_to_csv (/tmp/calendar_events.csv — local dev)
       └── upcoming_events_bq   ← dataframe_to_bigquery (cloud-friendly: a real BQ table)
```

## Components covered (3)

| Component | What it does |
|---|---|
| `google_calendar_ingestion` | Native service-account-authed Calendar reader. One row per event with summary, time, location, organizer, attendees, html_link. |
| `dataframe_to_csv` | Local CSV sink. Good for dev; not durable on Dagster+ Cloud (per-run executor filesystem). |
| `dataframe_to_bigquery` | Cloud-friendly sink — lands rows in a BQ table with WRITE_TRUNCATE / WRITE_APPEND options. |

## Validation status — all live

- 60 events fetched from `ethomasii@gmail.com` next 30 days.
- 60 rows in `/tmp/calendar_events.csv`.
- 60 rows in BigQuery table `servicepulse-490502.dagster_demo.calendar_events`.
  Verified post-run via `bq query` / `client.get_table().num_rows`.

## Cost

**$0.** Calendar API free up to 1M req/day. BigQuery loads of <60 rows
are well within the free tier.

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
export GOOGLE_CALENDAR_ID=you@gmail.com   # owner's email or named cal ID
```

## Setup

1. **Enable Calendar API + BigQuery API** on the SA's project. Components surface activation URLs on first call.
2. **Share the calendar with the SA email** (the `client_email` in your JSON):
   - Google Calendar → calendar's three-dot menu → Settings & sharing
   - Share with specific people or groups → paste SA email → "See all event details" → Save
3. **(Optional) BQ sink target**: by default writes to `servicepulse-490502.dagster_demo.calendar_events`. Override with `BQ_TARGET_TABLE=my-project.my_dataset.my_table` before running.

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_google_calendar_demo.sh | bash
cd google-calendar-demo
uv run dg launch --assets '*'
```

Inspect:

```bash
# Local CSV
cat /tmp/calendar_events.csv | head

# Cloud BQ table
bq query --nouse_legacy_sql 'SELECT COUNT(*) FROM dagster_demo.calendar_events'
```

## Local vs. Dagster+ Cloud sinks

`dataframe_to_csv` writes to the local filesystem of whoever's running
the materialization. On Dagster+ Cloud that's an ephemeral executor
container — the file is gone the moment the run ends, and you can't
download it. For cloud-deployed pipelines, use one of:

- **`dataframe_to_bigquery`** — lands a BQ table. Cleanest if you want
  SQL access to the result.
- **`dataframe_to_gcs`** — writes a `gs://bucket/path` object. Cleanest
  if you want a file-shaped output.
- **`dataframe_to_s3`** / **`dataframe_to_adls`** — same shape, AWS / Azure.
- **`dataframe_to_database`** — generic SQLAlchemy sink (Postgres, MySQL, Snowflake).

This demo wires up both `dataframe_to_csv` AND `dataframe_to_bigquery`
in parallel so you can see both shapes; drop the local one before
deploying to cloud.
