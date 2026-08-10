# Deploying to Dagster+ — the umbrella doc

**Everything about getting Python code onto Dagster+.** Analog to Prefect's [/v3/concepts/deployments](https://docs.prefect.io/v3/concepts/deployments), but shaped around how Dagster+ actually works: code locations, deployments, agents, queues, branch deployments.

If you know exactly what you want to do, jump to the scenario table at the end. Otherwise start with the concepts.

## Concepts (in the order you'll hit them)

### Code location

The unit of deploy. One code location = one bundle of Definitions (assets, schedules, sensors, jobs, resources) that Dagster+ loads and serves. Every deploy command below produces or updates exactly one code location.

Under the hood: a `pyproject.toml` with `[tool.dg.project]` telling `dg` which Python module holds your `Definitions`, plus (for Hybrid) a `build.yaml` telling `dg` which Docker registry to push to.

### Deployment (`prod`, `staging`, branch)

The top-level environment. A Dagster+ organization has one or more **deployments** — typically `prod`, sometimes `staging`, and auto-created **branch deployments** per PR. **Multiple code locations live inside one deployment.** All under one UI, one asset catalog, one run history.

> ⚠ Prefect uses the word "deployment" for a single flow's config. Dagster+ uses it for the whole environment. Mapping: Prefect deployment ≈ Dagster+ **code location**; Prefect workspace ≈ Dagster+ **deployment**.

### Agent (Hybrid only)

The runtime that pulls your code and executes runs. **Serverless has a built-in agent** — you don't manage it. **Hybrid means you run your own agent** (Docker / ECS / Kubernetes / Azure Container Apps / etc.), and it pulls Docker images you push to a registry you own. Setup docs: <https://docs.dagster.io/deployment/dagster-plus/hybrid>.

The CLI never deploys agents — they're your infrastructure. `dg-deploy --hybrid` pre-checks that at least one agent is running for your target queue and warns if not (the location would sit in an error state until an agent comes online serving it).

### Agent queue

**Every code location is aligned to an agent via a queue name.** If you don't specify one, the location goes to `default`. Multiple agents can serve the same queue (load balancing), or an agent can serve a specific named queue to route certain locations to specific hardware.

Config: `agent_queue = "my-queue"` under `[tool.dg.project]` in pyproject.toml, or `--agent-queue my-queue` on the `dg-deploy` CLI.

### Branch deployments

**Auto-spun-up per PR** against a repository connected to your Dagster+ org. Dagster+ creates a full copy of your `prod` deployment with the PR's code, so you can preview + test before merging. `dg plus deploy` auto-detects the branch context (via git) and targets the right deployment; no separate command.

Prefect has no direct analog — you'd stand up a separate Prefect workspace or fork configs per branch.

### Environment variables / secrets

Set per code location, per deployment, or per branch — via Dagster+ UI (**Deployment settings → Env vars**) or `dg plus secrets` CLI. **The deploy path doesn't need to know about them**; they're attached to the code location at runtime. Common pattern: `OPENAI_API_KEY` set at the deployment level, scoped to the AI code location.

For Hybrid-only extra config (custom CA certs, extra Docker mounts, service accounts): a `container_context.yaml` at project root. `dg plus deploy` picks it up automatically.

### Serverless vs Hybrid — pick one

| | Serverless | Hybrid |
|---|---|---|
| Runtime | Dagster+ ephemeral containers (built-in agent) | Your infra (Docker / ECS / K8s / Azure) |
| Build artifact | **pex bundle** (self-contained Python zipapp) | **Docker image** (you push to your registry) |
| Setup | `dg plus login`, done | Bring up an agent + provision a container registry + docker auth |
| Docker required locally? | No | Yes (to build+push the image) |
| Best for | Get started, small-to-medium pipelines, per-flow isolation | Custom OS libs, VPC-locked resources, corporate compliance |

**Both live under the same Dagster+ deployment.** A single `prod` deployment can host Serverless locations + Hybrid locations side by side in one UI. Mixed-mode is fine.

### Dependencies

Dagster+ has no proprietary deployment YAML. Deps go in standard `pyproject.toml [project] dependencies`. `dg-deploy` auto-detects them by parsing your `.py` for `import` statements (with name mapping for the ~10 common mismatches: `sklearn` → `scikit-learn`, `bs4` → `beautifulsoup4`, `PIL` → `Pillow`, etc.) — you can override with `--deps` or hand-edit the pyproject after the first scaffold.

For Serverless: your pyproject drives the pex bundle. For Hybrid: the Dockerfile does `pip install -e .` off your pyproject. Same file, both paths.

**Version isolation**: each Serverless code location ships its own hermetic pex, so `location_A` and `location_B` in the same `prod` deployment can pin completely different versions of Dagster or pandas or anything. No shared base image, no reconciliation.

## The tools

Three tools cover every scenario. All three drive `dg plus deploy` under the hood (session-based, safe by default — no destructive workspace-mirror behavior).

### `dg plus deploy` (raw)

The CLI that Dagster+ ships. Run from any project directory with `pyproject.toml [tool.dg.project]` set. Handles Serverless (pex) or Hybrid (docker) with `--build-strategy python-executable` or `--build-strategy docker`. Session-based (start → build → push → finish) with an interactive `-y` gate on the target deployment.

Docs: <https://docs.dagster.io/deployment/dagster-plus>. Install: `uvx --from dagster-dg-cli dg` (no persistent install needed).

### [`lib/dg_deploy.sh`](./lib/dg_deploy.sh) (the wrapper) — full reference: [`lib/dg_deploy.md`](./lib/dg_deploy.md)

Ergonomic wrapper for three scenarios:

1. **Loose `.py` file(s)** — scaffolds `pyproject.toml` + (for Hybrid) `build.yaml` + `Dockerfile`, auto-detects deps, deploys.
2. **Existing dg-native project** (`[tool.dg.project]` in pyproject) — detects, deploys in place, never clobbers your files.
3. **Legacy `dagster_cloud.yaml`** — auto-migrates to modern `[tool.dg.project]` + `build.yaml`, backs up original as `.legacy-bak`, then deploys.

Local dev: same wrapper, `--dev` flag → scaffolds + boots `dg dev` at http://localhost:3000. Same shape you'll deploy.

Fetch: `curl -sL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/lib/dg_deploy.sh > dg-deploy`

### [`deploy_to_dagster_plus.sh`](./deploy_to_dagster_plus.sh) (demo-oriented)

Sister script for the community-components demo library. Takes a demo name (`setup_<demo>_demo.sh` output), configures build artifacts, deploys. Aimed at people who've curled a demo and want it live in ~60 seconds. See [`deploy_to_dagster_plus.md`](./deploy_to_dagster_plus.md).

## Scenario matrix

| You have | Tool | Command |
|---|---|---|
| One `.py` file, want it live | `dg_deploy.sh` | `bash dg-deploy my_flow.py` |
| Folder of `.py` files, want one code location | `dg_deploy.sh` | `bash dg-deploy flows/` |
| Same, but Hybrid (need a Docker image on your registry) | `dg_deploy.sh` | `bash dg-deploy my_flow.py --hybrid --registry ghcr.io/USER/name` |
| Same, but want to run locally first (no deploy) | `dg_deploy.sh` | `bash dg-deploy my_flow.py --dev` |
| Existing `[tool.dg.project]` project | `dg plus deploy` (or `dg_deploy.sh` — auto-detects) | `cd myproj && dg plus deploy` |
| Legacy `dagster_cloud.yaml` project | `dg_deploy.sh` (auto-migrates) | `bash dg-deploy path/to/legacyproj/` |
| Community-component demo (`setup_X_demo.sh` scaffold) | `deploy_to_dagster_plus.sh` | `bash deploy_to_dagster_plus.sh X` |
| Prefect migration — per-flow Docker image | `dg_deploy.sh --hybrid` | See [`prefect_vs_dagster_single_file.md`](./prefect_vs_dagster_single_file.md) |
| Prefect migration — git-driven runner (one image, iterate via `git push`) | `hybrid_git_runner/deploy.sh` | See [`hybrid_git_runner/`](./hybrid_git_runner/) |
| CI/CD (branch deployments auto-spin per PR) | `dg plus deploy` inside GitHub Actions / GitLab CI | Generated by `dg plus deploy configure` |
| Set env vars / secrets on a code location | Dagster+ UI or `dg plus secrets` | Deployment settings → Env vars |

## Reference walkthroughs

- **[single_file_serverless.md](./single_file_serverless.md)** — the Serverless (pex, no Docker) story, minimum footprint, the wrapper's Prefect-parity shape.
- **[prefect_vs_dagster_single_file.md](./prefect_vs_dagster_single_file.md)** — honest side-by-side vs Prefect. What each wins on, dependency-version story, the multi-agent-in-one-deployment story Prefect doesn't have.
- **[deploy_to_dagster_plus.md](./deploy_to_dagster_plus.md)** — one-command demo deploy (both Serverless + Hybrid), CI/CD scaffolding.
- **[hybrid_git_runner/README.md](./hybrid_git_runner/README.md)** — one specific Hybrid pattern (Prefect migration primitive): deploy a runner container ONCE, iterate on flows by pushing to a git repo.

## FAQ

**Q: `dg dev` won't run in my loose-`.py` directory. Do I need to scaffold first?**
Yes. `dg dev` requires a project with `[tool.dg.project]` in `pyproject.toml`. Two options: (a) `bash dg-deploy my_flow.py --dev` scaffolds + runs `dg dev` in one command, or (b) `dagster dev -f my_flow.py` uses the pre-`dg` Dagster CLI which does support loose files.

**Q: I still have `~/.config/dagster_cloud/config.yaml` from the old `dagster-cloud` CLI. Do I need to migrate?**
No. `dg-deploy` auto-forwards the token / org / deployment from the legacy config into env vars for the `dg plus deploy` subprocess. When you get around to it: `dg plus login` writes the modern config location.

**Q: What happens if I `--hybrid` deploy but have no agent running?**
The wrapper pre-checks and warns. The image still pushes + the code location still registers, but it stays in an error state until an agent comes online serving its queue. Bring up an agent (docs above); no separate re-deploy needed once it's live.

**Q: Can I use my own Dockerfile for Hybrid?**
Yes. If the project already has a `Dockerfile` at root, `dg-deploy` leaves it alone. If it's missing, the wrapper writes a minimal one (python-slim + `pip install -e .`). Point `build.yaml directory:` at a different location if you keep your Dockerfile elsewhere.

**Q: How do I set `OPENAI_API_KEY` for one code location without exposing it to others?**
Dagster+ UI → Deployment settings → Env vars → scope to a specific location. Or `dg plus secrets create --location <name> OPENAI_API_KEY=...`. The value is never in your repo or the pex/Docker artifact.

**Q: Do branch deployments cost extra?**
They count against your Serverless minute quota (branch deployments run when someone views them or triggers a job). Idle branch deployments have no runtime cost. Hybrid branch deployments run on your infra, same as your prod one.
