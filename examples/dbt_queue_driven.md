# Message-driven dbt orchestration

An external system publishes messages to a queue. Dagster picks them up via
a sensor. Each message says either **"build this model with these vars"**
or **"build all models"**. Dagster runs the right job, then publishes a
success message back out to the queue.

This walkthrough builds that end-to-end — no queue broker required. The
sensor generates random messages so the whole demo runs with just `dg dev`.

**Setup:**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_dbt_queue_driven_demo.sh \
  -o setup_dbt_queue_driven_demo.sh
bash setup_dbt_queue_driven_demo.sh
cd dbt_queue_demo
uv run dg dev            # → http://localhost:3000
```

Watch runs come in. Each run appends a line to `dbt_queue_demo/output_queue.jsonl`:

```bash
tail -f dbt_queue_demo/output_queue.jsonl
```

Requirements: [uv](https://docs.astral.sh/uv/). Cost: $0.

## The shape

```
                         ┌─────────────────────────┐
                         │  queue_sensor           │
                         │  (simulated input queue)│
                         │                         │
                         │  every ~30s:            │
                         │    80% single-model msg │
                         │    15% skip             │
                         │     5% run-all msg      │
                         └───────────┬─────────────┘
                                     │
                                     │ RunRequest(job=..., asset_selection=[…], run_config=…)
                                     │
                       ┌─────────────┴──────────────┐
                       ▼                            ▼
        ┌─────────────────────────┐    ┌─────────────────────────┐
        │  run_single_model_job   │    │  run_all_models_job     │
        │                         │    │                         │
        │  asset_selection is     │    │  no narrowing — every   │
        │  narrowed per-request:  │    │  dbt model materializes │
        │  [orders,               │    │                         │
        │   queue_completion]     │    │                         │
        └──────────┬──────────────┘    └────────────┬────────────┘
                   │                                │
                   │       ┌────────────────────────┘
                   ▼       ▼
        ┌────────────────────────────────────────────────────────┐
        │  dbt_op  (DbtProjectWithRuntimeVarsComponent subclass) │
        │                                                        │
        │  reads `vars` from op config, appends to dbt CLI:      │
        │    dbt build --vars '{"start_date": "2025-01-01"}'     │
        │              --select demo.orders                       │
        └──────────┬─────────────────────────┬───────────────────┘
                   ▼                         ▼
        ┌──────────────────┐    ┌────────────────────────────┐
        │  customers       │    │  orders                    │
        │  (dbt model)     │    │  (dbt model)               │
        │                  │    │                            │
        │                  │    │  WHERE order_date >=       │
        │                  │    │    '{{ var("start_date") }}'│
        └────────┬─────────┘    └────────────┬───────────────┘
                 │                           │
                 └──────────┬────────────────┘
                            ▼
                 ┌──────────────────────────┐
                 │  order_summary           │
                 │  (dbt model)             │
                 └──────────────┬───────────┘
                                │
                                │ (deps on all 3 dbt models — runs LAST)
                                ▼
                 ┌──────────────────────────┐
                 │  queue_completion        │
                 │  (@asset)                │
                 │                          │
                 │  appends success msg     │
                 │  to output_queue.jsonl   │
                 └──────────────────────────┘
```

## The four pieces

### 1. The custom dbt component (customizing the official integration)

The stock `dagster_dbt.DbtProjectComponent` accepts static `cli_args` and
partition-context templates — but not arbitrary per-run vars from a
sensor's `RunRequest`. So we subclass it (~15 lines):

```python
# src/<pkg>/lib/custom_dbt.py
import json
from typing import Any
import dagster as dg
from dagster_dbt import DbtCliResource, DbtProjectComponent


class DbtRunVars(dg.Config):
    vars: dict[str, Any] = {}


class DbtProjectWithRuntimeVarsComponent(DbtProjectComponent):
    @property
    def op_config_schema(self) -> type[dg.Config] | None:
        return DbtRunVars

    def get_cli_args(self, context: dg.AssetExecutionContext) -> list[str]:
        args = super().get_cli_args(context)
        run_vars = context.op_execution_context.op_config.get("vars") or {}
        if run_vars:
            args += ["--vars", json.dumps(run_vars)]
        return args
```

The component YAML then just references the subclass:

```yaml
# src/<pkg>/defs/dbt/defs.yaml
type: <pkg>.lib.custom_dbt.DbtProjectWithRuntimeVarsComponent
attributes:
  project: "{{ project_root }}/dbt_project"
  op:
    name: dbt_op        # explicit name so the sensor can address it in run_config
```

This same pattern extends to any dbt CLI flag — `--full-refresh`,
`--target`, `--threads`, `--exclude`, etc. Add a field to `DbtRunVars`,
append the flag in `get_cli_args`, and it's addressable per-run.

### 2. The queue-completion asset (success-only write-back)

```python
# src/<pkg>/defs/queue_completion.py
@dg.asset(
    deps=[
        dg.AssetKey("customers"),
        dg.AssetKey("orders"),
        dg.AssetKey("order_summary"),
    ],
    group_name="queue",
)
def queue_completion(context, config: CompletionConfig) -> dg.MaterializeResult:
    payload = {
        "run_id": context.run_id,
        "action": config.action,
        "model": config.model,
        "vars": config.vars,
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "success",
    }
    Path("output_queue.jsonl").open("a").write(json.dumps(payload) + "\n")
    return dg.MaterializeResult(metadata={"payload": dg.MetadataValue.json(payload)})
```

**Why an asset, not a hook:** the completion message IS materialized data —
it belongs on the asset graph. Every downstream consumer (audit dashboard,
Collibra lineage, another sensor) sees it via lineage. A hook would hide
it from the graph entirely.

**Success-only by design:** the asset has `deps=` on the dbt models. If
dbt fails, `queue_completion` never runs, nothing gets appended. No
failure messages on the queue — that's the intended behavior.

### 3. The two jobs

```python
# src/<pkg>/defs/jobs.py
run_single_model_job = dg.define_asset_job(
    "run_single_model_job",
    selection=<all dbt models + queue_completion>,
)
run_all_models_job = dg.define_asset_job(
    "run_all_models_job",
    selection=<all dbt models + queue_completion>,
)
```

Both jobs wrap the same asset set. The distinction is at RunRequest time:
the sensor either narrows via `asset_selection=[orders, queue_completion]`
(single-model) or leaves it wide open (run-all).

### 4. The queue sensor (simulated)

```python
# src/<pkg>/defs/sensors.py
@dg.sensor(jobs=[run_single_model_job, run_all_models_job], minimum_interval_seconds=30)
def queue_sensor(context):
    roll = random.random()

    if roll < 0.15:
        yield dg.SkipReason("simulated: no message in queue this tick")
        return

    if roll < 0.95:
        # 80% — single-model request
        start_date = _random_start_date()
        yield dg.RunRequest(
            job_name="run_single_model_job",
            asset_selection=[dg.AssetKey("orders"), dg.AssetKey("queue_completion")],
            run_config={
                "ops": {
                    "dbt_op": {"config": {"vars": {"start_date": start_date}}},
                    "queue_completion": {"config": {"action": "run_model", "model": "orders", "vars": {"start_date": start_date}}},
                }
            },
        )
    else:
        # 5% — run-all
        yield dg.RunRequest(job_name="run_all_models_job", run_config={...})
```

**Swapping to a real queue:** replace the `random.random()` block with a
call to your queue library — `pika` (RabbitMQ), `redis.xread` (Redis
Streams), `boto3.receive_message` (SQS), `confluent_kafka.Consumer`
(Kafka). Parse each message's payload into a `RunRequest` the same way.
Ack the message after yielding the RunRequest. That's the entire
migration to a real queue.

## What each run looks like

**Single-model run** (80% of ticks):

```
Sensor tick:
  simulated queue msg: run_model orders with start_date=2025-01-01

Dagster run (job=run_single_model_job, selection=[orders, queue_completion]):
  dbt_op:            dbt build --vars '{"start_date": "2025-01-01"}' --select demo.orders
                      → 1 of 1 OK created sql table model main.orders
  queue_completion:  Published to output_queue.jsonl: {
                       "action": "run_model", "model": "orders",
                       "vars": {"start_date": "2025-01-01"}, "status": "success"
                     }
```

**Run-all run** (5% of ticks):

```
Sensor tick:
  simulated queue msg: run_all

Dagster run (job=run_all_models_job, selection=all assets):
  dbt_op:            dbt build --select fqn:*
                      → 5 of 5 OK  (2 seeds + 3 models)
  queue_completion:  Published to output_queue.jsonl: {
                       "action": "run_all", "model": null, "status": "success"
                     }
```

**Skip** (15% of ticks): sensor yields `SkipReason("no message this tick")`,
no run.

## Manual test — bypass the sensor

To verify vars flow end-to-end without waiting for the sensor's random dice:

```yaml
# /tmp/run.yaml
ops:
  dbt_op:
    config:
      vars: {start_date: "2025-01-01"}
  queue_completion:
    config:
      action: run_model
      model: orders
      vars: {start_date: "2025-01-01"}
```

```bash
uv run dg launch --assets 'orders,queue_completion' --config /tmp/run.yaml
```

Then inspect:

```bash
cat output_queue.jsonl                    # → new line appended
# Query the dbt-managed DuckDB directly:
uv run python -c "
import duckdb
con = duckdb.connect('src/dbt_queue_demo/defs/.local_defs_state/DbtProjectComponent__dbt_project__/project/demo.duckdb')
print(con.sql('select count(*), min(order_date) from orders').fetchall())
"
# → (3, datetime.date(2025, 1, 5))   ← the var filtered to 3 rows from 2025+
```

Change the `start_date` and re-run — the row count changes, and a new line
lands in `output_queue.jsonl` with the new vars.

## Why this shape

- **Customize the official integration, don't work around it.** The `--vars`
  flag is native to dbt — passing it through the same code path dbt already
  uses (via `DbtCliResource.cli(...)`) keeps the integration's other benefits
  (asset metadata, column lineage, insights, freshness) intact.
- **Sensor is user-owned Python.** Component wrappers around this pattern
  hide the mental model. A prospect learning Dagster benefits from writing
  the sensor + jobs themselves — ~50 lines total across three files.
- **Success as an asset.** The write-back is a first-class node on the
  asset graph, not an out-of-band hook. Everything downstream of dbt is
  visible in one place.
- **Failures don't publish.** Success-only semantics fall out of asset
  dependencies for free — no extra failure-handling code needed.

## Related walkthroughs

- `dbt_ml_pipeline.md` — Python ML asset between two dbt models
- `dbt_llm_pipeline.md` — LLM-augmented dbt transforms
- `warehouse_migration.md` — one-time legacy DB → warehouse migration
