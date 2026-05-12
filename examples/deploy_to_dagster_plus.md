# Deploy any demo to Dagster+

One script. Handles auth, generates artifacts, optionally scaffolds GitHub Actions CI/CD, then deploys.

## TL;DR

```bash
# 1. Run any demo's setup script
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_kitchen_sink_demo.sh | bash

# 2. Deploy it to Dagster+
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/deploy_to_dagster_plus.sh \
  | bash -s kitchen-sink-demo
```

The script prompts you along the way. To accept defaults and skip prompts in CI:

```bash
curl -fsSL .../deploy_to_dagster_plus.sh | bash -s -- kitchen-sink-demo --non-interactive
```

## What the script does (6 steps)

```
1/6  Ensures dagster-cloud-cli is in dev deps                     (idempotent)
2/6  Runs `dg plus login` if not already authed                   (browser flow)
3/6  [prompts] Run `dg plus deploy configure --git-provider github`?
       Auto-detects your agent type (Serverless vs Hybrid) and scaffolds:
       → build.yaml                              (modern code-location manifest)
       → Dockerfile + container_context.yaml     (Hybrid only)
       → .github/workflows/*.yml                 (CI/CD)
4/6  [prompts, if .github/ exists] Create a CI API token?
       → Paste into GitHub secret DAGSTER_CLOUD_API_TOKEN
5/6  Confirms deployment target (org / deployment / build strategy)
6/6  [prompts] Run dg plus deploy now?
```

**Key:** step 3 uses the official `dg plus deploy configure` command — not hand-written YAML. That command auto-detects whether your Dagster+ workspace is Serverless or Hybrid, then writes the exact artifacts the official tooling expects. If you've migrated from a `dagster_cloud.yaml`-based project, the new build.yaml replaces it.

## Prerequisites

- **A Dagster+ account** — free 30-day trial at <https://dagster.io/plus>.
- **`uv` + `uvx`** — already on your machine from running any demo.
- A demo project on disk (created by one of the `setup_*_demo.sh` scripts).

## Build strategy is auto-detected

The script doesn't pass `--build-strategy` by default. `dg plus deploy` auto-detects your agent type from the deployment and picks:

| Agent type (in your Dagster+ workspace) | Build strategy | Notes |
|---|---|---|
| Serverless | `python-executable` (PEX) | ~60-120s deploys, no Docker needed |
| Hybrid (your k8s) | `docker` | Builds + pushes a Docker image to a registry your agent can pull from |

You can force a specific strategy with `--build-strategy docker` or `--build-strategy python-executable` if you have a reason.

## Optional flags

```bash
./deploy_to_dagster_plus.sh kitchen-sink-demo \
  --organization my-org-slug \
  --deployment branch-feature-x \
  --build-strategy docker \
  --non-interactive \
  --with-ci             # scaffold GitHub Actions without prompting
```

| Flag | Default | When to set |
|---|---|---|
| `--organization` | from `dg plus login` | Override the org chosen at login time |
| `--deployment` | `prod` | Deploy to a branch deployment or a non-prod env |
| `--build-strategy` | auto-detect | Force `docker` or `python-executable` |
| `--non-interactive` | (interactive) | Skip all prompts — accept defaults. Use in CI. |
| `--with-ci` | (off) | Generate the GitHub Actions workflows without asking. Useful for CI bootstrap. |

## Artifacts the script creates (via `dg plus deploy configure`)

For **Serverless** deployments:

- `build.yaml` — the modern code-location manifest (replaces older `dagster_cloud.yaml`)
- `.github/workflows/*.yml` — deploy on push + branch deployments on PR

For **Hybrid** deployments:

- `build.yaml` + `Dockerfile` + `container_context.yaml`
- `.github/workflows/*.yml`

The exact contents come from the official `dg plus deploy configure` command, so they stay current with whatever Dagster+ expects. The script doesn't hand-write these.

## The CI API token

If you opt in to step 5, the script runs `dg plus create ci-api-token` and prints the token once. Copy it immediately — Dagster+ won't show it again.

Then in GitHub:

1. Your repo → Settings → Secrets and variables → Actions → New repository secret
2. Name: `DAGSTER_CLOUD_API_TOKEN`
3. Value: the token

After that, your CI workflows can deploy without further prompts.

## After the deploy

Open your workspace:

```bash
open "https://<your-org>.dagster.cloud/prod"
```

The first run will take longer (cold-start the agent). Subsequent runs reuse the container.

## Caveats: which demos work as-is on Serverless

| Demo type | Status |
|---|---|
| Pure synthetic-data demos (kitchen_sink, data_hygiene, x12_edi, etc.) | ✅ Work directly |
| Public-API demos (USGS earthquakes, SpaceX, weather, NBA scoreboard) | ✅ Work directly |
| Demos requiring env vars (DATABASE_URL, API keys) | ⚠️ Set via `dg plus create env` before deploying |
| Demos using local `/tmp/...` paths for cross-asset file passing | ⚠️ Work within one Serverless run, files vanish after — see [the disk-IO deployment note](https://github.com/eric-thomas-dagster/dagster-component-templates/blob/main/assets/ai/synthetic_image_generator/README.md#%EF%B8%8F-deployment-note-dagster--kubernetes) on every affected component |
| Demos with local-server fixtures (email_roundtrip's IMAP stub) | ❌ Re-point at real services before deploying |
| Cloud-credential demos (azure_*, bigquery_*) | ✅ Work — set creds via `dg plus create env` |

To add an env var to your deployment:

```bash
uv run dg plus create env API_KEY --value "..." --deployment prod
uv run dg plus create env DATABASE_URL --value "..." --deployment prod
```

## Troubleshooting

**`No Dagster+ config found`** — Re-run `uv run dg plus login` manually first.

**`Build failed: missing requirements.txt`** — Some demos forget `uv add <pkg>` for runtime deps. Check the demo's setup script.

**`DagsterInvariantViolationError in code-location loading`** — A component referenced an env var that isn't set. Use `dg plus create env` to add it.

**`Asset materialization failed: timeout`** — Serverless agents have a default 1h step timeout. Configure via run-config tags if you need longer.

**Hybrid build wants Docker but you don't have a registry** — Set `--build-strategy python-executable` to force PEX. Note: PEX deploys won't work with Hybrid agents in production; you'll need a registry eventually.

## Full reference

The script wraps the official `dg plus` CLI:

- <https://docs.dagster.io/api/clis/dg-cli/dg-plus> — top-level
- <https://docs.dagster.io/api/clis/dg-cli/dg-plus#login>
- <https://docs.dagster.io/api/clis/dg-cli/dg-plus#deploy>
- <https://docs.dagster.io/api/clis/dg-cli/dg-plus#create>

For deep CI/CD customization (matrix builds, deploy hooks, GitHub Status checks), see [dagster-cloud-action](https://github.com/dagster-io/dagster-cloud-action) — the official source for the workflow templates the script generates.
