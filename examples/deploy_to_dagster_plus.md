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

Already `cd`'d into a Dagster project (your own, or one you cloned)? The project dir argument is optional in that case — the script checks the current directory for a `[tool.dg]` project before erroring:

```bash
cd kitchen-sink-demo
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/deploy_to_dagster_plus.sh | bash
```

The script prompts you along the way. To accept defaults and skip prompts in CI:

```bash
curl -fsSL .../deploy_to_dagster_plus.sh | bash -s -- kitchen-sink-demo --non-interactive
```

## What the script does (8 steps)

```
1/8  Ensures dagster-cloud-cli is in dev deps                     (idempotent)
2/8  Runs `dg plus login` if not already authed                   (browser OAuth)
       → Saves a USER token to ~/.dagster_cloud_cli/config.
       → THIS token is what powers the local deploy in step 8.
3/8  [prompts] Run `dg plus deploy configure --git-provider github`?
       Auto-detects your agent type (Serverless vs Hybrid) and scaffolds:
       → build.yaml                              (modern code-location manifest)
       → Dockerfile + container_context.yaml     (Hybrid only -- the resource/sidecar/
                                                    mount config file always gets written,
                                                    but with everything commented out)
       → .github/workflows/*.yml                 (CI/CD)
       → If it's still 100% commented out (i.e. untouched by anyone), offers to add a
         default compute size for ECS/Kubernetes -- a common cause of a run getting
         killed is under-sized compute, and this is the fix for it.
4/8  [prompts, if .github/ exists] Create a CI API token?
       → A SEPARATE token for GitHub Actions runs (no browser available there).
       → Can be auto-set as a GitHub secret in step 5, or paste it in yourself.
       → Optional — skip this if you only deploy from your laptop.
5/8  [prompts, if git-provider=github and .github/workflows exists] Publish to GitHub?
       Only runs if the `gh` CLI is installed and authenticated — otherwise it
       prints the equivalent manual commands and moves on.
       → `git init` (if this isn't a repo yet) + sets a local-only git identity
         if none is configured
       → Already have a pushable `origin` (you cloned your own repo)? Uses it
         as-is, and commits/pushes ONLY the files this script generated
         (build.yaml, Dockerfile, .github/workflows/) — never sweeps up other
         uncommitted work sitting in your checkout.
       → `origin` exists but you can't push to it (e.g. you cloned someone
         else's public example)? Falls back to a second remote, `dagster-plus`,
         pointed at a repo under your own account instead.
       → No usable remote at all? Asks for a repo name — attaches to it if it
         already exists on GitHub, otherwise runs `gh repo create`. Since
         there's nothing committed yet in this case, this first commit is the
         whole project, not just the deployment config.
       → `gh secret set DAGSTER_CLOUD_API_TOKEN` (if step 4 created a token) —
         set BEFORE the first push, so the Action's first run doesn't fail on
         a missing secret
       → `git commit` + `git push -u <remote> <branch>`
       → **This push does NOT mean Dagster+ is deployed yet.** GitHub Actions
         doesn't reliably trigger on the push that creates a branch — if this
         was that first push, the script pushes one more harmless empty commit
         right after, specifically to kick off the first real run. Only once
         that's confirmed should you expect to see a run in the Actions tab.
       → Refuses to touch anything if this project dir is already nested inside
         a different git repo, to avoid creating a confusing nested repo.
6/8  Confirms deployment target (org / deployment / build strategy)
       → You see exactly which deployment env vars + the deploy will target
         before any changes get made.
7/8  Scans your project for env var references — and offers to set each one.
       → Greps for EnvVar("X"), os.environ["X"], *_env_var: X, ${env:X}.
       → Runs `dg plus pull env` first to see what's ALREADY set on this
         deployment, so we don't blindly overwrite. Existing vars get
         a "kept" status by default; you can opt in to overwrite.
       → For each new (not-yet-on-Dagster+) name:
           - If already set in your shell: shows a preview + asks "Use this value? [Y/n/skip]"
           - If not set: prompts for a value (Enter to skip)
       → Each provided value runs `dg plus create env X --value … --deployment …`.
       → Scan only catches the most common patterns — set any others manually:
           uv run dg plus create env MY_VAR --value '...' --deployment prod
8/8  [prompts] Run dg plus deploy now?  (uses your step-2 user token, NOT the CI token)
       → If step 5 actually published to GitHub, this defaults to NO instead of
         YES -- the scaffolded Action is already going to build and deploy this,
         so running a local deploy too would just be a redundant duplicate.
```

**Why two tokens?**

| Token | Lives in | Used by | Created by |
|---|---|---|---|
| **User login token** | `~/.dagster_cloud_cli/config` on your laptop | The local `dg plus deploy` from this script | `dg plus login` — step 2 |
| **CI API token** | GitHub repo secret `DAGSTER_CLOUD_API_TOKEN` | GitHub Actions runs scaffolded in step 3 | `dg plus create ci-api-token` — step 4, set as a secret in step 5 |

The CI token in step 4 is **optional for the current script run** — your `dg plus login` token from step 2 is what powers step 8. Step 4's token only matters if you want pushes to `main` to redeploy automatically via the GitHub Actions workflow — which is exactly what step 5 wires up.

**Key on step 3:** uses the official `dg plus deploy configure` command — not hand-written YAML. That command auto-detects whether your Dagster+ workspace is Serverless or Hybrid, then writes the exact artifacts the official tooling expects. If you've migrated from a `dagster_cloud.yaml`-based project, the new build.yaml replaces it.

**Step 5 closes a real gap:** steps 1-4 scaffold a GitHub Actions workflow, but none of the demo setup scripts `git init` a repo — so without step 5, that workflow has nothing to attach to and never runs until you set up git/GitHub yourself. Step 5 is entirely optional and skips cleanly (falling back to printed manual commands) if you don't have the `gh` CLI installed and authenticated, or if you answer no to its prompt.

## Prerequisites

- **A Dagster+ account** — free 30-day trial at <https://dagster.io/plus>.
- **`uv` + `uvx`** — already on your machine from running any demo.
- A demo project on disk (created by one of the `setup_*_demo.sh` scripts).
- **Optional, for step 5 (publish to GitHub):** the [`gh` CLI](https://cli.github.com), authenticated (`gh auth login`). Without it, step 5 just prints the manual git/gh commands and continues.

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
  --with-ci \
  --publish-to-github \
  --github-repo my-demo-repo \
  --github-visibility private
# --with-ci            scaffolds GitHub Actions without prompting
# --publish-to-github  runs git init + gh repo create + push without prompting
```

| Flag | Default | When to set |
|---|---|---|
| `--organization` | from `dg plus login` | Override the org chosen at login time |
| `--deployment` | `prod` | Deploy to a branch deployment or a non-prod env |
| `--build-strategy` | auto-detect | Force `docker` or `python-executable` |
| `--publish-to-github` | (off) | Run step 5 (git init / `gh repo create` / push) without the interactive prompt — needs `gh` installed + authenticated |
| `--github-repo` | project dir name | Repo name for `gh repo create`, if one doesn't exist yet |
| `--github-visibility` | `private` | `private` or `public`, for `gh repo create` |
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

If you opt in to step 4, the script runs `dg plus create ci-api-token` and prints the token once. Copy it immediately — Dagster+ won't show it again.

If `gh` is installed and authenticated, step 5 sets it as the GitHub secret for you automatically. Otherwise, do it yourself:

1. Your repo → Settings → Secrets and variables → Actions → New repository secret
2. Name: `DAGSTER_CLOUD_API_TOKEN`
3. Value: the token

After that, your CI workflows can deploy without further prompts.

## After the deploy

Open your workspace:

```bash
open "https://<your-org>.dagster.cloud/prod"
```

The agent is already running (Dagster+ keeps it warm). On each deploy / redeploy, your **code location** restarts — that takes a few seconds. After it's loaded, you can materialize assets right away.

### Redeploying later

After a code change, you don't need to re-run this whole script — your login token + `build.yaml` + env vars are all saved. Just:

```bash
cd <your_project_dir>
uv run dg plus deploy --deployment prod
```

If your login token has expired (default ~30 days):

```bash
uv run dg plus login
uv run dg plus deploy --deployment prod
```

To add or update an env var anytime:

```bash
uv run dg plus create env MY_KEY --value "..." --deployment prod
```

If you published to GitHub in step 5, **you don't need to run anything manually** — pushing to `main` redeploys automatically.

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

**Hybrid build wants Docker but you don't have a registry** — Hybrid only supports Docker images; the Hybrid agent has no PEX runtime. You need to provision a registry (ECR / GAR / ACR / GHCR / Docker Hub) and pass `--registry-url <url>` before deploying. There's no PEX shortcut for Hybrid.

## Full reference

The script wraps the official `dg plus` CLI:

- <https://docs.dagster.io/api/clis/dg-cli/dg-plus> — top-level
- <https://docs.dagster.io/api/clis/dg-cli/dg-plus#login>
- <https://docs.dagster.io/api/clis/dg-cli/dg-plus#deploy>
- <https://docs.dagster.io/api/clis/dg-cli/dg-plus#create>

For deep CI/CD customization (matrix builds, deploy hooks, GitHub Status checks), see [dagster-cloud-action](https://github.com/dagster-io/dagster-cloud-action) — the official source for the workflow templates the script generates.

## See also

Browse the [walkthrough index](README.md) for related demos across every component family.
