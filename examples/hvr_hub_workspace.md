# HVR Hub Workspace — standalone HVR 6.x → Dagster external assets

**Component:** `dagster_community_components.HvrHubWorkspaceComponent`
**Validation:** `code` (mock-tested; live-validate against your Hub with `{{ env }}` changes)

## What this is

Full Fivetran-shape workspace component for **standalone HVR Hub 6.x**.
One YAML wires the whole Hub — every replicated table shows up as a
Dagster asset with lineage + freshness telemetry.

```
┌─────────────────────────────────────────────────────────────────────┐
│                      HVR Hub Server (6.1 – 6.3)                     │
│    ┌──────────────┐   ┌──────────────┐   ┌──────────────┐           │
│    │ channel:     │   │ channel:     │   │ channel:     │           │
│    │  sales_cdc   │   │ orders_stream│   │  ...         │           │
│    │  ├─ orders   │   │  └─ events   │   │              │           │
│    │  └─ customers│   │              │   │              │           │
│    └──────────────┘   └──────────────┘   └──────────────┘           │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ REST (bearer JWT)
                               ▼
     ┌──────────────────────────────────────────────────────────┐
     │           HvrHubWorkspaceComponent (one YAML)             │
     │  - Discovers channels + tables at load                    │
     │  - Emits one Dagster asset per (channel × loc × table)    │
     │  - Optional polling sensor: integrate-lag observations    │
     │  - Optional asset check: SLA on integrate lag             │
     └──────────────────────────────────────────────────────────┘
                               │
                               ▼
       Dagster catalog: hvr/prod_hub/sales_cdc/snowflake_dw/orders
                        hvr/prod_hub/sales_cdc/snowflake_dw/customers
                        hvr/prod_hub/orders_stream/snowflake_dw/events
                        prod_hub_hvr_observer (sensor)
```

## Which HVR do you have?

Ask this first:

> *"Is your HVR through the Fivetran dashboard, or a standalone HVR Hub
> install with a `.hvr` config directory?"*

- **Fivetran dashboard** → use official [`dagster-fivetran`](https://docs.dagster.io/integrations/fivetran) — this component doesn't reach that surface.
- **Standalone HVR Hub** → keep reading.

## Setup

```bash
uvx create-dagster project hvr-catalog --no-uv-sync
cd hvr-catalog
uv sync
uvx --from dagster-community-components-cli dagster-component init --auto-install
uvx --from dagster-community-components-cli dagster-component add hvr_hub_workspace
```

Fill in `src/<pkg>/defs/hvr_hub_workspace/defs.yaml`:

```yaml
type: dagster_community_components.HvrHubWorkspaceComponent
attributes:
  workspace:
    hub_url:  "{{ env.HVR_HUB_URL }}"       # http://hvr-hub.internal:4340
    hub_name: "{{ env.HVR_HUB_NAME }}"      # prod_hub
    username: "{{ env.HVR_USERNAME }}"
    password: "{{ env.HVR_PASSWORD }}"
    api_version: "latest"                    # or "v6.3.5" to pin
    verify_ssl: true

  channel_selector:
    by_pattern: [sales_*, orders_*]          # discover matching channels
    exclude_by_pattern: [*_test]

  group_name: hvr
  action: noop                                # or 'refresh' to trigger POST /channels/{c}/refresh on materialize
  polling_sensor: true
  observation_interval_seconds: 300
  freshness_lag_threshold_seconds: 900       # 15-minute SLA
```

Environment:

```bash
export HVR_HUB_URL=http://hvr-hub.internal:4340
export HVR_HUB_NAME=prod_hub
export HVR_USERNAME=hvradmin
export HVR_PASSWORD='<your-hub-password>'
```

Then:

```bash
uv run dg dev
```

Open http://localhost:3000 — every replicated table is in the catalog.
The polling sensor is running (`prod_hub_hvr_observer`); observations
land in each asset's history every 5 min with `integrate_lag_seconds` in
metadata. The SLA check surfaces green/red per asset.

## What each YAML knob does

| Knob | Effect |
|---|---|
| `workspace: {…}` | Auth block — canonical `{{ env }}`-templated. Mirrors `dagster-fivetran` shape. |
| `channel_selector` | Filter which channels get discovered. Include (`by_name`, `by_pattern`) + exclude (`exclude_by_name`, `exclude_by_pattern`). |
| `translation` | Python callable — override per-asset `AssetSpec` (rename key, add tags, override group). |
| `action: noop` | Default. External assets, HVR CDC is continuous, materialization is a no-op. |
| `action: refresh` | Assets become **materializable** — click one → posts `/channels/{c}/refresh` + polls to completion. Fivetran-style. |
| `polling_sensor: true` | Emit `{hub_name}_hvr_observer` polling `/jobs?fetch=latency`. Every 5 min → one `AssetObservation` per (channel × target × table) with lag metadata. |
| `freshness_lag_threshold_seconds: N` | Emit `integrate_lag_within_sla` per-asset check that fails when last observed lag > N. |
| `defs_state` | State backend for cached discovery. Local FS by default; override to Dagster Cloud state store per-deploy. |

## Custom translation

For per-channel routing, tag application, or key restructuring:

```python
# my_project/hvr_translation.py
from dagster_community_components import HvrObjectProps
from dagster import AssetSpec, AssetKey


def tag_by_channel(props: HvrObjectProps) -> AssetSpec:
    return AssetSpec(
        key=AssetKey(["warehouse", props.target_loc, props.object_name]),
        tags={"hvr_channel": props.channel, "sla_tier": "gold" if props.channel.startswith("sales_") else "silver"},
        kinds={"hvr", "cdc", props.channel},
    )
```

```yaml
attributes:
  workspace: {...}
  translation: "{{ load_python_module_attr('my_project.hvr_translation.tag_by_channel') }}"
```

## StateBackedComponent

Discovery hits the REST API at `write_state_to_path` (initial load or
`dg utils refresh-defs-state`) and caches the channel/table snapshot to
disk. `build_defs_from_state` reads the cache on every subsequent
code-location reload — no HVR HTTP call, instant load. Same pattern as
`FivetranWorkspace` and `QlikReplicateWorkspaceComponent`.

Refresh the catalog explicitly:

```bash
uv run dg utils refresh-defs-state
```

Or wire an automation to refresh on a schedule (per-deploy config on the
`defs_state` field).

## Trying it locally with the HVR eval Docker image

Fivetran ships a pre-configured `fivetraninc/hvrpov` eval image (HVR Hub
+ Postgres repo). Explicitly evaluation-only:

```bash
docker pull fivetraninc/hvrpov
docker run -d --name hvr-eval --platform linux/amd64 -p 4340:4340 fivetraninc/hvrpov
sleep 5
docker exec -u hvr -d hvr-eval hvrhubserver
# HVR Hub REST API is now live at http://localhost:4340
```

Bootstrapping the eval Hub (creating an admin, initializing the repo,
defining a channel with real source/target DBs) is a full HVR domain
workflow — see [Fivetran's Quick Start Guide](https://fivetran.com/docs/hvr6/getting-started/quick-start-guide).
Not required for validating the component itself against auth + discovery.

## Version compatibility

Tested against 6.1.5.2 API surface. Also compatible with:

- **6.2.5** — docker eval image
- **6.3.5/2** — customer prod deployments as of 2026-08

Leave `api_version: latest` (default) unless you specifically need to pin
across Hub upgrades.

## What this component does NOT do (yet)

- **Snapshot channel config for drift detection** — roadmap: `hvr_definition_snapshot`.
- **Per-column asset lineage** — v1 is one asset per table.
- **HVR Alert / Event surface** — companion sensor components on the roadmap.

## Roadmap companion components

- **`hvr_channel_refresh`** — dedicated single-channel bulk-refresh trigger (already accessible via `action: refresh` on the workspace — this would break it out for per-channel scheduling).
- **`hvr_definition_snapshot`** — materialize channel definition JSON as a versioned asset (config drift detection).
- **`hvr_alert_sensor`** — poll HVR Alert Interface + emit Dagster asset failures.
