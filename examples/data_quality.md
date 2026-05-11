# Data quality — 4 asset_check components on a synthetic orders asset

**Validated end-to-end** — `dg check` passes with 4 asset_check
components attached to a synthetic `orders` source. Pure local, $0 cost.

```
orders (synthetic source: 30 rows, 4 cols)
       │
       ├── pandas_dataframe_check        → required columns + dtype contract
       ├── pandera_asset_check           → declarative schema (OrdersSchema)
       ├── enhanced_data_quality_checks  → row_count + null_check selection
       └── freshness_check               → time_window policy (25h fail / 13h warn)
```

## Components covered (4)

| Component | What it checks |
|---|---|
| [`pandas_dataframe_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/pandas_dataframe_check) | Asserts the asset's DataFrame has the listed columns with the listed dtypes |
| [`pandera_asset_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/pandera_asset_check) | Defers to a [pandera](https://pandera.readthedocs.io/) `DataFrameModel` for full row-level validation (range, isin, regex, etc.) |
| [`enhanced_data_quality_checks`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/enhanced_data_quality_checks) | Multi-asset selection-based: applies row count + null checks to all matching assets |
| [`freshness_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/freshness_check) | Time-window or cron freshness policy — flags assets that haven't materialized recently |

## Cost

**$0.** All checks run locally on the materialized DataFrame.

## Run it

```bash
./setup_data_quality_demo.sh
cd data-quality-demo

# Materialize the orders source
uv run dg launch --assets orders

# Run all attached asset checks
uv run dg launch --asset-checks '*'
```

Or open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

Each check shows up in the asset graph as an "Asset Check" hanging off
the `orders` asset. Pass / fail / warn status is visible in the run UI.

## Skipped components (need extra setup)

The asset_checks family has 11 components total. This demo exercises 4
local-runnable ones. The remaining 7 need extra dependencies or SaaS
credentials:

- **[`great_expectations_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/great_expectations_check)** — install `great_expectations` first
  (heavy dependency); needs a configured GE context + expectation suite.
- **[`acceldata_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/acceldata_check)** — needs Acceldata credentials.
- **[`monte_carlo_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/monte_carlo_check)** — needs Monte Carlo Data API credentials.
- **[`sifflet_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/sifflet_check)** — needs Sifflet API credentials.
- **[`soda_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/soda_check)** — needs a Soda Cloud / SodaCL configuration.
- **[`ocsf_validator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/ocsf_validator)** — security-specific (OCSF schema validation);
  worth its own demo paired with [`siem_event_normalizer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/transforms/siem_event_normalizer).
- **[`openlineage_emitter`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/openlineage_emitter)** — emits an OpenLineage event per check run
  (related to but not the same as [`openlineage_export_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/openlineage_export_job)).

## Pandera schema referenced by the demo

```python
# src/<pkg>/schemas/__init__.py
import pandera as pa
from pandera.typing import Series

class OrdersSchema(pa.DataFrameModel):
    order_id: Series[int] = pa.Field(ge=1000)
    customer_id: Series[int] = pa.Field(ge=1, le=100)
    amount: Series[float] = pa.Field(gt=0)
    status: Series[str] = pa.Field(isin=["pending", "shipped", "delivered", "cancelled"])
```

[`pandera_asset_check`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/asset_checks/pandera_asset_check) references this by `schema_module: <pkg>.schemas`
+ `schema_name: OrdersSchema`. Pandera does the row-level validation on
every materialization.
