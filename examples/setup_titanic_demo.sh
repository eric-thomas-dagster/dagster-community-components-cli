#!/usr/bin/env bash
# Titanic demo — community components ingest → filter → summarize → CSV sink.
#
# Pulls a public Titanic CSV, filters to first-class passengers, summarizes
# survival rate by gender, writes the result to /tmp/survival_report.csv.
#
# Uses 4 community components from the registry, installed via the CLI.

set -euo pipefail

PROJECT_DIR="${1:-titanic-demo}"

echo ">>> Creating project at $PROJECT_DIR"
mkdir -p "$PROJECT_DIR" && cd "$PROJECT_DIR"

uv init --python 3.11 --no-readme >/dev/null 2>&1 || true
uv add -q "dagster>=1.10.0" "dagster-webserver" "pandas"

CLI="uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git \
       dagster-component"

echo ">>> Installing 4 community components"
$CLI add csv_file_ingestion --auto-install
$CLI add filter             --auto-install
$CLI add summarize          --auto-install
$CLI add dataframe_to_csv   --auto-install

echo ">>> Writing definitions.py"
cat > definitions.py <<'PY'
"""Titanic demo — community components ingest → filter → summarize → CSV sink.

Pulls public Titanic data, filters to first-class passengers, summarizes
survival rate by gender, writes the result to /tmp/survival_report.csv.

Loads each Component class via importlib (since the templates repo's
dagster_community_components PyPI package may not be installed yet).
Once `pip install dagster-community-components` is published, this loader
can be replaced with a one-line:
    from dagster_community_components import (
        CSVFileIngestionComponent, FilterComponent,
        SummarizeComponent, DataframeToCsvComponent,
    )

Components inherit from Pydantic Model, so we instantiate with model_validate
which honors field defaults (direct kwargs on the @dataclass-decorated class
require every optional field to be passed explicitly).
"""
import importlib.util
from pathlib import Path

import dagster as dg


def _load(category: str, component_id: str, class_name: str):
    here = Path(__file__).parent
    path = here / "components" / "assets" / category / component_id / "component.py"
    spec = importlib.util.spec_from_file_location(f"_dcc_{component_id}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


CSVFileIngestion = _load("ingestion",  "csv_file_ingestion", "CSVFileIngestionComponent")
Filter           = _load("transforms", "filter",             "FilterComponent")
Summarize        = _load("transforms", "summarize",          "SummarizeComponent")
DataframeToCsv   = _load("sinks",      "dataframe_to_csv",   "DataframeToCsvComponent")


# 1. Ingest — public Titanic CSV from GitHub raw, no auth needed
ingest = CSVFileIngestion.model_validate({
    "asset_name": "titanic_raw",
    "file_path": "https://raw.githubusercontent.com/datasciencedojo/datasets/master/titanic.csv",
    "description": "Public Titanic passenger data",
    "group_name": "ingest",
})

# 2. Filter — first-class passengers only
first_class = Filter.model_validate({
    "asset_name": "first_class_passengers",
    "upstream_asset_key": "titanic_raw",
    "condition": "Pclass == 1",
    "group_name": "transform",
})

# 3. Summarize — survival rate + count by sex
survival_summary = Summarize.model_validate({
    "asset_name": "survival_by_sex",
    "upstream_asset_key": "first_class_passengers",
    "group_by": ["Sex"],
    "aggregations": {
        "Survived":    "mean",   # mean of 0/1 == survival rate
        "PassengerId": "count",  # group size
    },
    "group_name": "transform",
})

# 4. Sink — write the summary to local CSV
write_csv = DataframeToCsv.model_validate({
    "asset_name": "survival_report",
    "upstream_asset_key": "survival_by_sex",
    "file_path": "/tmp/survival_report.csv",
    "group_name": "sink",
})

defs = dg.Definitions.merge(
    ingest.build_defs(None),
    first_class.build_defs(None),
    survival_summary.build_defs(None),
    write_csv.build_defs(None),
)
PY

if ! grep -q "\[tool.dagster\]" pyproject.toml 2>/dev/null; then
  cat >> pyproject.toml <<'TOML'

[tool.dagster]
module_name = "definitions"
TOML
fi

cat <<MSG

>>> Setup complete.

Run the pipeline:
    cd $PROJECT_DIR
    uv run dagster dev

Then in the Dagster UI (http://localhost:3000):
  - Navigate to Assets
  - Right-click 'survival_report' → 'Materialize all upstream'
  - Output lands at /tmp/survival_report.csv

Expected output:
    Sex,Survived,PassengerId
    female,0.968,94
    male,0.369,122

(96.8% female survival rate, 36.9% male survival rate, in 1st class.)
MSG
