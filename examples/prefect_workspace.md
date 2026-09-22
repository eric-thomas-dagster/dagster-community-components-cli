# Prefect Workspace — Auto-Discover Every Deployment, Zero Per-Flow YAML

> ❌ **Dagster+ Serverless / Hybrid:** this walkthrough is local-only — the setup script starts its own local Prefect server for zero-credential reproducibility, and Serverless/Hybrid can't run that for you. The component itself has no such requirement: point `workspace.api_url` (+ `api_key_env_var`) at Prefect Cloud or any reachable Prefect server and the same YAML deploys to Dagster+ as-is — see "Switch to Prefect Cloud" below.

`PrefectFlowRunAssetComponent` is one YAML block per Prefect deployment. That's
fine for a handful of flows. It's a real tax once a Prefect estate has dozens —
every new deployment means another hand-written `defs.yaml`. `PrefectWorkspaceComponent`
removes that tax: point it at a Prefect API, and it enumerates every deployment
itself, emitting one triggerable Dagster asset per deployment, with the same
observability richness (`stream_logs`, `stream_artifacts`, cancellation
forwarding) applied uniformly — no per-deployment config required for any of
that.

Two things are deliberately **not** uniform, and this demo proves both:

- **Asset checks** (`check_names`) are opt-in per deployment via `assets_by_name`
  — a global check name would be required on every discovered deployment, most
  of which won't emit that exact artifact convention.
- **Dagster-side scheduling** (`auto_schedule` + `assets_by_name.<name>.schedule: true`)
  is opt-in per deployment too, and is never inferred from whether Prefect's own
  cron happens to be paused. This demo pauses one deployment's Prefect-side cron
  explicitly, then opts it into a mirrored Dagster `ScheduleDefinition` — the
  only way `auto_schedule` will ever take a deployment over.

## The story

Two Prefect flows are already deployed and running against a local Prefect
server: `daily_etl` (has its own cron schedule) and `nightly_report` (writes a
table artifact recording a row-count check). Neither has ANY Dagster-specific
code or YAML written for it individually — `PrefectWorkspaceComponent` discovers
both through the Prefect API and builds:

- `prefect/daily_etl_main` — triggerable asset, plus a mirrored Dagster
  `ScheduleDefinition` (opted in explicitly, and only after its Prefect-side
  cron is paused).
- `prefect/nightly_report_main` — triggerable asset with one declared
  `AssetCheckSpec` (`row_count_check`), fed by the `row-count-check` table
  artifact the flow writes on every run.

Add a third Prefect deployment tomorrow and it shows up in `dg check defs`
with zero new YAML — that's the entire pitch.

## Architecture

```
  Prefect (:4200 server, 2 deployments already live)   │   Dagster
──────────────────────────────────────────────────────  │  ──────────────────────────────────────
                                                        │
  daily_etl/main                                        │  type: PrefectWorkspaceComponent
    cron: "0 2 * * *"  (paused by setup script)          │  attributes:
                                                        │    workspace: {api_url: :4200}
  nightly_report/main                                    │    stream_logs: true
    writes table artifact key="row-count-check"           │    stream_artifacts: true
    data: [{passed, row_count, min_rows}]                │    auto_schedule: true
                                                        │    assets_by_name:
        │                                               │      daily_etl/main: {schedule: true}
        │           enumerated via read_deployments()   │      nightly_report/main:
        └──────────────────────────────────────────────►│        check_names: [row_count_check]
                                                        │              │
                                                        │              ▼  (discovery, cached to disk)
                                                        │   prefect/daily_etl_main        (asset)
                                                        │   prefect_daily_etl_main_schedule (schedule, mirrors the paused cron)
                                                        │   prefect/nightly_report_main   (asset + row_count_check AssetCheckSpec)
```

**No per-deployment `defs.yaml`.** One `PrefectWorkspaceComponent` block
produces both assets, both bearing full observability, plus the one schedule
and the one check — both opted in by name, nothing blanket.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_prefect_workspace_demo.sh \
  -o setup_prefect_workspace_demo.sh
bash setup_prefect_workspace_demo.sh
```

Requirements: `uv`. **No API keys.** ~2 min first run (installs Prefect +
starts a local Prefect server).

The setup script:
1. Starts a **local Prefect server** in background at `:4200`.
2. Serves **two** Prefect flows at once (`daily_etl/main`, `nightly_report/main`)
   via a single `serve()` worker process — `daily_etl` is deployed with a cron
   schedule, `nightly_report` writes a `row-count-check` table artifact on
   every run.
3. Pauses `daily_etl/main`'s Prefect-side cron — the explicit precondition
   `auto_schedule` checks for before it will let `assets_by_name.daily_etl/main.schedule: true`
   mirror it into Dagster (this is a safety check, not the trigger — the
   `schedule: true` entry is).
4. Scaffolds a Dagster project with **one** `PrefectWorkspaceComponent` block —
   no per-deployment YAML.
5. Runs `dg check defs` to show both deployments were discovered automatically,
   plus the one mirrored schedule.
6. Materializes both discovered assets and prints the streamed logs, the
   forwarded artifact, and the `row_count_check` result.

## Watch both UIs side by side

- **Prefect UI**: http://127.0.0.1:4200 — Deployments tab shows `daily_etl/main`
  (schedule paused) and `nightly_report/main`. Flow Runs tab shows the runs
  Dagster triggered. Click into `nightly_report`'s run → Artifacts tab shows
  the `row-count-check` table.
- **Dagster UI**: `cd $PROJECT_DIR && uv run dg dev` at http://localhost:3000 —
  Assets tab shows `prefect/daily_etl_main` and `prefect/nightly_report_main`,
  both auto-discovered from the same component block. Click
  `nightly_report_main` → Checks tab shows `row_count_check` (pass/fail +
  `row_count`/`min_rows` metadata pulled straight from the artifact). Automation
  tab shows the one mirrored `ScheduleDefinition` for `daily_etl_main`. Run logs
  for either asset include the forwarded Prefect log lines (`stream_logs`).

## The component used

- [`PrefectWorkspaceComponent`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/integrations/prefect_workspace)
  — the whole demo is one block of this. Everything else (`PrefectResourceComponent`,
  `PrefectFlowRunAssetComponent`, `PrefectFlowRunSensorComponent`) is the
  per-deployment alternative this component exists to remove the tax of; see
  [`dagster_orchestrates_prefect.md`](dagster_orchestrates_prefect.md) for that
  hand-configured version side by side.

## Switch to Prefect Cloud

```yaml
type: dagster_community_components.PrefectWorkspaceComponent
attributes:
  workspace:
    api_url: https://api.prefect.cloud/api/accounts/<acct-id>/workspaces/<ws-id>
    api_key_env_var: PREFECT_API_KEY
  deployment_selector:
    by_pattern: ["*/production"]
  stream_logs: true
  stream_artifacts: true
```

Set `PREFECT_API_KEY` in your shell before `dg dev`. `deployment_selector` is
optional — narrow discovery with `by_name` / `by_pattern` /
`exclude_by_name` / `exclude_by_pattern` on `"flow_name/deployment_name"`
strings when a workspace has deployments you don't want Dagster to see at all.

## When to reach for this pattern

- **You already have a Prefect estate with more than a couple of deployments**
  and want Dagster's catalog to reflect all of them without hand-writing a
  YAML block per flow. New deployments show up on the next state refresh.
- **You want uniform observability across a whole Prefect instance** —
  `stream_logs`/`stream_artifacts`/`forward_termination` apply to every
  discovered deployment the moment they're turned on, with no per-deployment
  wiring.
- **A few specific deployments need an asset check or a Dagster-owned
  schedule** — `assets_by_name` opts those in by exact name, leaving every
  other discovered deployment untouched.

## When NOT to reach for this pattern

- **You have one or two Prefect deployments** and want fine control over each
  one's asset shape (custom `deps`, per-deployment `partition_type`, etc.) —
  `PrefectFlowRunAssetComponent` gives you that per block; the workspace
  component optimizes for "discover everything," not per-asset customization
  beyond what `assets_by_name` covers.
- **Every deployment needs Dagster-side scheduling** — `auto_schedule` still
  requires naming each one explicitly in `assets_by_name`, by design (see the
  module docstring in `component.py` on why a blanket switch was rejected).
  If that feels like busywork for a workspace with 50 scheduled deployments,
  that's the signal this pattern isn't the right fit for that subset — use
  Prefect's own scheduler for those and let this component's asset stay
  triggerable-only.

## Files worth reading in the scaffolded project

- `prefect_worker/flows.py` — both Prefect flows, served together. Note there's
  no Dagster awareness in this file at all — it's an ordinary Prefect worker.
- `src/<pkg>/defs/prefect_workspace/defs.yaml` — the entire Dagster side of
  this demo. One `PrefectWorkspaceComponent` block; compare its size to
  `dagster_orchestrates_prefect.md`'s per-deployment `defs.yaml` for the same
  kind of asset.

## See also

Browse the [walkthrough index](README.md) for related demos across every
component family, especially
[`dagster_orchestrates_prefect.md`](dagster_orchestrates_prefect.md) for the
per-deployment `PrefectFlowRunAssetComponent` this component sits alongside.
