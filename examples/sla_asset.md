# sla_asset — `@sla` decorator: wall-clock SLA enforcement + escalation
> ✅ **100% offline** — no API keys, no external services.

**Live-validated** — the setup script runs end-to-end and demonstrates a
full 3-breach → escalation cycle in ~90 seconds.

## What this demo shows

Four back-to-back runs, controlled by a `SLEEP_SECONDS` env var:

| Run | Sleep | SLA (0.5s) | Observation |
|---|---|---|---|
| 1 | 0.2s | within | no breach observation |
| 2 | 1.0s | breach | `sla_breach=slow_report_sla`, `sla_escalated=false` |
| 3 | 1.0s | breach #2 | `sla_escalated=false` |
| 4 | 1.0s | breach #3 → **escalated** | `sla_escalated=true` — sensor-actionable |

Escalation is counted across runs using the event log (`context.instance.get_event_records`) within `escalate_window_seconds` — no external state.

## Components used

| Component | What it does |
|---|---|
| `sla_asset` (`@sla` decorator) | Wraps a `@dg.asset` compute with a wall-clock timer. On overrun, emits `AssetObservation(sla_breach=<key>, sla_actual_seconds, sla_expected_seconds, sla_overrun_pct, sla_escalated)`. Companion `SlaAssetComponent` YAML wrapper — see the [`wraps:` composability](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/infrastructure/sla_asset/README.md#composability--wraps-an-existing-component) for stacking SLA onto other components. |

## Why this belongs in Dagster

`@dg.asset(freshness_policy=...)` governs **source freshness** ("when should the upstream data be refreshed"). This is different: `@sla` enforces **wall-clock compute duration** ("this compute should finish in <= 60 seconds"). Complementary, not redundant.

Every primitive rides on Dagster events:
- **Breach event** → `AssetObservation` with typed `MetadataValue`
- **Cross-run breach history** → `context.instance.get_event_records` filtered on the breach tag
- **Escalation** → observation tags; sensors watch them
- **UI trend** → the observation panel shows SLA overrun % over time

Doing this outside Dagster requires a metrics store + alerting integration + retention. Doing it inside: 20 lines of config.

## Two on_breach modes

| Mode | Behavior |
|---|---|
| `warn` (default) | Asset materializes normally + breach observation |
| `fail` | Raise `dg.Failure` with breach metadata AFTER compute (compute time already spent, but downstream doesn't run) |

## Cost

**$0.** Fully offline.

## Required env vars

None (demo controls behavior via `SLEEP_SECONDS`).

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_sla_asset_demo.sh | bash
cd sla-asset-demo
uv run dg dev
```

## The decorated asset

```python
# src/<pkg>/defs/slow_report.py
import os
import time
import dagster as dg
from dagster_community_components import sla

@dg.asset(group_name="sla_demo")
@sla(
    expected_duration_seconds=0.5,
    on_breach="warn",
    escalate_after_n_breaches=3,
    escalate_window_seconds=3600,
    key="slow_report_sla",   # shared with any other assets you want under one SLA budget
)
def slow_report(context) -> dict:
    sleep_s = float(os.environ.get("SLEEP_SECONDS", "0.2"))
    time.sleep(sleep_s)
    return {"rows_processed": 100}
```

## Pair with a sensor

The escalation tag is what makes @sla page-worthy without any extra plumbing:

```python
@dg.sensor
def sla_escalation_alert(context):
    from dagster import DagsterEventType, EventRecordsFilter
    recs = context.instance.get_event_records(
        event_records_filter=EventRecordsFilter(event_type=DagsterEventType.ASSET_OBSERVATION),
        limit=50, ascending=False,
    )
    for r in recs:
        tags = r.asset_observation.tags or {}
        if tags.get("sla_escalated") == "true":
            # page oncall
            ...
```

## After the demo — inspect in the UI

```bash
cd sla-asset-demo
export DAGSTER_HOME=$(pwd)/.dagster_home
uv run dg dev
```

Click `slow_report` → **Observations panel** shows `sla_actual_seconds`, `sla_expected_seconds`, `sla_overrun_pct`, `sla_escalated`. Materialize with `SLEEP_SECONDS=1.5` → another breach appears. Materialize with `SLEEP_SECONDS=0.3` → no breach.

## Composability — SLA-wraps another component

The SlaAssetComponent supports a `wraps:` field to stack the SLA over another component's assets without touching its config — YAML analog of the Python `@sla @dg.asset` decorator stack. See the [`sla_asset` README](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/infrastructure/sla_asset/README.md#composability--wraps-an-existing-component).

## See also

- [`sla_asset` component reference](https://dagster-component-ui.vercel.app/c/sla_asset)
- [`cached_asset` walkthrough](cached_asset.md) — sibling decorator, same shape
- Browse the [walkthrough index](README.md) for more decorator + infrastructure demos.
