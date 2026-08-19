# HVR Hub Workspace — standalone HVR 6.x → Dagster external assets

> One YAML wires the whole HVR Hub. Every replicated table shows up as a
> Dagster asset with lineage + freshness telemetry. Fivetran-shape.
> Ships with a fully-runnable demo (mock HVR Hub → Dagster catalog in
> one command, no license required).

**Component:** `dagster_community_components.HvrHubWorkspaceComponent`
**Setup script:** `setup_hvr_hub_workspace_demo.sh`
**Validation:** `live` (mock Hub end-to-end; drop-in for a real Hub via env-var swap)

## Try it in one command

```bash
bash setup_hvr_hub_workspace_demo.sh
```

That does everything: spins up a mock HVR Hub REST server on
`localhost:4340`, scaffolds a Dagster project, installs
`hvr_hub_workspace`, points it at the mock Hub, refreshes discovery, and
prints the resulting asset catalog. Takes ~30 seconds.

What you get:

```
Assets  ── hvr/demo_hub/sales_cdc/target_snowflake_dw/orders
        ── hvr/demo_hub/sales_cdc/target_snowflake_dw/customers
        ── hvr/demo_hub/orders_stream/target_snowflake_dw/events
        ── hvr/demo_hub/orders_stream/target_snowflake_dw/clicks
        ── hvr/demo_hub/inventory_cdc/target_snowflake_dw/stock_levels
                                    ↑
        (test_ephemeral is filtered out by channel_selector)

Sensor  ── demo_hub_hvr_observer         (polls integrate-lag every 60s)
Checks  ── integrate_lag_within_sla × 5  (one per asset)
```

Open the Dagster UI:

```bash
cd hvr-hub-demo && source .env.demo
uv run dg dev
```

## What this component is

Auto-discovers every channel + replicated table on a **standalone HVR
Hub 6.x** server and emits one Dagster asset per (channel × target
location × table). Optional polling sensor emits AssetObservations with
integrate-lag metadata. Optional per-asset check fails when lag exceeds
your SLA.

Fivetran-shape — same YAML shape as `dagster-fivetran`, same fields
(`workspace`, `channel_selector`, `translation`, `polling_sensor`),
same `StateBackedComponent` lifecycle (discovery cached on disk;
refresh explicitly).

## Which HVR do you have?

Ask before installing anything:

> *"Is your HVR through the Fivetran dashboard, or a standalone HVR
> Hub install with a `.hvr` config directory?"*

| Answer | Use |
|---|---|
| **Fivetran dashboard** (HVR Enterprise tier managed by the Fivetran platform) | Official [`dagster-fivetran`](https://docs.dagster.io/integrations/fivetran) |
| **Standalone HVR Hub** (`hvrhubserver` on your own hardware) | This component |

The two products share a name and a corporate parent but different REST
APIs, auth flows, and Dagster surfaces.

## Point at your real Hub

Only 4 env vars change:

```bash
export HVR_HUB_URL=https://your-hub:4340
export HVR_HUB_NAME=prod_hub
export HVR_USERNAME=hvradmin
export HVR_PASSWORD='<your-hub-password>'
```

`defs.yaml` stays exactly as the setup script wrote it — it already
uses `{{ env.HVR_HUB_URL }}` etc.

```bash
uv run dg utils refresh-defs-state    # pulls channels + tables from your Hub
uv run dg dev                          # UI at http://localhost:3000
```

## Full defs.yaml

```yaml
type: dagster_community_components.HvrHubWorkspaceComponent
attributes:
  workspace:
    hub_url:  "{{ env.HVR_HUB_URL }}"
    hub_name: "{{ env.HVR_HUB_NAME }}"
    username: "{{ env.HVR_USERNAME }}"
    password: "{{ env.HVR_PASSWORD }}"
    api_version: "latest"                    # or pin to "v6.3.5"
    verify_ssl: true

  # Filter which channels get discovered. Mirrors FivetranWorkspace.connector_selector.
  channel_selector:
    by_pattern: [sales_*, orders_*]
    exclude_by_pattern: [*_test]

  group_name: hvr
  # asset_key_prefix: ["hvr", "prod_hub"]    # default ['hvr', <hub_name>]
  # kinds: [hvr, cdc]                         # default
  # tags: {tier: production}
  # owners: [data-platform@acme.com]

  # What happens when a user clicks materialize.
  action: noop                                # or 'refresh' → POST /channels/{c}/refresh + poll
  # wait_for_completion: true                 # only when action=refresh
  # poll_interval_seconds: 30
  # timeout_seconds: 3600

  # Optional polling sensor — emits AssetObservations with integrate-lag metadata.
  polling_sensor: true
  observation_interval_seconds: 300

  # Optional per-asset check — fails when last observed lag > threshold.
  freshness_lag_threshold_seconds: 900

  # Optional custom translation callable — override key / tags / group / kinds
  # per asset (see "Custom translation" below).
  # translation: "{{ load_python_module_attr('my_project.hvr_translation.tag_by_channel') }}"
```

## How each knob behaves

| Knob | Effect |
|---|---|
| `workspace.*` | Connection block (mirrors `dagster-fivetran` shape). `{{ env }}`-templated. |
| `channel_selector` | Include/exclude filter. `by_name`, `by_pattern`, `exclude_by_name`, `exclude_by_pattern` (globs). Exclusion wins. |
| `action: noop` (default) | Assets are external. HVR CDC is continuous — there's genuinely nothing to trigger. |
| `action: refresh` | Assets become materializable. Clicking one posts `/channels/{c}/refresh` and polls until integrate catches up. Fivetran-style. |
| `polling_sensor: true` | Emits `{hub_name}_hvr_observer` sensor. Every `observation_interval_seconds`, calls `GET /jobs?fetch=latency` and writes one AssetObservation per (channel × target × table) with `integrate_lag_seconds`, `state`, `job_name`, `observed_at`. |
| `freshness_lag_threshold_seconds` | Emits `integrate_lag_within_sla` per-asset check. Pass = last observation ≤ threshold; fail = above. Wire into an asset-checks-first schedule or alert. |
| `translation` | Python callable path — customize per-asset AssetSpec (rename key, add tags, override group). Same convention as `dagster-fivetran` and `dagster-databricks`. |

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Standalone HVR Hub 6.x (customer-hosted)          │
│                                                                      │
│  ┌────────────┐    ┌────────────┐    ┌────────────┐                  │
│  │ channel:   │    │ channel:   │    │ channel:   │                  │
│  │ sales_cdc  │    │orders_strm │    │inventory   │                  │
│  │ ├─ orders  │    │ ├─ events  │    │ └─ stock   │                  │
│  │ └─ custom… │    │ └─ clicks  │    │            │                  │
│  └────────────┘    └────────────┘    └────────────┘                  │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ REST (bearer JWT)
                                  ▼
       ┌─────────────────────────────────────────────────────────┐
       │            HvrHubWorkspaceComponent (one YAML)          │
       │                                                         │
       │  StateBackedComponent:                                  │
       │    write_state_to_path → REST discovery → JSON on disk  │
       │    build_defs_from_state → reads cache, no HTTP         │
       │                                                         │
       │  polling_sensor → GET /jobs?fetch=latency every 5 min   │
       │  action=refresh → POST /channels/{c}/refresh on click   │
       └────────────────────────────────┬────────────────────────┘
                                        │
                                        ▼
       Dagster catalog + graph:
         hvr/prod_hub/sales_cdc/target_snowflake_dw/orders
         hvr/prod_hub/sales_cdc/target_snowflake_dw/customers
         hvr/prod_hub/orders_stream/target_snowflake_dw/events    …
         prod_hub_hvr_observer (sensor)
         integrate_lag_within_sla × N (asset checks)
```

## Custom translation

For per-channel routing, tag application, or asset-key restructuring:

```python
# my_project/hvr_translation.py
from dagster_community_components import HvrObjectProps
from dagster import AssetSpec, AssetKey


def tag_by_channel(props: HvrObjectProps) -> AssetSpec:
    tier = "gold" if (props.channel or "").startswith("sales_") else "silver"
    return AssetSpec(
        key=AssetKey(["warehouse", props.target_loc, props.object_name]),
        tags={"hvr_channel": props.channel or "?", "sla_tier": tier},
        kinds={"hvr", "cdc", props.channel or "unknown"},
    )
```

```yaml
attributes:
  workspace: {...}
  translation: "{{ load_python_module_attr('my_project.hvr_translation.tag_by_channel') }}"
```

## StateBackedComponent

Discovery hits the REST API only on `write_state_to_path` (initial load or
`dg utils refresh-defs-state`). Every subsequent code-location reload
reads the cached snapshot — no HVR HTTP calls, instant load. Same pattern
as `FivetranWorkspace` and `QlikReplicateWorkspaceComponent`.

Refresh the catalog explicitly:

```bash
uv run dg utils refresh-defs-state
```

Or wire an automation to refresh on a schedule (per-deploy config on the
`defs_state` field).

## What it hits (REST endpoints)

```
POST  /auth/v1/password                                        — bearer JWT
GET   /api/{ver}/hubs/{hub}/definition/channels                — list channels
GET   /api/{ver}/hubs/{hub}/definition/channels/{c}/tables     — tables per channel
GET   /api/{ver}/hubs/{hub}/definition/channels/{c}/loc_groups — locations per channel
GET   /api/{ver}/hubs/{hub}/jobs?fetch=latency                 — integrate lag per job
POST  /api/{ver}/hubs/{hub}/channels/{c}/refresh               — when action=refresh
```

## Version compatibility

Tested API surface: HVR **6.1.5.2**. Also known to work with:

- **6.2.5** — Fivetran's docker eval image
- **6.3.5/2** — current customer prod deployments as of 2026-08

Leave `api_version: latest` (default) unless you specifically need to pin
across Hub upgrades.

## Why not use the Docker eval image for the demo?

Fivetran ships a `fivetraninc/hvrpov` eval Docker image (HVR Hub +
pre-configured Postgres repo) — you can absolutely spin it up and point
this component at it:

```bash
docker run -d --name hvr-eval --platform linux/amd64 -p 4340:4340 fivetraninc/hvrpov
sleep 5
docker exec -u hvr -d hvr-eval bash -lc 'hvrhubserver'
```

But **creating a hub against it requires a temporary eval license key
from Fivetran Support** (see the license notice in the image's
`/opt/hvr/hvr_home/www/License.txt`). Not a blocker for kicking the tires,
but too much friction for a one-command demo. The mock server in
`setup_hvr_hub_workspace_demo.sh` runs the same 5 endpoints the component
uses — enough for full end-to-end validation of the Dagster surface.

Once you have an eval license, the same defs.yaml points at the real
Hub with the 4 env-var swap.

## What this component does NOT do (yet)

- **Snapshot channel config for drift detection** — roadmap: `hvr_definition_snapshot`.
- **Per-column asset lineage** — v1 is one asset per table.
- **HVR Alert / Event surface** — companion sensor components on the roadmap.

## Roadmap companion components

- `hvr_channel_refresh` — dedicated single-channel bulk-refresh trigger
- `hvr_definition_snapshot` — versioned channel-definition asset for drift detection
- `hvr_alert_sensor` — poll HVR Alert Interface → emit asset failures
