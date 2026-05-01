#!/usr/bin/env bash
# Palmer Penguins ML feature engineering demo.
#
# Pulls Palmer Penguins data, fills missing values, one-hot encodes
# categorical columns, standard-scales numeric features, writes the
# ML-ready feature matrix to /tmp/penguins_features.parquet.
#
# Demonstrates 5 community components — different category mix from the
# Titanic demo, focused on ML preprocessing instead of descriptive analytics:
#
#   csv_file_ingestion → imputation → one_hot_encoding → feature_scaler → dataframe_to_parquet

set -euo pipefail

PROJECT_DIR="${1:-penguins-demo}"

echo ">>> Creating project at $PROJECT_DIR"
mkdir -p "$PROJECT_DIR" && cd "$PROJECT_DIR"

uv init --python 3.11 --no-readme >/dev/null 2>&1 || true
uv add -q "dagster>=1.10.0" "dagster-webserver" "pandas" "pyarrow"

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 5 community components"
$CLI add csv_file_ingestion --auto-install
$CLI add imputation         --auto-install
$CLI add one_hot_encoding   --auto-install
$CLI add feature_scaler     --auto-install
$CLI add dataframe_to_parquet --auto-install

echo ">>> Writing definitions.py"
cat > definitions.py <<'PY'
"""Palmer Penguins ML preprocessing demo.

Pulls public Palmer Penguins data, fills missing numeric values with the
column mean, one-hot encodes categorical columns (species/island/sex),
standardizes the numeric measurements, and writes an ML-ready feature
matrix to /tmp/penguins_features.parquet.

Components from the registry:
    csv_file_ingestion → imputation → one_hot_encoding → feature_scaler → dataframe_to_parquet

Loads each Component class via importlib (since the source repo's
`dagster_component_templates` namespace is not on PyPI). For projects that
prefer pip install, replace the importlib block with:

    from dagster_community_components import (
        CSVFileIngestionComponent, ImputationComponent,
        OneHotEncodingComponent, FeatureScalerComponent,
        DataframeToParquetComponent,
    )

then `pip install dagster-community-components` and remove the loader.

Components inherit from Pydantic Model, so we instantiate with model_validate
which honors field defaults (the @dataclass init requires every optional
field passed explicitly).
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


CSVFileIngestion   = _load("ingestion",  "csv_file_ingestion",   "CSVFileIngestionComponent")
Imputation         = _load("transforms", "imputation",           "ImputationComponent")
OneHotEncoding     = _load("transforms", "one_hot_encoding",     "OneHotEncodingComponent")
FeatureScaler      = _load("transforms", "feature_scaler",       "FeatureScalerComponent")
DataframeToParquet = _load("sinks",      "dataframe_to_parquet", "DataframeToParquetComponent")


# 1. Ingest — Palmer Penguins from public seaborn-data GitHub raw URL
ingest = CSVFileIngestion.model_validate({
    "asset_name": "penguins_raw",
    "file_path": "https://raw.githubusercontent.com/mwaskom/seaborn-data/master/penguins.csv",
    "description": "Palmer Penguins dataset (no auth)",
    "group_name": "ingest",
})

# 2. Impute — fill NaN in numeric columns with the column mean
imputed = Imputation.model_validate({
    "asset_name": "penguins_imputed",
    "upstream_asset_key": "penguins_raw",
    "strategy": "mean",
    "columns": ["bill_length_mm", "bill_depth_mm", "flipper_length_mm", "body_mass_g"],
    "group_name": "feature_engineering",
})

# 3. One-hot encode — species (3 levels), island (3), sex (2)
encoded = OneHotEncoding.model_validate({
    "asset_name": "penguins_encoded",
    "upstream_asset_key": "penguins_imputed",
    "columns": ["species", "island", "sex"],
    "drop_first": True,         # avoid the dummy-variable trap
    "dummy_na": True,           # explicit NaN indicator (sex has some NaN)
    "dtype": "int",
    "group_name": "feature_engineering",
})

# 4. Scale — z-score standardize the numeric columns (mean=0, sd=1)
scaled = FeatureScaler.model_validate({
    "asset_name": "penguins_scaled",
    "upstream_asset_key": "penguins_encoded",
    "strategy": "standard",
    "columns": ["bill_length_mm", "bill_depth_mm", "flipper_length_mm", "body_mass_g"],
    "suffix": "",               # overwrite originals — keep frame compact
    "group_name": "feature_engineering",
})

# 5. Sink — write the feature matrix to parquet
write_parquet = DataframeToParquet.model_validate({
    "asset_name": "penguins_features",
    "upstream_asset_key": "penguins_scaled",
    "file_path": "/tmp/penguins_features.parquet",
    "compression": "snappy",
    "index": False,
    "group_name": "sink",
})

defs = dg.Definitions.merge(
    ingest.build_defs(None),
    imputed.build_defs(None),
    encoded.build_defs(None),
    scaled.build_defs(None),
    write_parquet.build_defs(None),
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
    uv run dagster asset materialize --select '*' -m definitions

Output lands at /tmp/penguins_features.parquet — ML-ready feature matrix
with imputed missing values, one-hot encoded categoricals, and z-score
scaled numeric measurements.

Inspect the output:
    uv run python -c "
    import pandas as pd
    df = pd.read_parquet('/tmp/penguins_features.parquet')
    print(f'Shape: {df.shape}')
    print(f'Columns: {list(df.columns)}')
    print(df.head())
    "

Expected: ~344 rows, columns include the standardized 4 measurements +
the one-hot encoded species_*, island_*, sex_* indicators.
MSG
