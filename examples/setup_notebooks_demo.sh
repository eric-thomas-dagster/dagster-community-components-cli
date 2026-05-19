#!/usr/bin/env bash
# Notebooks demo — execute a Jupyter notebook as a Dagster asset via
# papermill (jupyter_notebook_asset). No SaaS, no cloud, no auth — pure
# local notebook execution against an in-memory synthetic dataset.
#
# WHAT THIS DEMONSTRATES
#   How to materialize an analysis notebook as a first-class Dagster asset:
#     - papermill executes the notebook out-of-process
#     - parameters are injected into the tagged parameters cell
#     - the fully-executed `.ipynb` is saved next to the input as
#       evidence (auditable, downloadable from the catalog)
#
# Components exercised (2):
#   - synthetic_data_generator   (already validated — upstream)
#   - jupyter_notebook_asset     (papermill runner)
#
# Asset graph:
#   synthetic_orders (generator)
#     → daily_revenue_report (papermill notebook)
#
# Note on dagstermill: a sibling component (dagstermill_notebook_asset) wraps
# dagster's own dagstermill runner, but **dagstermill globally overrides
# papermill's Python translator on import** — so it can't coexist with
# jupyter_notebook_asset in the same code location. Validate dagstermill in
# a separate project if you want both runners.
#
# COST: \$0 — all local.

set -euo pipefail
PROJECT_DIR="${1:-notebooks-demo}"

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q pandas papermill ipykernel

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 2 components"
for c in synthetic_data_generator jupyter_notebook_asset; do
  $CLI add $c --auto-install
done

echo ">>> Writing the analysis notebook"
mkdir -p notebooks

python3 - <<'PY' > notebooks/revenue_report.ipynb
import json
nb = {
    "cells": [
        {
            "cell_type": "code",
            "metadata": {"tags": ["parameters"]},
            "source": ["start_date = '2024-01-01'\n", "end_date = '2024-01-31'\n"],
            "outputs": [],
            "execution_count": None,
        },
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "import pandas as pd\n",
                "df = pd.DataFrame({\n",
                "    'date': pd.date_range(start_date, end_date),\n",
                "    'revenue': range(31),\n",
                "})\n",
                "total = float(df['revenue'].sum())\n",
                "print(f'Total revenue from {start_date} to {end_date}: ${total:.2f}')\n",
                "total\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
    ],
    "metadata": {
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}
print(json.dumps(nb, indent=2))
PY

echo ">>> Registering the local kernel for papermill"
uv run python -m ipykernel install --user --name=python3 --display-name="Python 3" >/dev/null 2>&1 || true

echo ">>> Writing defs.yaml"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

PROJ_ABS="$(pwd)"

write_yaml "synthetic_data_generator" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: synthetic_orders
  schema_type: orders
  row_count: 100
  random_state: 42
  group_name: notebooks"

write_yaml "jupyter_notebook_asset" "type: $PKG.components.jupyter_notebook_asset.component.JupyterNotebookAssetComponent
attributes:
  asset_name: daily_revenue_report
  notebook_path: $PROJ_ABS/notebooks/revenue_report.ipynb
  parameters:
    start_date: '2024-01-01'
    end_date: '2024-01-31'
  execution_timeout: 120
  kernel_name: python3
  deps: [synthetic_orders]
  group_name: notebooks"

cat <<MSG

>>> Setup complete.

Validate the 2 components load:
    cd $PROJECT_DIR
    uv run dg check defs

Materialize the asset graph:
    uv run dg launch --assets '*'

Inspect the executed papermill notebook:
    ls notebooks/*_executed_*.ipynb

Browse it in the UI:
    uv run dg dev   # http://localhost:3000 → Assets graph

This demo never reaches outside the project directory — papermill runs
pure Python in a local kernel. Swap in your real notebook + parameters
(connecting to a warehouse, dbt model, etc.) without changing the
component config shape.
MSG
