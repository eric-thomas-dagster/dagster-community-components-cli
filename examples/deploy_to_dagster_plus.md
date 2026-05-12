# Deploy any demo to Dagster+

You ran a demo locally with `curl … | bash`. Now you want it running on Dagster+ Serverless. This guide ships a single script that handles the auth + deploy in one shot.

## TL;DR

```bash
# 1. Run any demo's setup script
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_kitchen_sink_demo.sh | bash

# 2. Deploy it to Dagster+
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/deploy_to_dagster_plus.sh \
  | bash -s kitchen-sink-demo
```

That's it. The script:

1. Ensures `dagster-cloud-cli` is in the project's dev deps
2. Runs `dg plus login` if you aren't already logged in (opens a browser tab)
3. Runs `dg plus deploy --build-strategy python-executable` (no Docker required)

Within ~60s your demo is live at `https://<your-org>.dagster.cloud/prod`.

## Prerequisites

- **A Dagster+ account** — free 30-day trial at <https://dagster.io/plus>. Pick the **Serverless** offering; the script defaults to that.
- **`uvx` + `uv` on your machine** — you already have these from running any demo setup.
- A demo project already created locally via one of the `setup_*_demo.sh` scripts.

## Script flow (4 steps)

### 1. Verify the project

The script checks that `<project_dir>/pyproject.toml` contains `[tool.dg]` — i.e. it's a Dagster project. If not, you get a friendly error.

### 2. Add `dagster-cloud-cli` to dev deps

```bash
uv add --dev dagster-cloud-cli
```

Idempotent — skipped if already present.

### 3. Authenticate (`dg plus login`)

If you've never run `dg plus login` on this machine, the script triggers it. It opens a browser tab, you confirm the org/region, the token gets saved to `~/.dagster_cloud_cli/config`. One-time setup per machine.

If you're already logged in (`dg plus config view` returns config), the script prints the existing settings and skips login.

### 4. Deploy (`dg plus deploy`)

```bash
uv run dg plus deploy --build-strategy python-executable
```

`python-executable` = PEX build (Serverless agent default). Much faster than Docker — typically 60-120s end-to-end, vs. 5-10 min for Docker.

For Dagster+ Hybrid (your own k8s agent), pass `--build-strategy docker`. You'll need Docker running locally + a container registry the agent can pull from.

## Optional flags

```bash
./deploy_to_dagster_plus.sh kitchen-sink-demo \
  --organization my-org-slug \
  --deployment branch-feature-x \
  --build-strategy docker
```

| Flag | Default | When to set |
|---|---|---|
| `--organization` | from `dg plus login` | Override the org chosen at login time |
| `--deployment` | `prod` | Deploy to a branch deployment instead of prod |
| `--build-strategy` | `python-executable` | Use `docker` for Hybrid |

## What happens after deploy

1. The script returns.
2. Dagster+ runs the code-location bootstrap (you see "Loading definitions" in the UI).
3. Your assets show up in the asset graph.
4. You can materialize them via the UI or `uv run dg launch --assets '*'` (which now hits the cloud agent instead of your laptop).

## Caveats: which demos work as-is on Serverless

| Demo type | Status |
|---|---|
| Pure synthetic-data demos (kitchen_sink, data_hygiene, x12_edi, etc.) | ✅ Work directly |
| Public-API demos (USGS earthquakes, SpaceX, weather, etc.) | ✅ Work directly |
| Demos requiring env vars (DATABASE_URL, API keys) | ⚠️ Set via `dg plus create env` before deploying |
| Demos using local `/tmp/...` paths for cross-asset file passing | ⚠️ Work within a single run, files vanish after — see the [Deployment note](https://github.com/eric-thomas-dagster/dagster-community-components-cli/blob/main/examples) on affected components |
| Demos with local-server fixtures (email_roundtrip, mqtt, etc.) | ❌ Need to be re-pointed at real services |
| Cloud-credential demos (azure_sql, bigquery_*) | ✅ Work — just set env vars via `dg plus create env` |

For env vars:

```bash
uv run dg plus create env API_KEY --value "..." --deployment prod
uv run dg plus create env DATABASE_URL --value "..." --deployment prod
# Then deploy:
uv run dg plus deploy --build-strategy python-executable
```

## Troubleshooting

**"No Dagster+ config found"** — Run `uv run dg plus login` manually first.

**"Build failed: missing requirements.txt"** — Some demos forget `uv add <pkg>` for runtime deps. Check the demo's setup script for any `uv add` lines that didn't make it.

**"DagsterInvariantViolationError in code-location loading"** — A component referenced an env var that isn't set. Use `dg plus create env` to add it.

**"Asset materialization failed: timeout"** — Serverless agents have a default 1h step timeout. For longer-running steps, configure `MaxConcurrencyPerStep` in the run config.

## Full reference

The script wraps the official `dg plus` CLI. Underlying docs:
- <https://docs.dagster.io/api/clis/dg-cli/dg-plus>
- <https://docs.dagster.io/api/clis/dg-cli/dg-plus#login>
- <https://docs.dagster.io/api/clis/dg-cli/dg-plus#deploy>
- <https://docs.dagster.io/api/clis/dg-cli/dg-plus#create>

For deep customization (branch deployments, CI/CD workflows, GitHub PR integration, etc.), run `uv run dg plus deploy --help` or read the docs above.
