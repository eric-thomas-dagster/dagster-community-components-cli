# partition_lock_asset — `@partition_lock` decorator + `PartitionLockAssetComponent.wraps:` composability
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end demonstrating BOTH
shapes of the DCC decorator API (including a real held-lock conflict) in ~3 minutes.

## What this demo shows

Two shapes, same event-log-backed per-partition mutex. Sequential
launches normally can't demonstrate the conflict path (the try/finally
releases the lock before the next launch), so the setup script uses
`DagsterInstance.report_runless_asset_event` to **pre-inject a stale
"acquired" observation** — the same event-log state a concurrent run
would create.

| Run | Asset | Partition | Behavior |
|---|---|---|---|
| 1 | `py_backfill` (Python decorator) | us-east | acquired lock, ran (1s sleep), released |
| 2 | `py_backfill` (Python decorator) | us-west | held lock pre-injected → SKIPPED, compute short-circuits (no `[py_backfill]` line in log) |
| 3 | `yaml_customers` (YAML composability) | us-east | acquired lock, inner data-gen ran (100 customer rows for us-east), released |
| 4 | `yaml_customers` (YAML composability) | us-west | held lock pre-injected → SKIPPED, inner data-gen NEVER INVOKED (no "Generated DataFrame" line) |

## The two shapes — same primitive, different authoring surface

**Shape 1: Python decorator** — best when you already have Python code you want to protect:

```python
# src/<pkg>/defs/py_backfill.py
import dagster as dg
from dagster_community_components import partition_lock

REGIONS = dg.StaticPartitionsDefinition(["us-east", "us-west"])

@dg.asset(partitions_def=REGIONS, group_name="python_decorator")
@partition_lock(ttl_seconds=300, on_conflict="skip")
def py_backfill(context):
    return backfill_region(context.partition_key)
```

Each `partition_key` is its own lock scope — us-east and us-west can materialize concurrently; two runs on the same partition_key cannot.

**Shape 2: YAML composability — the money shot.** `PartitionLockAssetComponent` wraps **another DCC component**. Inner defines its own `partitions_def`; outer gates each partition's compute on the per-partition lock:

```yaml
# src/<pkg>/defs/yaml_customers/defs.yaml
type: dagster_community_components.PartitionLockAssetComponent
attributes:
  ttl_seconds: 300
  on_conflict: skip
  wraps:
    type: dagster_community_components.SyntheticDataGeneratorComponent
    attributes:
      asset_name: yaml_customers
      schema_type: customers
      row_count: 100
      random_state: 42
      partition_type: static
      partition_values: us-east,us-west
```

**One asset is registered** (`yaml_customers`) — no duplication. Add or remove the outer wrap without touching the inner's config. Stack arbitrarily deep — `PartitionLockAssetComponent { wraps: SlaAssetComponent { wraps: SnowflakeQueryComponent { ... } } }` = `@partition_lock @sla @snowflake-compute` in Python decorator terms.

## Conflict-handling policies

| Policy | Behavior |
|---|---|
| `on_conflict: wait` (default) | Raise `dg.RetryRequested` — step goes to `up_for_retry`, Dagster frees the worker slot during backoff, polls visible in the run graph. Budget capped by `max_wait_seconds` (default 300s) and `poll_seconds` (default 5s). |
| `on_conflict: skip` | Emit `partition_lock_skipped` observation, return synthetic `MaterializeResult` — inner compute NEVER invoked. Best when duplicate work is silly (idempotent backfills, cache warmers). |
| `on_conflict: fail` | Raise `dg.Failure` immediately with metadata (partition_key, held_age_seconds, ttl_seconds). Best when a conflict is a real problem (leader-election patterns). |

## Why the YAML composability is the money shot

Before `wraps:`, YAML users had to write Python callables and reference them via `compute: {kind: python, python: 'mod:fn'}` to use decorators. That's fine but requires user Python.

With `wraps:`, `PartitionLockAssetComponent` stacks over **any DCC component** with zero user Python:

- `PartitionLockAssetComponent { wraps: SnowflakeQueryComponent }` — one warehouse query per partition at a time
- `PartitionLockAssetComponent { wraps: DatabaseReplicationComponent }` — one replication run per source per period
- `PartitionLockAssetComponent { wraps: LlmPromptExecutorComponent }` — one LLM call per (tenant, day) — prevent expensive dupes
- `PartitionLockAssetComponent { wraps: DataframeToBigqueryTableComponent }` — one write per partition at a time
- `PartitionLockAssetComponent { wraps: SyntheticDataGeneratorComponent }` — the demo shape

## Components used

| Component | What it does |
|---|---|
| `partition_lock_asset` (`@partition_lock` decorator + `PartitionLockAssetComponent`) | Per-partition mutex via `AssetObservation` events. Lock auto-expires after `ttl_seconds`. Three conflict policies. Companion sensor pattern: find stuck holders (acquired observations older than TTL with no corresponding released). |
| `synthetic_data_generator` | Inner component the YAML shape wraps — this component supports `partition_type: static` + `partition_values:` for per-region partitioning. |

## Why this belongs in Dagster

- **Lock state lives in the event log** — restart-safe, worker-safe. No Redis, no side database.
- **Auto-expires via TTL** — protects against stuck holders. No manual cleanup jobs.
- **`on_conflict: wait` uses `dg.RetryRequested`** — worker slot freed during backoff, poll count visible in the run graph, no thread blocked on `time.sleep`.
- **Composability at the component layer** — wrap ANY DCC component with per-partition mutex without editing that component's config.

## Race-condition disclosure

This is a **probabilistic mutex, not a distributed atomic**. Two runs starting within ~200 ms of each other could both observe an unlocked state and acquire. Acceptable for "prevent 5-minute concurrent backfill duplicates" — **NOT for money transfers**. For strong mutual exclusion, use a warehouse table lock or Postgres advisory lock inside the compute.

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_partition_lock_asset_demo.sh | bash
cd partition-lock-asset-demo
uv run dg dev
```

## Pair with a sensor — stuck-lock detection

```python
@dg.sensor(name="partition_lock_stuck_holder_watcher")
def partition_lock_stuck_watcher(context):
    import time
    from dagster import DagsterInstance
    TTL = 300  # seconds
    inst = context.instance
    for asset_name in ("yaml_customers",):
        recs = list(reversed(inst.fetch_observations(
            records_filter=dg.AssetKey(asset_name), limit=200,
        ).records))
        # Find acquired events with no matching released event within TTL.
        acquired = {}
        for r in recs:
            tags = dict(r.asset_observation.tags or {})
            if pk := tags.get("partition_lock_acquired"):
                acquired[pk] = float(r.timestamp)
            if pk := tags.get("partition_lock_released"):
                acquired.pop(pk, None)
        now = time.time()
        for pk, acquired_at in acquired.items():
            if now - acquired_at > TTL:
                # Lock held longer than TTL with no release → probably a dead run
                ...
```

## After the demo — inspect in the UI

```bash
cd partition-lock-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click either asset → **Observations panel** shows `partition_lock_acquired` / `partition_lock_released` / `partition_lock_skipped` tags per partition_key. Filter by tag for a per-lock-scope activity view.

## See also

- [`partition_lock_asset` component reference](https://dagster-component-ui.vercel.app/c/partition_lock_asset)
- [`throttle_asset` walkthrough](throttle_asset.md) — orthogonal: throttle = inter-run gap; partition_lock = per-partition concurrency=1
- [`dry_run_asset` walkthrough](dry_run_asset.md) — same `wraps:` pattern, different primitive
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
