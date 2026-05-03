# Detect Changes demo

Diff yesterday's customer snapshot against today's, classify each row as
**insert / update / delete / unchanged**.

```
csv (yesterday) ─┐
                 ├─→ detect_changes → CSV
csv (today)     ─┘
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Yesterday: C001-C004 |
| 2 | `csv_file_ingestion` | ingestion | Today: C001-C003, C005 (no C004); C003's plan_tier upgraded |
| 3 | `detect_changes` | transformation | Outer-merge + classify |
| 4 | `dataframe_to_csv` | sink | Write changeset |

## Configuration

```yaml
business_key_columns: [customer_id]
compare_columns: [plan_tier, country]
include_unchanged: true     # set false to drop unchanged rows
change_type_column: change_type
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_detect_changes_demo.sh | bash
cd detect-changes-demo && uv run dg launch --assets '*'
```

## Expected output

5 rows with the following `change_type` values:

| customer_id | change_type | reason |
|---|---|---|
| C001 | unchanged | identical in both snapshots |
| C002 | unchanged | identical in both snapshots |
| C003 | update | plan_tier free → pro |
| C004 | delete | only in yesterday |
| C005 | insert | only in today |

## Why it's useful

CDC-friendly. Pipe the output into `alter_row` to mark rows for a downstream
sink, into `summarize` for daily-changes dashboards, or into a Slack/email
digest of "what changed today".
