#!/usr/bin/env bash
# Regenerate ir.json (the IR airflow_ir.py extracts from dags/cosmos_shop.py) with Airflow 2.10.5 and astronomer-cosmos.
#   AIRFLOW_PY   a Python with apache-airflow==2.10.5 (use its constraints file) and astronomer-cosmos installed
#   DBT_BIN      a dbt that has dbt-duckdb (Cosmos runs `dbt ls` to render the tasks; dbt may live in a separate virtualenv)
# Absolute paths in the output are rewritten to /opt/airflow so the committed file does not depend on the machine.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
DBT_EXECUTABLE="${DBT_BIN:?}" "${AIRFLOW_PY:?}" "$here/../../airflow_ir.py" "$here/dags" --out "$here/ir.raw.json"
python3 - "$here" <<'PY'
import sys, pathlib
here = pathlib.Path(sys.argv[1]); raw = (here / "ir.raw.json").read_text()
raw = raw.replace(str(here), "/opt/airflow")
import re, os
raw = re.sub(r'"dbt_executable_path": "[^"]*"', '"dbt_executable_path": "/opt/dbt-venv/bin/dbt"', raw)
(here / "ir.json").write_text(raw)
(here / "ir.raw.json").unlink()
PY
echo "wrote $here/ir.json"
