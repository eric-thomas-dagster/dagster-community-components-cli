# Doris + StarRocks — one round-trip demo, two OSS MPP databases
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

Docker-local end-to-end validation of the Apache Doris component set — and, because StarRocks is a Doris fork that speaks the same MySQL wire protocol, the same demo shell works against `starrocks/allin1-ubuntu` with a one-argument swap.

**Setup (Doris — default):**

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_doris_starrocks_demo.sh \
  -o setup_doris_starrocks_demo.sh
bash setup_doris_starrocks_demo.sh
```

**Setup (StarRocks variant):**

```bash
bash setup_doris_starrocks_demo.sh sr_demo starrocks
```

Both variants: `cd <project> && export <ENGINE>_FE_HOST=127.0.0.1 && uv run dg dev` → UI at http://localhost:3000. Requirements: [uv](https://docs.astral.sh/uv/) + Docker. Cost: $0.

## What gets validated

| Component | Role in demo |
|---|---|
| `doris_resource` / `starrocks_resource` | Shared connection handle |
| `doris_query_asset` | SQL query source → DataFrame |
| `dataframe_to_doris` / `dataframe_to_starrocks` | Bulk-load DataFrame back into the engine (round-trip validation) |
| `external_doris_table` / `external_starrocks_table` | Declare a table as a first-class Dagster asset (no runtime) |
| `doris_workspace` | Auto-enumerate every `analytics.*` table as a Dagster asset |
| `doris_routine_load_sensor` | Watch a Routine Load job's health (wire it against Kafka in production) |

## The lineage that materializes

```
                ┌────────────────────────────┐
                │  Container: apache/doris   │
                │           (or starrocks)   │
                │                            │
                │  analytics.orders  (seed:  │
                │  5 rows via setup script)  │
                └─────────────┬──────────────┘
                              │
                              ▼
                ┌────────────────────────────┐
                │ recent_orders              │
                │ (doris_query_asset SQL:    │
                │  SELECT ... WHERE total>100│
                │  → DataFrame, 3 rows)      │
                └────────────────────────────┘

┌────────────────────┐
│ orders_top3        │
│ (Python @asset —   │
│  synthetic 3 rows) │
└────────┬───────────┘
         │
         ▼
┌────────────────────────────┐
│ orders_top3_loaded         │
│ (dataframe_to_doris sink — │
│  bulk-loads back into      │
│  analytics.orders)         │
└────────────────────────────┘

Plus (from doris_workspace enumeration):
   analytics.orders — one asset per discovered table

Plus (declare-only):
   external doris/analytics/orders — first-class node, no execution
```

## Docker-image notes

- **Apache Doris** — `apache/doris:doris-all-in-one-2.1.0` (bundled FE + BE + broker). ~2 GB first-run pull. First-boot is 60-90 seconds; the setup script polls until MySQL protocol accepts connections.
- **StarRocks** — `starrocks/allin1-ubuntu:latest`. Slightly smaller. Same MySQL protocol, so every Doris-family component works against it via the `starrocks_resource` / `dataframe_to_starrocks` sibling components.

Both use the same host / port defaults (`:9030` for MySQL query, `:8030` for HTTP bulk-load). If your local `:9030` is already in use, edit the setup script's `-p ${QUERY_PORT}:${QUERY_PORT}` binding.

## Teardown

```bash
docker stop dagster_doris_demo && docker rm dagster_doris_demo
# (or dagster_starrocks_demo)
```

## Why Doris + StarRocks in one demo

The two engines fork from the same codebase and remain wire-protocol-compatible. If a customer asks "does this work with StarRocks too?" the answer is yes — same YAML, one `starrocks_*` prefix swap. Shipping the demo with both variants tests the compatibility claim in one setup script.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_doris_starrocks_demo.sh \
  -o setup_doris_starrocks_demo.sh
bash setup_doris_starrocks_demo.sh
```

## See also

- `examples/README.md` — the demo TOC
- [clickhouse](clickhouse.md) — the same round-trip pattern for ClickHouse
- Component registry pages: `doris_workspace`, `starrocks_resource`, etc.
