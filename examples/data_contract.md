# Producer/consumer schema contracts — with breaking-change detection
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end in ~2 minutes and
exercises the whole DCC data-contract surface:

1. `@data_contract` producer emits its contract as an `AssetObservation`.
2. `@requires_contract` consumer reads that observation before compute.
3. A stricter consumer FAILS with a clean `dg.Failure` when the upstream
   contract version is behind.
4. Rewriting the producer with a schema change triggers **breaking-change
   detection** — an extra observation with a rendered markdown diff.
5. Both shapes: Python decorators AND `DataContractComponent` YAML.
6. Bonus: `contract_from_json_schema('.../schema.json')` — turn an
   existing JSON Schema into a DCC contract dict.

## What this demo shows

Every enforcement primitive is a Dagster event:

| Primitive | Dagster event |
|---|---|
| Schema violation | `AssetCheckResult(severity=ERROR)` per column |
| Freshness / row-count SLA | `AssetCheckResult` vs. prior materialization (event log lookup) |
| Contract version | Asset `code_version` — the UI shows bumps automatically |
| Ownership / consumer registry | `AssetObservation` tagged `contract_owners` / `contract_consumers` |
| Contract snapshot | `AssetObservation` with `contract_snapshot` metadata (JSON) |
| Breaking change | `AssetObservation` tagged `contract_breaking_change=true` + markdown summary |
| Consumer-side gate | `dg.Failure` from `@requires_contract` BEFORE compute runs |

You can't build this outside Dagster without reimplementing the event
log, the check panel, the automation-condition engine, and change
detection — the primitives are already there.

## Runs

| Run | Asset | Behavior |
|---|---|---|
| 1 | `orders` (`@data_contract` v1.0.0) | contract v1.0.0 emitted as an `AssetObservation` |
| 2 | `daily_totals` (`@requires_contract` min v1.0.0) | passes; emits `requires_contract_satisfied=true` |
| 3 | `strict_consumer` (`@requires_contract` min v3.0.0) | **FAILS** with `upstream contract version 1.0.0 < required 3.0.0` — before compute |
| 4 | `orders` rewritten to v2.0.0 (`status` dropped, `amount` narrowed) | extra observation tagged `contract_breaking_change=true` with markdown diff |
| 5 | `orders_yaml` (`DataContractComponent`) | same engine, YAML shape — contract emitted as observation |
| 6 | `yaml_consumer` (`RequiresContractComponent`) | passes upstream v1.0.0 |
| Bonus | `contract_from_json_schema('.../orders.schema.json')` | JSON Schema → DCC contract dict |

## PRODUCER — `@data_contract`

```python
# src/<pkg>/defs/orders/asset.py
import pandas as pd
import dagster as dg
from dagster_community_components import data_contract


@data_contract(
    contract={
        "version": "1.0.0",
        "owners":    ["data-platform@example.com"],
        "consumers": ["analytics-team", "finance"],
        "schema": [
            {"name": "order_id", "type": "string",  "nullable": False, "unique": True},
            {"name": "amount",   "type": "float64", "nullable": False, "min": 0},
            {"name": "status",   "type": "string",  "allowed_values": ["placed", "shipped", "cancelled"]},
        ],
    },
    on_violation="block",           # default: dg.Failure on any check fail
    detect_breaking_changes=True,   # diff vs. prior emission — see below
    on_breaking_change="warn",      # "warn" emits observation; "fail" also raises dg.Failure
)
@dg.asset(group_name="producer")
def orders(context) -> pd.DataFrame:
    return pd.DataFrame({
        "order_id": ["A1", "A2", "A3", "A4", "A5"],
        "amount":   [10.50, 22.00, 3.25, 47.75, 8.00],
        "status":   ["placed", "shipped", "placed", "cancelled", "shipped"],
    })
```

Every rule in the contract becomes a first-class `AssetCheckResult` in
the panel — you never hand-mirror them into `check_specs=` yourself.
The full contract is also emitted as `AssetObservation.metadata.contract_snapshot`
so downstream consumers can inspect the whole schema.

## CONSUMER (passing) — `@requires_contract`

```python
# src/<pkg>/defs/daily_totals/asset.py
import dagster as dg
from dagster_community_components import requires_contract


@dg.asset(group_name="consumer", deps=["orders"])
@requires_contract(
    upstream="orders",
    min_version="1.0.0",
    require_columns=["order_id", "amount"],
)
def daily_totals(context):
    # `@requires_contract` looks up the upstream contract observation BEFORE
    # this line runs. If it's missing / older / lacking a required column,
    # dg.Failure is raised BEFORE compute.
    return {"rows_seen": 5}
```

On success, `@requires_contract` emits its own
`AssetObservation(requires_contract_satisfied=true, upstream_contract_version=…)`
on the DOWNSTREAM asset — searchable alongside the producer's contract
observation.

## CONSUMER (fails) — version-mismatch `dg.Failure`

```python
# src/<pkg>/defs/strict_consumer/asset.py
@dg.asset(group_name="consumer", deps=["orders"])
@requires_contract(
    upstream="orders",
    min_version="3.0.0",   # producer is 1.0.0
)
def strict_consumer(context):
    # never reached
    ...
```

Run output:

```
dagster._core.definitions.events.Failure: upstream contract version 1.0.0 < required 3.0.0
```

The failure is raised BEFORE compute so no wasted work — you get a clean
event-log record of exactly which consumer blocked on which version. Pair
with `AutomationCondition.eager()` to gate re-run of everything downstream
of the failing consumer.

## Breaking-change detection

Between RUN 3 and RUN 4, the demo REWRITES `orders/asset.py` to bump the
contract to v2.0.0 with two breaking changes:

```diff
     "version": "1.0.0",                                       →   "2.0.0",
     "schema": [
         {"name": "order_id", ...},
-        {"name": "amount",   "type": "float64", ...},         →   {"type": "int64", ...},   # narrowed
-        {"name": "status",   "allowed_values": [...]},        →   (dropped)
     ],
```

Materializing `orders` triggers `detect_breaking_changes=True`. The
component compares current vs. prior contract observation from the event
log and emits an ADDITIONAL observation:

```
tags:
  contract_breaking_change:  true
  contract_prior_version:    1.0.0
  contract_current_version:  2.0.0
metadata:
  breaking_change_count:     2
  breaking_changes: [
    {"kind": "narrowed_type",  "column": "amount", "prior": "float64", "current": "int64",
     "detail": "column 'amount': float64 → int64 (narrowed)"},
    {"kind": "dropped_column", "column": "status",
     "detail": "column 'status' removed"},
  ]
  summary: |
    # Contract breaking change: `1.0.0` → `2.0.0`

    **2 breaking change(s) detected:**

    | Kind             | Column   | Detail                                            |
    |------------------|----------|---------------------------------------------------|
    | narrowed_type    | amount   | column 'amount': float64 → int64 (narrowed)       |
    | dropped_column   | status   | column 'status' removed                           |
```

`on_breaking_change="warn"` (default) keeps the run green — flip to
`"fail"` to promote breaking flags to a hard `dg.Failure` and block
materialization. Either way, the observation lands so downstream can
sensor on it.

Breaking-change kinds detected:

- **Dropped column** — present in prior, missing in current
- **Narrowed type** — e.g. `float64 → int64`, `string → int`
- **Nullability narrowed** — `nullable: true → false`

## Shape 2 — YAML (`DataContractComponent` + `RequiresContractComponent`)

Same enforcement engine as the Python decorators, no Python for the
asset itself. The `compute:` block references a `mod:fn` callable that
returns a DataFrame — everything else lives in YAML:

```yaml
# src/<pkg>/defs/orders_yaml/defs.yaml
type: dagster_community_components.DataContractComponent
attributes:
  asset_name: orders_yaml
  group_name: yaml_shape
  compute:
    kind: python
    python: "<pkg>.defs.yaml_compute:build_orders_yaml"
  contract:
    version: "1.0.0"
    owners:    [data-platform@example.com]
    consumers: [analytics-team]
    schema:
      - {name: order_id, type: string,  nullable: false, unique: true}
      - {name: amount,   type: float64, nullable: false, min: 0}
      - {name: status,   type: string,  allowed_values: [placed, shipped, cancelled]}
  on_violation: block
  detect_breaking_changes: true
  on_breaking_change: warn
```

Consumer side — `RequiresContractComponent` — has two authoring modes:

```yaml
# Mode A: `compute:` — new asset from scratch.
type: dagster_community_components.RequiresContractComponent
attributes:
  asset_name: yaml_consumer
  upstream: orders_yaml
  min_version: "1.0.0"
  require_columns: [order_id, amount]
  compute:
    kind: python
    python: "<pkg>.defs.yaml_compute:build_consumer_yaml"
```

```yaml
# Mode B: `wraps:` — gate ANY DCC component's compute with the contract
# check. Same composability pattern as ThrottleAssetComponent.wraps.
type: dagster_community_components.RequiresContractComponent
attributes:
  upstream: orders_yaml
  min_version: "1.0.0"
  wraps:
    type: dagster_community_components.DataframeTransformerComponent
    attributes:
      asset_name: daily_revenue
      # ... inner component's normal config ...
```

## Bonus — `contract_from_json_schema()`

Teams that already publish schemas as JSON Schema (OpenAPI, event bus,
contract-registry) don't have to hand-mirror them into DCC contract
dicts:

```python
from dagster_community_components import contract_from_json_schema, data_contract

CONTRACT = contract_from_json_schema(
    "orders.schema.json",
    version="1.0.0",
    owners=["data-platform@example.com"],
)

@data_contract(contract=CONTRACT)
@dg.asset
def orders(context): ...
```

Supported top-level shape: `{"type": "object", "properties": {...}, "required": [...]}`.
Per-property mapping:

- `type: string | integer | number | boolean | array | object` →
  `string | int | float | bool | list | dict`
- Union types (`["string", "null"]`) — pick the non-null member and force
  `nullable=True`
- `required` (top-level list) — set `nullable=False`
- `pattern` → `regex`
- `enum` → `allowed_values`
- `minimum` / `maximum` → `min` / `max`

## Components used

| Component | What it does |
|---|---|
| `data_contract` (`@data_contract` decorator + `DataContractComponent`) | Producer-side. Enforces schema / nullability / uniqueness / min/max / allowed_values / regex / freshness / row-count SLA on every materialization. Emits `AssetCheckResult` per rule + one `AssetObservation` with contract snapshot + optional breaking-change observation. |
| `data_contract` (`@requires_contract` decorator + `RequiresContractComponent`) | Consumer-side. Reads upstream contract observation from the event log before compute; `dg.Failure` on missing / stale / missing-column. |

## Why Dagster is the right home for contracts

- **State-free** — every prior contract, every row-count baseline, every
  materialization timestamp already lives in the event log. No sidecar
  contract registry to run.
- **First-class UI** — schema failures render in the check panel. Contract
  version bumps show up in the `code_version` column. Owner tags are
  searchable.
- **Composes with automation** — `AutomationCondition.eager()` on any
  failing check blocks downstream automatically. `code_version_changed()`
  triggers re-materialization on contract bumps.
- **Composes with sensors** — a sensor on `contract_breaking_change=true`
  observations can page an owner, open a Slack thread, or bump a version.
- **Contract snapshot in metadata** — every observation carries the full
  JSON contract, so a downstream agent (or a human in the check panel)
  can diff two arbitrary versions from log records alone.

## Cost

**$0.** Fully offline.

## Required env vars

None.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_data_contract_demo.sh | bash
cd data-contract-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
# → http://localhost:3000 → Assets → orders → Checks + Observations
```

## After the demo — inspect in the UI

```bash
cd data-contract-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click `orders` in the asset graph:

- **Checks panel** — one row per contract column (`schema_order_id`,
  `schema_amount`, `schema_status`) plus freshness / SLA if declared.
- **Observations panel** — the `contract_version` observation, and (after
  RUN 4) the `contract_breaking_change=true` observation with the rendered
  markdown diff.

Click `daily_totals` — its `requires_contract_satisfied=true` observation
records exactly which upstream version it accepted.

## Pair with a breaking-change sensor

```python
@dg.sensor(name="contract_breaking_watcher")
def contract_breaking_watcher(context):
    from dagster import DagsterEventType, EventRecordsFilter
    recs = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(event_type=DagsterEventType.ASSET_OBSERVATION),
        limit=100, ascending=False,
    )
    for r in recs:
        obs = r.asset_observation
        if (obs.tags or {}).get("contract_breaking_change") == "true":
            # page the owner listed in the contract, or open a Slack thread
            ...
```

## See also

- [`data_contract` component reference](https://dagster-component-ui.vercel.app/c/data_contract)
- [`throttle_asset` walkthrough](throttle_asset.md) — same `wraps:` composability pattern used by `RequiresContractComponent`.
- [`sla_asset` walkthrough](sla_asset.md) — orthogonal quality primitive (compute duration, not schema).
- [Composition primitives walkthrough](composition_primitives.md) — the broader `wraps:` family.
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
