# Google Sheets ingestion — service-account auth end-to-end

**Validated end-to-end.** RUN_SUCCESS pulling 2 rows from a real
Google Sheet via service-account auth.

Pull rows from a Google Sheets spreadsheet using a service-account
JSON, materialize as a Dagster asset.

```
class_data_sheet     ← google_sheets_ingestion (Sheets API → DataFrame)
```

## Components used

| Component | What it does |
|---|---|
| `google_sheets_ingestion` | Service-account-authenticated Google Sheets reader. Pulls one or more named ranges / sheet tabs from a spreadsheet, returns a pandas DataFrame, optionally persists to a dlt destination (Snowflake, BigQuery, Postgres, etc.). |

## Validation status

- **`google_sheets_ingestion`: live** — `dg check defs` passes; service
  account auth flows; Sheets API call lands; and the actionable
  permission-denied path was verified end-to-end (the component
  surfaces a 403 with a clickable share-link rather than crashing).

## Cost

**$0.** Sheets API has a 300 req/min/project free tier.

## Run

1. Get a service-account JSON. See the
   [setup walkthrough](#service-account-setup) below if you don't
   have one.

2. **Share at least one spreadsheet with the service account.**
   This is the gotcha — service accounts can't see anything in
   Drive unless explicitly shared.
   - Open the sheet in your browser.
   - Click **Share** → paste the service-account email → **Viewer**.
   - The SA email is in the JSON's `client_email` field (looks like
     `<name>@<project>.iam.gserviceaccount.com`).

3. Copy the spreadsheet ID from the URL — the long segment between
   `/d/` and `/edit` in
   `https://docs.google.com/spreadsheets/d/<ID>/edit`.

## Run it

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
export GOOGLE_SHEET_ID=<your-sheet-id>      # optional, defaults to a public sample
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_google_sheets_demo.sh | bash
cd google-sheets-demo
uv run dg launch --assets '*'
```

If the SA hasn't been shared on the sheet, the asset materializes
empty and logs:

```
sheet 'Class Data': 403 PERMISSION_DENIED. Share the spreadsheet
with the service-account email (...) — open
https://docs.google.com/spreadsheets/d/<ID> → Share → paste the SA
email → Viewer.
```

This is intentional — the asset still materializes (with empty
output), so downstream-dependent jobs aren't bricked, and the log
contains the exact remediation URL.

## Service account setup

If you don't already have a service-account JSON:

1. Open the GCP project picker at <https://console.cloud.google.com/>
   and confirm you're on the right project (top-left dropdown).

2. **Enable the Sheets API.**
   <https://console.cloud.google.com/apis/library> →
   search "Google Sheets API" → **Enable**. (Add Drive API too if
   you'll use other components.)

3. **Create a service account.**
   <https://console.cloud.google.com/iam-admin/serviceaccounts> →
   **+ CREATE SERVICE ACCOUNT** → name it `dagster-demo` →
   **Create and Continue** → skip role grant (Sheets uses per-file
   sharing, not IAM) → **Done**.

4. **Create a JSON key.** Click the service account → **Keys** tab
   → **Add Key** → **Create new key** → **JSON** → it'll download.

5. **Share the sheet** with the SA email (step 2 above).

6. `export GOOGLE_APPLICATION_CREDENTIALS=/path/to/the/json`. Most
   Google SDKs read this env var by default.

## See also

<!-- TODO: link related walkthroughs -->
