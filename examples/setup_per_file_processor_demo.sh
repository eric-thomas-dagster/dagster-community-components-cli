#!/usr/bin/env bash
# per_file_processor_job demo (local mode) — list synthetic CSVs in a tmp
# directory, parse each in parallel, archive on success.

set -euo pipefail
PROJECT_DIR="${1:-per-file-demo}"

echo ">>> Scaffolding"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas
uv add --dev -q dagster-dg-cli dagster-webserver

mkdir -p /tmp/per_file_demo/incoming /tmp/per_file_demo/archive
echo ">>> Writing 5 synthetic CSV files into the inbox"
for i in 1 2 3 4 5; do
  echo "id,name,value" > "/tmp/per_file_demo/incoming/order_$i.csv"
  for j in $(seq 1 4); do
    echo "$j,item_${i}_${j},$((100 * i + j))" >> "/tmp/per_file_demo/incoming/order_$i.csv"
  done
done
ls /tmp/per_file_demo/incoming/

cat > "src/$PKG/file_callables.py" <<'PY'
"""Per-file processor for the demo."""
import csv


def parse_csv(file_info: dict) -> dict:
    path = file_info["path"]
    with open(path) as f:
        rows = list(csv.DictReader(f))
    total = sum(int(r["value"]) for r in rows)
    return {
        "key": file_info["key"],
        "rows": len(rows),
        "total_value": total,
    }
PY

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing component"
$CLI add per_file_processor_job --auto-install

cat > "src/$PKG/defs/per_file_processor_job/defs.yaml" <<EOF
type: $PKG.components.per_file_processor_job.component.PerFileProcessorJobComponent
attributes:
  job_name: process_inbox_csvs
  schedule: "*/5 * * * *"
  default_status: STOPPED

  storage: local
  local_directory: /tmp/per_file_demo/incoming
  pattern: "*.csv"
  max_files_per_run: 50

  process_callable_path: "$PKG.file_callables:parse_csv"

  archive_prefix: "archive"

  retry_max_retries: 1
  fail_on_empty: false
EOF

cat <<MSG

>>> Setup complete.
Run once to test:
    cd $PROJECT_DIR && uv run dg launch --job process_inbox_csvs

Expected: 5 files processed in parallel, each parsed, then moved to
    /tmp/per_file_demo/incoming/archive/
MSG
