# Deploy any demo to Dagster+

### 👋 New to Dagster+?

| | |
|---|---|
| 🚀 **Start free** | [Sign up for a 30-day trial](https://dagster.io/plus) — no credit card. Serverless agent included. |
| 💬 **Talk to sales** | [Contact Sales](https://dagster.io/contact-sales) — enterprise pricing, SSO, dedicated support, custom SLAs |
| 📖 **Learn more** | [Dagster+ overview](https://dagster.io/plus) · [Hybrid vs Serverless](https://docs.dagster.io/guides/deploy/dagster-plus) · [Pricing](https://dagster.io/pricing) |

---

> **Serverless-first.** This script is the easy button for Dagster+ Serverless: `curl | bash` → running demo in ~60 seconds.
>
> **For Hybrid deployments**, this script is helpful but not magical — it generates the artifacts (`build.yaml`, Dockerfile, container_context.yaml, CI workflows) and runs `dg plus deploy`, but assumes you already have a Hybrid agent running, a container registry provisioned, and Docker auth set up. See the [Hybrid section](#hybrid-additional-setup-required) below.

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

## What the script does (7 steps)

```
1/7  Ensures dagster-cloud-cli is in dev deps                     (idempotent)
2/7  Runs `dg plus login` if not already authed                   (browser OAuth)
       → Saves a USER token to ~/.dagster_cloud_cli/config.
       → THIS token is what powers the local deploy in step 7.
3/7  [prompts] Run `dg plus deploy configure --git-provider github`?
       Auto-detects your agent type (Serverless vs Hybrid) and scaffolds:
       → build.yaml                              (modern code-location manifest)
       → Dockerfile + container_context.yaml     (Hybrid only)
       → .github/workflows/*.yml                 (CI/CD)
4/7  [prompts, if .github/ exists] Create a CI API token?
       → A SEPARATE token for GitHub Actions runs (no browser available there).
       → Paste into GitHub secret DAGSTER_CLOUD_API_TOKEN.
       → Optional — skip this if you only deploy from your laptop.
5/7  Scans your project for env var references — and offers to set each one.
       → Greps for EnvVar("X"), os.environ["X"], *_env_var: X, ${env:X}.
       → For each detected name:
           - If already set in your shell: shows a preview + asks "Use this value? [Y/n/skip]"
           - If not set: prompts for a value (Enter to skip)
       → Each provided value runs `dg plus create env X --value … --deployment …`.
       → Scan only catches the most common patterns — set any others manually:
           uv run dg plus create env MY_VAR --value '...' --deployment prod
6/7  Confirms deployment target (org / deployment / build strategy)
7/7  [prompts] Run dg plus deploy now?  (uses your step-2 user token, NOT the CI token)
```

**Why two tokens?**

| Token | Lives in | Used by | Created by |
|---|---|---|---|
| **User login token** | `~/.dagster_cloud_cli/config` on your laptop | The local `dg plus deploy` from this script | `dg plus login` — step 2 |
| **CI API token** | GitHub repo secret `DAGSTER_CLOUD_API_TOKEN` | GitHub Actions runs scaffolded in step 3 | `dg plus create ci-api-token` — step 4 |

The CI token in step 4 is **optional for the current script run** — your `dg plus login` token from step 2 is what powers step 6. Step 4's token only matters if you want pushes to `main` to redeploy automatically via the GitHub Actions workflow.

**Key on step 3:** uses the official `dg plus deploy configure` command — not hand-written YAML. That command auto-detects whether your Dagster+ workspace is Serverless or Hybrid, then writes the exact artifacts the official tooling expects. If you've migrated from a `dagster_cloud.yaml`-based project, the new build.yaml replaces it.

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
  --agent-type hybrid \
  --registry-url 123456789012.dkr.ecr.us-east-1.amazonaws.com/my-repo \
  --agent-platform k8s \
  --non-interactive \
  --with-ci             # scaffold GitHub Actions without prompting
```

| Flag | Default | When to set |
|---|---|---|
| `--organization` | from `dg plus login` | Override the org chosen at login time |
| `--deployment` | `prod` | Deploy to a branch deployment or a non-prod env |
| `--build-strategy` | auto-detect | Force `docker` or `python-executable` |
| `--agent-type` | prompt | `serverless` or `hybrid` |
| `--registry-url` | prompt (Hybrid only) | Container registry URL for `docker push` / `docker pull` |
| `--agent-platform` | prompt (Hybrid only) | `k8s` / `ecs` / `docker` — selects the right `container_context.yaml` shape |
| `--git-provider` | `github` | `github` or `gitlab` — controls which CI workflow files get generated |
| `--non-interactive` | (interactive) | Skip all prompts — accept defaults. Use in CI. |
| `--with-ci` | (off) | Generate the workflows without asking. Useful for CI bootstrap. |

## Artifacts the script creates (via `dg plus deploy configure`)

For **Serverless** deployments:

- `build.yaml` — the modern code-location manifest (replaces older `dagster_cloud.yaml`)
- `.github/workflows/*.yml` — deploy on push + branch deployments on PR

For **Hybrid** deployments:

- `build.yaml` + `Dockerfile` + `container_context.yaml` (platform-specific config)
- `.github/workflows/*.yml`

The exact contents come from the official `dg plus deploy configure` command, so they stay current with whatever Dagster+ expects.

## Hybrid: additional setup required

The script defaults to Serverless. To use Hybrid, pass `--agent-type hybrid` explicitly:

```bash
./deploy_to_dagster_plus.sh kitchen-sink-demo \
  --agent-type hybrid \
  --registry-url 123456789012.dkr.ecr.us-east-1.amazonaws.com/my-repo \
  --agent-platform k8s
```

**What this script can do for Hybrid:**

- Run `dg plus deploy configure hybrid --registry-url … --agent-platform …` to scaffold `build.yaml`, `Dockerfile`, `container_context.yaml`, and CI workflows
- Print the right `docker login` command for your registry provider
- Run `dg plus deploy --build-strategy docker` once the artifacts exist

**What this script can NOT do for Hybrid (you must set up beforehand):**

| Concern | One-time setup | How |
|---|---|---|
| Hybrid agent running in your cluster | Once per cluster | [Hybrid agent install docs](https://docs.dagster.io/guides/deploy/dagster-plus/hybrid) |
| Container registry provisioned | Once | `aws ecr create-repository`, GCP Artifact Registry UI, etc. |
| Local Docker authed to push | Per machine | `aws ecr get-login-password`, `gcloud auth configure-docker`, etc. |
| Agent authed to pull | Once per cluster | Kubernetes `imagePullSecret`, ECS task IAM role, etc. |

For **Serverless**, Dagster+ owns the build cache + image storage. Nothing of the above applies.

For **Hybrid**, your agent runs in *your* infra and pulls Docker images from a registry *you* own. The script can't bootstrap that from a stranger's `curl | bash`. You're on the hook for:

| Concern | Who handles it |
|---|---|
| Creating the registry (ECR / GAR / ACR / GHCR / Docker Hub) | You |
| Auth so this machine can `docker push` | You (`aws ecr get-login-password`, `gcloud auth configure-docker`, etc.) |
| Auth so the Dagster+ agent can `docker pull` | You (imagePullSecret for k8s, IAM role for ECS, etc.) |
| Writing the registry URL into `build.yaml` | Script (via `--registry-url` flag or interactive prompt) |
| The `docker build` + `docker push` lifecycle | `dg plus deploy` (uses build.yaml) |

The script prints the credential-setup commands you need based on common registry types when it detects Hybrid:

```
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com

gcloud auth configure-docker us-central1-docker.pkg.dev

echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin
```

For Kubernetes agents pulling private images, you'll also need an `imagePullSecret` attached to the agent's service account — see the [Hybrid agent docs](https://docs.dagster.io/guides/deploy/dagster-plus/hybrid).

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
| Demos requiring env vars (DATABASE_URL, API keys) | ✅ Step 5 of this script detects + prompts for each one |
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
