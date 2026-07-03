# Vercel Deployment — Sensor + Catalog Demo

**Components:**
- `VercelDeploymentSensorComponent` (`sensors/vercel_deployment_sensor`)
- `ExternalVercelDeploymentAsset` (`external_assets/external_vercel_deployment`)

**Script:** [`setup_vercel_deployment_demo.sh`](./setup_vercel_deployment_demo.sh)
**Cost:** $0 (Vercel deployment reads are free)
**Duration:** ~10 seconds to scaffold + one live API roundtrip
**Validated:** 2026-07-02 — live sensor tick against the `dagster-component-ui` production deployment on Vercel returned:
```
vercel_deployment_uid: dpl_FLcdDB3LsjzrtaLGadkPvBTpqyhr
vercel_state:          READY
vercel_target:         production
vercel_deployment_url: https://dagster-component-72gt43rtf-ethomasiis-projects.vercel.app
vercel_commit_sha:     004a5fce2d48df0d14a8544bc8e9e8a7b6af2237
vercel_branch:         main
vercel_commit_message: Tier search results: vendors, then resources, I/O managers, templates
```

## What it demonstrates

Vercel is itself the deploy orchestrator — you don't want Dagster starting builds. What Dagster *does* care about is **when a production deployment goes green**, so downstream data assets can react: post-deploy smoke tests, CDN warmers, snapshotted-content ingestion, analytics ETL that reads from the new prod URL.

This demo wires that pattern with two components:

```
┌───────────────────────────────┐    poll /v6/deployments    ┌────────────────┐
│ vercel_deployment_sensor      │ ─────── every 60s ───────▶ │ api.vercel.com │
└───────────────────────────────┘                            └────────────────┘
        │ AssetObservation on READY
        ▼
┌───────────────────────────────────┐
│ external_vercel_deployment        │ ◀── downstream Dagster
│ (declare-only AssetSpec in the    │      assets can deps:
│  catalog with clickable URLs)     │      ["vercel/site/production"]
└───────────────────────────────────┘
```

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `VERCEL_API_TOKEN` — create at https://vercel.com/account/tokens with **Read Access** scope
- `VERCEL_PROJECT_ID` — from your Vercel project → Settings → General (format `prj_...`)

## Run

```bash
export VERCEL_API_TOKEN=vck_...
export VERCEL_PROJECT_ID=prj_...
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_vercel_deployment_demo.sh -o setup_vercel_deployment_demo.sh
chmod +x setup_vercel_deployment_demo.sh
./setup_vercel_deployment_demo.sh                        # → vercel_demo/
./setup_vercel_deployment_demo.sh my_vercel_proj         # custom name
```

The script:
1. Verifies your token can hit `https://api.vercel.com/v6/deployments`.
2. Scaffolds a fresh Dagster project via `uvx create-dagster project`.
3. Installs `dagster-community-components` + `requests`.
4. Writes two `defs.yaml`:
   - `external/defs.yaml` — the external asset declaring the deployment stream.
   - `sensor/defs.yaml` — the polling sensor.
5. Runs one sensor tick against your real Vercel account and prints the observed deployment.

## The two defs

External asset (declare-only catalog placement):

```yaml
type: dagster_community_components.ExternalVercelDeploymentAsset
attributes:
  asset_key: vercel/site/production
  project_id: prj_...
  target: production
  group_name: vercel
```

Sensor (polls and emits observations):

```yaml
type: dagster_community_components.VercelDeploymentSensorComponent
attributes:
  sensor_name: vercel_prod_ready
  project_id: prj_...
  target: production
  api_token_env_var: VERCEL_API_TOKEN
  job_name: __ASSET_JOB
  asset_key: vercel/site/production
  asset_event_type: observation
  minimum_interval_seconds: 60
```

## What lands in Dagster

On a live tick:
- **Run request** with tags: `vercel/deployment_uid`, `vercel/state`, `vercel/target`, `vercel/commit_sha`, `vercel/branch`, `vercel/deployment_url`. Fires the job on the terminal-success cursor advance.
- **AssetObservation** on `vercel/site/production` with metadata: `vercel_deployment_uid`, `vercel_state=READY`, `vercel_deployment_url` (clickable), `vercel_commit_sha`, `vercel_branch`, `vercel_commit_message`, `vercel_created_at`.

## Extension patterns

- **Post-deploy smoke tests.** Add a Dagster job that runs Playwright/pytest against the deployed URL; wire `job_name: post_deploy_smoke_tests_job` on the sensor.
- **CDN warmup.** Downstream asset with `deps: ["vercel/site/production"]` fetches `/sitemap.xml` + hits the top N routes to prime the edge.
- **Preview environment analytics.** Add a second sensor with `target: preview` and a different `asset_key` to track preview URLs feeding a preview-analytics ETL.
- **Alert on failures.** Set `error_state_triggers_job: true` and route to a Slack/PagerDuty notification job.

## Bugs surfaced during validation

1. **Initial component used Vercel `/v13/deployments`.** Vercel's stable Deployments API is `/v6` — `/v13` returns `400 Invalid API version`. Fixed.
2. **External asset materialization via CLI.** `dagster asset materialize --select vercel/site/production` failed with "Selected keys must be a subset of existing executable asset keys" — because `AssetSpec` is declare-only. Demo now invokes the sensor via a small python program instead.

## Related

- [`vercel_ai_gateway_agent`](./vercel_ai_gateway_agent.md) — LLM agent via Vercel AI Gateway (separate credential required).
- [`temporal_workflow_sensor`](./temporal_workflow.md) — same observation pattern for Temporal workflows.
