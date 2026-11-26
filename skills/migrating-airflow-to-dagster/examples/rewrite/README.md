# Worked example: the rewrite route on two DAGs

The rewrite route (`scripts/dag_spec.py`, `scripts/rewrite_prompt.py`, `scripts/accept_rewrite.py`) next to the converter, on two real DAGs extracted with Airflow 2.10.5:

| DAG | Converter | Why this one |
|---|---|---|
| `simple_sequential_pipeline` (`airflow_src/simple_sequential.py`) | converts cleanly (three assets, no caveat) | the case the converter handles well |
| `partner_ingest` (`airflow_src/partner_ingest.py`) | converts the shape but **stubs two tasks**: the `HttpSensor` (its `response_check` function is refused) and the fetch task (its callable uses `BaseHook`, an Airflow import); both raise `AIRFLOW-CAVEAT` when run | the case it stubs |

Files: `ir.json`, `report.json`, `manifest.json` (the converter's run); `specs/<dag>.spec.{md,json}` (facts only), `specs/<dag>.prompt.md` (the generated prompt), `specs/<dag>.gates.json` (the gate results); `converter_output/` (the converter's modules and tests, the reference draft); `project/` (a Dagster project with the two rewrites in `src/rw_pkg/defs/`, `dg check defs` passes); `manifest.after_rewrite.json` (every unit of both DAGs `route: rewrite`, `complete`); `parity_airflow.json` (Airflow's own outcome and values for the first DAG); `check_partner_ingest.py`.

**I wrote both rewrites myself by following the generated prompts** (searching the registry with `dagster-component search --json` and reading `dagster-component schema <id> --format json`, as the prompt says), then ran `accept_rewrite.py`. One rewrite each, plus fixes to the gates and the spec generator described below.

## `simple_sequential_pipeline`

| | Converter (task granularity) | Rewrite |
|---|---|---|
| structure | 3 assets (`fetch_data`, `process_data`, `save_results`), values passed through the IO manager, 2 structural checks | **1 asset** `simple_sequential_pipeline/processed_summary` (the three steps are plain functions inside it), 1 structural check |
| size | 100 lines, importing a shared 1,561-line `runtime.py` | 52 lines, imports only `dagster` |
| retries | `RetryPolicy(3, 300)` on each asset | the same policy once, on the asset; a failed step restarts from the start of the asset (stated in the header) |
| registry | none used | searched (`multi-step transform pipeline`): the `*_pipeline` components take dataframes or warehouse tables, these steps are list arithmetic, so none fits (stated in the header) |
| gates | converter's generated tests: pass | **all 9 pass** (the table below lists the first seven; `task_states` is `not run` without a baseline and `decision_record` covers retries and xcom): `dg check defs`, generated pytest (3), asset check present, parity with Airflow (overall outcome and the value of `save_results`, `'Saved 5 items from api'`), no secrets, no invented APIs, every task accounted for |

Idioms: the rewrite is what the converter's `--granularity product` would aim at but cannot reach here (its steps have retries, so the converter keeps them separate); it is smaller and has no runtime dependency. It lost nothing the spec lists as must-be-preserved; it lost per-task run records, which the spec lists as acceptable.

## `partner_ingest`

| | Converter | Rewrite |
|---|---|---|
| structure | 5 assets, **2 loud stubs** (`wait_for_export`, `fetch_export`): the DAG fails at the first of them when it runs | **2 assets** (`partner_export_rows` is the external boundary with its own retry policy, `customer_summary` does reshape and publish) + 1 structural check; the sensor's wait is a loop at the start of the first asset, the `rm -rf` cleanup task is folded into the second |
| size | 132 lines + the shared runtime | 88 lines, imports `dagster` and `requests` |
| registry | n/a | `http_poll_sensor` (tier live) starts a run when a response slice *changes*: it does not wait for `status == "ready"` inside a run; `rest_api_ingestion` (tier live) takes a literal `api_url`, but the host comes from the Airflow connection; neither fits, so the header says so and uses `requests` |
| secrets | stubbed | connection `partner_api` by name: `PARTNER_API_BASE_URL`, `PARTNER_API_PASSWORD` |
| gates | stubs are not translated (the units stay deferred) | **7 of 7 pass**; parity is `not run (no Airflow baseline)`: the DAG calls an external API, so it has none |
| behaviour check | none possible | `check_partner_ingest.py` ran the rewrite against a **local stand-in** for the partner API: it polled once while `pending`, fetched the rows and produced the same summary the Airflow DAG's `reshape` code gives for the same rows. Not the real partner. |

## What the spec missed, and what was fixed (for the class, not the DAG)

1. The sensor's `response_check` logic was not in the spec: the IR recorded only that the argument held a function. `airflow_ir.py` now records the source of such functions (the user's, not the library's) and `dag_spec.py` prints it.
2. A connection read inside a callable (`BaseHook.get_connection("partner_api")`) was not listed as a connection; `dag_spec.py` now finds `get_connection("...")` calls (names only).
3. Gate bugs the first run exposed (not rewrite defects): the asset-check gate demanded as many checks as the task-per-asset converter derived (a rewrite folds steps, so it now requires at least one when the converter derived any); the parity gate compared the value of *folded* tasks against the asset (only tasks the header says are `replaced by` an asset are compared); and the example project needed the standard `definitions.py` and `[tool.dagster]` settings for `dg check defs` to load it.

## Honest comparison

The rewrite is smaller and more idiomatic in both cases (one asset per data product, steps inside, glue folded, no runtime library), and it covers the DAG the converter could only stub. Cost: model effort per DAG (the prompts are 14 KB and 16 KB; I read each, ran several registry searches and wrote the file), where the converter takes under a second. The gates found gate bugs, not rewrite bugs, on these two; two DAGs say nothing about a typical estate. The converter's tests were the more mechanical check: for `partner_ingest` the rewrite's only behavioural evidence is a local stand-in.
