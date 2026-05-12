# Partitions — the four canonical shapes end-to-end

**Validated end-to-end** — exercises the canonical partition shape (Phase 1
of the partition rework) across four use cases on the same `dataframe_to_csv`
sink, plus a `PerPartitionBackfillJob` driving multi-tenant rebuilds with
per-tenant concurrency keys. RUN_SUCCESS on every shape, $0 cost.

```
tenant_orders (synthetic source: 3 tenants × 3 regions × 7 days × 5 orders)
       │
       ├── orders_unpartitioned   → unpartitioned baseline
       ├── orders_per_region      → static partitions  (us / eu / apac)
       ├── orders_per_day         → daily partitions   (2025-04-25 … 2025-05-01)
       └── orders_per_tenant      → dynamic partitions (acme / globex / initech, registered at runtime)

tenant_backfill_job              → PerPartitionBackfillJob
                                     materializes orders_per_tenant for every
                                     registered tenant, with per-tenant concurrency
                                     keys (`tenant-{partition_key}`)
```

## Why this demo exists

The original consumer feedback that motivated the partition rework
called out three concrete gaps:

1. **No `dynamic` partitions on any sink** — multi-tenant SaaS users had
   to fork every component to add `DynamicPartitionsDefinition`.
2. **`MultiPartitionsDefinition` hardcoded `"date"` as one axis** — couldn't
   express `(tenant, date)`.
3. **`PerPartitionBackfillJob` had a single static concurrency key** —
   couldn't express "serialize same-tenant runs but parallelize cross-tenant".

This demo demonstrates that all three are now first-class:

- `partition_type: dynamic` + `dynamic_partition_name: tenants` works
  on any partition-aware component (every sink, transform, ingestion,
  AI, analytics, external_*_asset).
- `partition_dimensions` (a list of dim specs) replaces the hardcoded
  multi-axis shape and supports any combination including `(dynamic, daily)`.
- `concurrency_key_template: "tenant-{partition_key}"` on
  `PerPartitionBackfillJob` produces per-partition concurrency keys.

## Components used

| Component | Role |
|---|---|
| `dataframe_to_csv` | Sink — exercised four times with different `partition_type` values |
| `per_partition_backfill_job` | Job — drives the dynamic-partition sink with per-tenant concurrency |

## Required env vars

None. Demo is fully local.

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_partitions_demo.sh | bash
cd partitions-demo

# 1. Materialize the source. Use +<asset> so DAGSTER_HOME state persists
#    across launches (each `dg launch` creates a fresh tmp home otherwise).
uv run dg launch --assets +orders_unpartitioned                # one CSV
uv run dg launch --assets +orders_per_region --partition us    # one CSV per region
uv run dg launch --assets +orders_per_day --partition 2025-04-30  # one CSV per day

# 2. Register the synthetic tenants (one-shot sensor populates the
#    DynamicPartitionsDefinition named 'tenants').
uv run dg sensor cursor register_tenants_sensor --tick

# 3. Materialize a single dynamic-partition key.
uv run dg launch --assets +orders_per_tenant --partition acme

# 4. Run the backfill job — materializes orders_per_tenant for every
#    registered tenant in parallel, with per-tenant concurrency keys.
uv run dg launch --jobs tenant_backfill_job
```

Or open the asset graph:

```bash
uv run dg dev   # http://localhost:3000 → Assets graph
```

## What to look at after running

```bash
ls /tmp/partitions_demo/per_*
# per_region/   us  eu  apac  CSVs
# per_day/      2025-04-25 … 2025-05-01 CSVs
# per_tenant/   acme  globex  initech CSVs
```

In the Dagster UI, the asset graph shows partition status per shape.
The `tenant_backfill_job` run page shows three parallel ops materializing
the per-tenant sinks, each tagged with its `dagster/concurrency_key`
(`tenant-acme`, `tenant-globex`, `tenant-initech`).

## Strict validation in action

The new helper raises clear errors on misconfiguration. Try editing
`orders_per_tenant/defs.yaml`:

- Remove `dynamic_partition_name`: get `partition_type='dynamic'
  requires dynamic_partition_name.`
- Set both `partition_dimensions: [...]` and `partition_type: dynamic`:
  get `Set either partition_type (flat-fields shape) or
  partition_dimensions (multi-axis shape), not both.`
- Set `partition_type: daily` without `partition_start`: get
  `partition_type='daily' requires partition_start (ISO date, e.g.
  '2024-01-01').`

Previously these silently picked default values (e.g. `'2024-01-01'`)
or created an empty `MultiPartitionsDefinition` that would fail downstream.
