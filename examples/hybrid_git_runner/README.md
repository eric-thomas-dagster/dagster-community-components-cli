# hybrid_git_runner — Deploy once. Iterate with `git push`.

> **⚠️ Status: DESIGN-VERIFIED, RUNTIME VERIFICATION IN PROGRESS.**
> - Component subclass loads correctly (verified locally). ✓
> - `deploy.sh` scaffolds + emits the right `deploy-docker` command shape (verified via dry-run). ✓
> - Prebuilt image pushed to `ghcr.io/eric-thomas-dagster/hybrid-git-runner:latest` (2026-08-07). ✓
> - **Still pending**: no live Hybrid agent has been deployed against a test Dagster+ deployment yet — end-to-end `./deploy.sh` → `git push` → asset materializes flow is not yet verified. See [Testing this end-to-end](#testing-this-end-to-end) for the ~30 min one-time infra setup needed to close that gap.

**A "runner container" for Dagster+ Hybrid.** Deploy it ONCE to your Hybrid agent; from then on, iterate by pushing `.py` flows to a git repo the runner watches. No Docker rebuild per iteration. Same ergonomics as Prefect's Managed pool — you push code, the platform runs it.

Wraps [`ScriptGithubComponent`](https://github.com/eric-thomas-dagster/script_scheduling_and_orchestration/blob/main/script_orchestrator/README.md) (a `StateBackedComponent` that clones a git repo + parses Airflow DAGs / Prefect flows / plain Python scripts) in a light subclass tuned for the Hybrid runner pattern.

## The story

```
      ┌─────────────────────────────────────────┐
      │ Dagster+ deployment (prod)              │
      │                                         │
      │   ┌───────────────────────────────┐     │
      │   │ Hybrid agent (your infra)     │     │
      │   │                               │     │
      │   │   ┌─────────────────────┐     │     │
      │   │   │ hybrid_git_runner    │◄─── │─────│──── clone at load time ──┐
      │   │   │  code location      │     │     │                          │
      │   │   └─────────────────────┘     │     │                          ▼
      │   └───────────────────────────────┘     │                    ┌─────────┐
      │                                         │                    │ GitHub  │
      │   Env vars per deployment:              │                    │ (your   │
      │     SCRIPTS_REPO_URL                    │                    │  flows) │
      │     SCRIPTS_REPO_BRANCH                 │                    └─────────┘
      │     SCRIPTS_DIR                         │
      └─────────────────────────────────────────┘

     Iterate:  git push  →  Dagster+ reload code location  →  new asset graph
```

The runner image is **built once**, pushed to a registry, and pointed at by Dagster+. New flows in the repo appear on the next code-location load (or state refresh) — the container itself doesn't change.

## Deploy — three modes

Three deploy paths in [`deploy.sh`](./deploy.sh), pick whichever you want:

### Mode 1 — Use the prebuilt image (least effort)

```bash
./deploy.sh
```

Deploys `ghcr.io/eric-thomas-dagster/hybrid-git-runner:latest`. No local Docker needed; the maintainer publishes a prebuilt image so you can validate the pattern without building anything yourself.

### Mode 2 — Build + push to your registry

```bash
./deploy.sh --registry ghcr.io/your-org/hybrid-git-runner
```

Runs `docker build .` + `docker push` + registers with Dagster+. Best when you want to review / patch the image before shipping (add extra system libs, pin different Python version, embed a company base image, etc.). Requires local Docker + push access to your registry.

### Mode 3 — Point at a specific already-pushed image

```bash
./deploy.sh --image ghcr.io/somewhere/an-image:v1.2.3
```

Skip build, skip push, just register. Useful when CI builds the image separately and this script only handles the Dagster+ registration step.

Common options across all modes:

```bash
./deploy.sh \
    --location-name my-runner \        # (default: hybrid_git_runner)
    --deployment prod \                # (default: whatever's in ~/.config/dagster_cloud)
    --tag v1                           # (default: git short SHA or 'latest')
```

## Configure — env vars, not YAML edits

The runner's config surface is entirely env-vars. Set them on the Dagster+ code location's **Environment variables** page:

| Env var | Required | Purpose |
|---|---|---|
| `SCRIPTS_REPO_URL` | yes | HTTPS URL of the flows repo (e.g., `https://github.com/your-org/flows`) |
| `SCRIPTS_REPO_BRANCH` | no | Branch to track (default: `main`) |
| `SCRIPTS_DIR` | no | Subdir inside the repo (default: `scripts`) |
| `GITHUB_TOKEN` | for private repos | Personal access token with repo:read scope |
| `AIRFLOW_ENABLED` | no | Set `false` if your repo has no Airflow DAGs (default: true) |
| `PREFECT_ENABLED` | no | Set `false` if your repo has no Prefect flows (default: true) |

**Why env vars over YAML**: same runner image + same code location can be re-pointed at different repos / branches per deployment (prod vs staging vs branch previews) without image churn. Set once per deployment; forget.

## Multi-deployment topology — this is where it shines

Dagster+ organizations can have **multiple deployments** — typically `prod` + `branch deployments` (per-PR previews) + optional custom deployments (`staging`, `dev`, `qa`). The runner works cleanly across all of them because config is env-var driven per deployment.

**Example 3-deployment topology:**

| Deployment | Repo | Branch | Purpose |
|---|---|---|---|
| `prod` | `github.com/co/flows` | `main` | Production runs |
| `staging` | `github.com/co/flows` | `staging` | Pre-prod validation |
| `dev` | `github.com/co/flows` | `dev` | Feature development |

**All three run the same runner image.** The only difference is env vars set on each deployment's location config:

```
prod:     SCRIPTS_REPO_URL=https://github.com/co/flows  SCRIPTS_REPO_BRANCH=main
staging:  SCRIPTS_REPO_URL=https://github.com/co/flows  SCRIPTS_REPO_BRANCH=staging
dev:      SCRIPTS_REPO_URL=https://github.com/co/flows  SCRIPTS_REPO_BRANCH=dev
```

Each deployment's runner independently clones its branch on load and emits its own asset graph. `prod` and `staging` can have different asset lineage if the branches have different flow files. State (via `StateBackedComponent`) is namespaced per deployment — no cross-contamination.

## Branch deployments — PR previews get a real per-PR runner

When someone opens a PR against your flows repo, Dagster+ auto-creates an ephemeral **branch deployment** (usually named after the branch or PR number, e.g., `pr-42`). The branch deployment can point the runner at the PR's branch:

```
branch_deployment (pr-42):
  SCRIPTS_REPO_URL=https://github.com/co/flows
  SCRIPTS_REPO_BRANCH=feature-xyz          # ← matches the PR branch
```

The result: **every PR gets a preview code location with the PR's flows loaded**. Reviewers can materialize assets from the PR branch without merging. When the PR merges, the branch deployment tears down automatically.

Two ways to wire this:

1. **Manually** — set the branch deployment's env vars in the Dagster+ UI when the branch deployment spins up.
2. **Automatically via CI** — GitHub Action / CI job sets the env vars when the branch deployment is created. Dagster+ exposes the PR branch name in the deployment context; a small script writes it into the location env vars.

Either way, **the runner image is the same**. What changes is the branch it clones.

## Mixed agents — Serverless + Hybrid in the same deployment

One deployment can host multiple code locations across BOTH agent types:

```
deployment: prod
├── location: hello                  (Serverless — deployed via serverless_minimal/)
├── location: agentic_tour           (Serverless — deployed via agentic_tour_serverless/)
└── location: hybrid_git_runner      (Hybrid — deployed via this project)
```

Users don't care. All three locations show up in the same asset catalog, same run history, same Dagster+ UI. Pick the deployment type per code location based on:

- **Serverless** — least infra to manage; the tradeoff is Dagster+ picks the runtime.
- **Hybrid (script_orchestrator runner)** — Prefect-like git-push iteration; you own the container.
- **Hybrid (traditional per-code Docker image)** — full control over image, iterate by rebuilding (heavier). Not what this project does; see the general `deploy-docker` docs.

## What ships in this project

```
hybrid_git_runner/
├── src/hybrid_git_runner/
│   ├── __init__.py                       (empty)
│   ├── definitions.py                    (Dagster autoloader — 5 lines)
│   ├── hybrid_runner_component.py        (HybridRunnerComponent subclass — 30 lines)
│   └── defs/scripts/defs.yaml            (component instance — points at env-var-configured repo)
├── example_flows_repo/                   (sample flows repo layout — copy to a new repo)
│   ├── README.md
│   └── flows/
│       ├── hello_flow.py
│       └── weather_pipeline.py
├── Dockerfile                            (python:3.12-slim + git + deps)
├── deploy.sh                             (3-mode deploy: prebuilt / build+push / --image)
├── pyproject.toml
├── dagster_cloud.yaml
└── README.md                             (this)
```

## Ergonomics compared

| | Prefect Managed | Dagster+ Serverless (with wrapper) | **Dagster+ Hybrid (this runner)** |
|---|---|---|---|
| Files author writes | 1 (.py, git pushed) | 1 (.py, local OR `--from`) | **1 (.py, git pushed)** |
| Iteration action | `git push` | `bash dg-deploy my_flow.py` | **`git push`** |
| One-time setup | Prefect Cloud + Managed pool config | `dagster-cloud config setup` | Hybrid agent up + runner image deployed once |
| Iteration latency | ~seconds (Prefect fetches at run time) | ~minute (pex bundle upload) | **~seconds (git clone at load time)** |
| Infra owned | None | None | Hybrid agent + registry for runner image |
| Custom infra allowed | No | Limited | Yes — put anything in the image |
| Multi-deployment topology | Prefect workspaces (separate accounts) | Same runner image, different env vars per deployment | **Same runner image, different env vars per deployment** |
| Branch deployments | Manual per-PR setup | Manual per-PR setup | **Auto — Dagster+ spins up branch deployment; env vars route to PR branch** |

## Testing this end-to-end

This project is **NOT yet runtime-verified.** Here's what an end-to-end test would take:

**Prereqs (one-time, ~30 min):**

1. **A Hybrid agent running on your Dagster+ deployment.** Simplest: docker-compose. From Dagster+ docs:
   ```bash
   # In your Dagster+ deployment settings, generate an agent token.
   # Then on any host with Docker:
   docker run -d \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -e DAGSTER_CLOUD_AGENT_TOKEN=<your-agent-token> \
     -e DAGSTER_CLOUD_URL=https://YOUR-ORG.dagster.cloud/prod \
     dagster/dagster-cloud-agent:latest \
     dagster-cloud-agent run
   ```
   Once the agent is up, verify in Dagster+ UI under **Agents** — should show "Running." (K8s or ECS alternatives exist; docker-compose is fastest for validation.)

2. **A container registry with push access.** GHCR is easiest (auto-provisioned per GitHub user/org, free for public images):
   ```bash
   # One-time GHCR auth:
   echo $GITHUB_PAT | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
   ```

3. **A flows repo** (public, or with `GITHUB_TOKEN` for private). Point at anything you want the runner to load. If you want a starter, copy `example_flows_repo/` from this project into a new git repo.

**Verification run (~10 min):**

```bash
cd hybrid_git_runner/

# Mode 2: build locally + push to your registry + deploy to Dagster+
./deploy.sh --registry ghcr.io/YOUR_USER/hybrid-git-runner \
            --location-name hybrid-runner-test

# In Dagster+ UI:
#   Deployment settings → Code locations → hybrid-runner-test → Environment variables:
#     SCRIPTS_REPO_URL=https://github.com/YOUR_USER/YOUR_FLOWS_REPO
#     SCRIPTS_REPO_BRANCH=main
#     SCRIPTS_DIR=flows

# The agent pulls the image, launches it, ScriptGithubComponent clones
# your flows repo, and emits Dagster assets for each .py under flows/.

# Verify: the location loads (green in UI), assets appear in catalog,
# `dg launch --location hybrid-runner-test --assets '*'` from your
# local machine materializes them.
```

**Once that first run succeeds**, we can:
- Push the runner image to `ghcr.io/eric-thomas-dagster/hybrid-git-runner:latest` (public) so Mode 1 (`./deploy.sh` with no args) works out of the box for everyone.
- Update this README's status banner from "design-verified" to "runtime-verified."
- Ship the walkthrough as an external-facing story.

## Verified

- **Component subclass loads:** `HybridRunnerComponent(ScriptGithubComponent)` — inherits all fields, overrides `use_local=False` + `airflow_auto_install=False` + `prefect_auto_install=False`. ✓
- **`deploy.sh` scaffolds + emits correct commands:** dry-run tested — all 3 modes emit the expected `dagster-cloud serverless deploy-docker ...` invocation. ✓
- **`deploy-docker` command shape** — validated separately: same command shape shipped 7 locations to a private Dagster+ prod deployment on 2026-08-03 (Serverless projects with docker deploys).
- **Live Hybrid deploy — NOT YET RUN.** See [Testing this end-to-end](#testing-this-end-to-end) above for what's needed. Blocker: no Hybrid agent running on any test Dagster+ deployment yet.

## Related

- **[serverless_minimal/](../serverless_minimal/)** — the absolute-floor Serverless example (2 assets, 3 files).
- **[agentic_tour_serverless/](../agentic_tour_serverless/)** — the ceiling Serverless example (4 pipelines, 14 partitions).
- **[single_file_serverless.md](../single_file_serverless.md)** — the Serverless single-file story + wrapper CLI.
- **[prefect_vs_dagster_single_file.md](../prefect_vs_dagster_single_file.md)** — honest side-by-side of both platforms' single-file ergonomics.
- **[script_orchestrator](https://github.com/eric-thomas-dagster/script_scheduling_and_orchestration)** — the underlying component this project subclasses.
