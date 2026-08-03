# Jupyter notebooks as Dagster assets — papermill, no auth

Materialize an analysis `.ipynb` as a first-class Dagster asset. `papermill` runs the notebook out-of-process, injects parameters into a tagged parameters cell, and stores the fully-executed `.ipynb` (with cell outputs) next to the input as evidence. Pure local — no SaaS, no cloud, no auth.

## Components used

| Component | Source | Role |
|---|---|---|
| `synthetic_data_generator` | community | Seeds an upstream pandas DataFrame |
| `jupyter_notebook_asset` | community | Runs a `.ipynb` via papermill, saves the executed notebook |

## Architecture

```
   ┌──────────────────────────┐
   │ synthetic_orders         │
   │ (generator → df)         │
   └────────────┬─────────────┘
                │
                ▼
   ┌──────────────────────────┐
   │ daily_revenue_report     │
   │ papermill executes       │
   │ notebooks/revenue_       │
   │ report.ipynb with        │
   │ injected parameters      │
   │ → revenue_report_        │
   │   executed_YYYYMMDD.ipynb│
   └──────────────────────────┘
```

## Run

```bash
bash setup_notebooks_demo.sh
cd notebooks-demo

uv run dg check defs
uv run dg launch --assets '*'
```

Expect `RUN_SUCCESS`. Then:

```bash
ls notebooks/
# revenue_report.ipynb                       (input)
# revenue_report_executed_20260514.ipynb     (papermill output, dated)
```

The executed notebook is full of outputs (printed strings, dataframes, plots) — open it in Jupyter / VS Code to inspect what the asset materialization actually did.

## How parameters flow

`papermill` injects YAML-supplied parameters into the cell tagged `parameters`. The notebook has:

```python
# Cell with tag "parameters"
start_date = '2024-01-01'
end_date = '2024-01-31'
```

…and the component config supplies:

```yaml
parameters:
  start_date: '2024-01-01'
  end_date: '2024-01-31'
```

Papermill inserts a new cell *after* the parameters cell with the injected values, so the notebook re-runs against the supplied dates without code changes.

For partitioned materializations, the partition_key is automatically passed as a parameter — point the notebook at it directly:

```yaml
parameters:
  partition_date: '{partition_key}'   # filled in at materialization time
partition_type: daily
partition_start: '2024-01-01'
```

## Why papermill, not dagstermill

Both wrap Jupyter notebooks as assets, but they diverge:

| | `jupyter_notebook_asset` (papermill) | `dagstermill_notebook_asset` (dagstermill) |
|---|---|---|
| Runner | papermill out-of-process | dagstermill (in-process under Dagster) |
| Access to `context` | No — pure Python kernel | Yes — `context.log`, resources, yield-able assets |
| Stores executed notebook | Yes (auditable artifact) | Yes (via output_notebook_io_manager) |
| Coexists with the other in one project | **No** — dagstermill globally overrides papermill's Python translator on import |

For most analysis use cases (reports, dashboards, charts), `jupyter_notebook_asset` is the right choice. Pick `dagstermill_notebook_asset` when the notebook needs Dagster's `context.resources` or wants to yield multiple assets.

## Trade-offs & gotchas

- **Kernels.** The kernel named in YAML (`kernel_name: python3`) must exist on the executor. The setup script installs the local kernel as part of the demo.
- **Notebook size.** Papermill stores the entire executed notebook including outputs — large plots / DataFrames inflate the file. Set `store_output_ipynb: false` to skip persistence (papermill still writes intermediate state).
- **Cell-level error reporting.** When a cell fails, the component surfaces cell number + error name + value, but you still need the executed `.ipynb` to see the full traceback. Always keep `store_output_ipynb: true` for debugging.

## See also

- [`composition_primitives.md`](composition_primitives.md) — small jobs with no auth
- [`lakehouse_local.md`](lakehouse_local.md) — Iceberg + Delta roundtrip with no auth
