# Temporal Signal + Query — Push and Pull Live Workflow State

**Components:**
- `TemporalSignalAssetComponent` (`assets/infrastructure/temporal_signal_asset`)
- `TemporalQueryAssetComponent` (`assets/infrastructure/temporal_query_asset`)

**Script:** [`setup_temporal_signal_query_demo.sh`](./setup_temporal_signal_query_demo.sh)
**Cost:** $0 (local Temporal dev server, no external APIs)
**Duration:** ~15 seconds cold-to-green
**Validated:** 2026-07-02 — full push+pull sequence executed against a live long-lived `OrderBatchWorkflow`.

## The story (blog post material)

The existing Temporal trio — [trigger](./temporal_workflow.md), [external](./temporal_workflow.md), [sensor](./temporal_workflow.md) — covers **start** and **observe terminal state**. That's fine for one-shot workflows.

**But Temporal's real superpower is long-lived workflows** — batch aggregators, approval routers, streaming pipelines, long-running LLM agents. These workflows run for hours, days, or indefinitely, and their state is more interesting *while they're running* than when they finish.

That's what these two components add:
- **`temporal_signal_asset`** — a Dagster asset that PUSHES state into a running workflow (via Temporal Signal).
- **`temporal_query_asset`** — a Dagster asset that PULLS live state OUT of a running workflow (via Temporal Query).

Together with the trio, you now have all four modes of Dagster ↔ Temporal interaction:

|                              | one-shot workflow    | long-lived workflow    |
|------------------------------|----------------------|------------------------|
| **fire from Dagster**        | `trigger`            | `signal_asset` (push)  |
| **read from Dagster**        | `sensor` / `external`| `query_asset` (pull)   |

## The demo workflow

The setup script spins up a local Temporal dev server plus a Python worker that registers `OrderBatchWorkflow` — a long-lived workflow with:

- Signal handlers: `add_order(order)`, `flush_batch({...})`, `shutdown()`
- Query handlers: `get_pending() -> list[dict]`, `stats() -> dict`

The workflow is started and kept running (awaiting signals). The Dagster project then defines four assets, one per component:

```
temporal/batch/pending  ← query get_pending  (list of pending orders)
temporal/batch/stats    ← query stats         ({pending_count, flushed_batch_count, total_flushed_orders})
temporal/batch/add      → signal add_order    (adds one order sourced from Dagster)
temporal/batch/flush    → signal flush_batch  (triggers batch flush)
```

The script then runs a scripted sequence:

```
Query 1 (stats)    → {pending_count: 2, flushed_batch_count: 0, total_flushed_orders: 0}
Signal 1 (add)     → adds {"id":"dagster-run-...", "amount":99.99}
Query 2 (pending)  → 3 items: seed-1, seed-2, dagster-run-...
Signal 2 (flush)   → triggers batch flush
Query 3 (stats)    → {pending_count: 0, flushed_batch_count: 1, total_flushed_orders: 3}
```

Every arrow is a real gRPC call against the running workflow. No mocks.

## Validated run output (2026-07-02)

```
=== QUERY 1: initial state ===
query_result: {"pending_count": 2, "flushed_batch_count": 0, "total_flushed_orders": 0}

=== SIGNAL 1: add_order ===
signal_sent_at:      2026-07-03T00:56:06.464121+00:00
signal_args_preview: {"id": "signal-1", "amount": 42.5}

=== QUERY 2: pending list after add ===
query_result_count: 3
query_result: [{"amount":10.0,"id":"seed-1"},{"amount":20.0,"id":"seed-2"},{"amount":42.5,"id":"signal-1"}]

=== SIGNAL 2: flush_batch ===
signal_sent_at:      2026-07-03T00:56:06.564599+00:00
signal_args_preview: {"reason": "nightly-smoke-test", "requested_at": "138dd5c0-..."}

=== QUERY 3: stats after flush ===
query_result: {"pending_count": 0, "flushed_batch_count": 1, "total_flushed_orders": 3}
```

The state visibly changes across the sequence — proof both directions of the bridge work.

## Use cases

- **Batch aggregators.** Long-running workflow buffers records; nightly Dagster asset signals `flush_batch` and a downstream Dagster asset queries the resulting count for lineage.
- **Approval routers.** Workflow waits for reviewer decisions; Dagster asset signals `record_approval` when an upstream reviewer commits; another Dagster asset queries `pending_approvals` for daily reporting.
- **Long-running LLM agents.** Agent workflow runs indefinitely accumulating a transcript; Dagster asset signals `add_prompt` from a downstream pipeline; another Dagster asset queries `current_transcript` for observability.
- **Human-in-the-loop.** Dagster asset materializes when a reviewer clicks "approve"; the materialization signals a pending approval workflow.
- **Streaming pipelines.** Workflow processes events indefinitely; Dagster asset queries "last N processed" for smoke tests.

## The worker (Temporal side)

```python
# temporal_worker/worker.py
@workflow.defn
class OrderBatchWorkflow:
    def __init__(self):
        self._pending: list[dict] = []
        self._flushed_batches: list[list[dict]] = []
        self._flush_now = False

    @workflow.signal
    def add_order(self, order: dict) -> None:
        self._pending.append(order)

    @workflow.signal
    def flush_batch(self, args: dict = {}) -> None:
        self._flush_now = True

    @workflow.query
    def get_pending(self) -> list[dict]:
        return list(self._pending)

    @workflow.query
    def stats(self) -> dict:
        return {
            "pending_count": len(self._pending),
            "flushed_batch_count": len(self._flushed_batches),
            "total_flushed_orders": sum(len(b) for b in self._flushed_batches),
        }

    @workflow.run
    async def run(self):
        while True:
            await workflow.wait_condition(lambda: self._flush_now)
            if self._pending:
                self._flushed_batches.append(list(self._pending))
                self._pending.clear()
            self._flush_now = False
```

## The Dagster side (four defs.yaml files)

```yaml
# temporal/batch/pending
type: dagster_community_components.TemporalQueryAssetComponent
attributes:
  asset_key: temporal/batch/pending
  workflow_id: order-batch-demo
  query_name: get_pending

# temporal/batch/stats
type: dagster_community_components.TemporalQueryAssetComponent
attributes:
  asset_key: temporal/batch/stats
  workflow_id: order-batch-demo
  query_name: stats

# temporal/batch/add — sends a Dagster-sourced order into the workflow
type: dagster_community_components.TemporalSignalAssetComponent
attributes:
  asset_key: temporal/batch/add
  workflow_id: order-batch-demo
  signal_name: add_order
  signal_arg:
    id: dagster-run-{run_id}
    amount: 99.99

# temporal/batch/flush — triggers a batch flush
type: dagster_community_components.TemporalSignalAssetComponent
attributes:
  asset_key: temporal/batch/flush
  workflow_id: order-batch-demo
  signal_name: flush_batch
  signal_arg:
    reason: dagster-scheduled-flush
    requested_at: "{run_id}"
```

## Requirements

- `uv` (https://docs.astral.sh/uv/)
- `temporal` CLI (`brew install temporal`)

## Temporal Cloud

Same components, same code. Point at Cloud in the YAML:

```yaml
target_host: myns.abcde.tmprl.cloud:7233
namespace:   myns.abcde
api_key_env_var: TEMPORAL_CLOUD_API_KEY
# OR mTLS:
tls_cert_env_var: TEMPORAL_TLS_CERT
tls_key_env_var:  TEMPORAL_TLS_KEY
```

## Related

- [`temporal_workflow` demo](./temporal_workflow.md) — the trio (trigger + external + sensor) for terminal-state observation.
- [`langgraph_agent`](./langgraph_agent.md) — multi-step LLM pipeline as a Dagster asset (composes nicely with query_asset for durable agent state).
