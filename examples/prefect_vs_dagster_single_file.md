# Prefect vs. Dagster+ — the single-file story

**Prefect's pitch:** one `.py` file, one `prefect deploy` command, done. **Where's Dagster+?**

Answer: **matched, twice, both without Docker.** Dagster+ Serverless single-file deploys are a **pex bundle** — a self-contained Python executable, no container image, no registry, no Docker Desktop. `deploy-python-executable` uploads the pex straight to Dagster+ and runs it. For Hybrid, we've built a git-runner pattern that also skips per-iteration image builds (one runner container deployed once; iterate by pushing to a git repo it watches).

Plus meaningful differentiators Prefect can't cleanly match: mixed-agent deployments, branch deployments per PR, first-class assets/lineage/metadata across all of them.

## The honest comparison

### Prefect Cloud — Method 2 (Managed Serverless)

```bash
# 1. Write my_flow.py
# 2. Push to GitHub
# 3. Deploy:
uvx prefect-cloud deploy my_flow.py:hello_world \
    --from your-github-username/your-repo-name \
    --name managed-single-file
```

Prefect fetches the .py from your GitHub repo at runtime, executes in Prefect Managed pool.

### Dagster+ path A — Serverless with the CLI wrapper

```bash
# 1. Write my_flow.py
# 2. Deploy:
bash dg_deploy_one_file.sh my_flow.py                          # local file
bash dg_deploy_one_file.sh --from user/repo/path/file.py       # pull from GitHub
```

Same shape as Prefect. `dg_deploy_one_file.sh` auto-scaffolds the boilerplate, auto-detects Python deps from imports, and calls `dagster-cloud serverless deploy-python-executable ...` under the hood.

### Dagster+ path B — Hybrid with the git-runner (`hybrid_git_runner`)

```bash
# One-time: deploy the runner container to your Hybrid agent.
cd examples/hybrid_git_runner
./deploy.sh                                                    # uses prebuilt image

# From then on — iterate by pushing flows to your git repo:
git push origin main                                           # runner picks up on next load
```

Matches Prefect Method 2 (git-driven Managed pool) exactly once the runner is up. Runner image is deployed ONCE; new flows in the target repo appear on the next code-location load — no rebuild, no image push per iteration. See [`hybrid_git_runner/`](./hybrid_git_runner/) for the full walkthrough.

### Side-by-side (all three options)

| Aspect | Prefect Cloud (Managed) | Dagster+ Serverless (CLI wrapper) | **Dagster+ Hybrid (git-runner)** |
|---|---|---|---|
| Files author writes | 1 | 1 | 1 (git-pushed) |
| Deploy command | `uvx prefect-cloud deploy my_flow.py:hello_world --from user/repo --name X` | `bash dg-deploy my_flow.py --location-name X` | `git push` (after one-time `./deploy.sh`) |
| Deps declaration | Autodetected + `pip_packages` | **Auto-detected + `--deps`** | Baked into runner image at build time (reproducible) |
| Pull from GitHub | `--from user/repo` (required) | `--from user/repo/path/file.py` (optional — supports local files too) | **Always git-based** (runner points at your repo) |
| One-time setup | Prefect Cloud + Managed pool config | `dagster-cloud config setup` (single interactive command) | Hybrid agent up + `./deploy.sh` once |
| Runtime env | Prefect Managed workers (ephemeral) | Dagster+ Serverless agents (ephemeral) | **Your Hybrid agent** (K8s / ECS / VM / anything you run) |
| Custom container? | No | Very limited | **Full control** — put anything in the image |
| Iteration latency | ~seconds (fetch at run time) | ~1 min (pex bundle upload) | **~seconds (git clone at load time)** |
| Env vars on deploy | Cloud UI or `prefect variable set` | Cloud UI (Deployment settings → Env vars) | Cloud UI (per deployment) |
| Local dev | `python my_flow.py` runs the flow directly | `uv run dg dev` opens the asset UI | Same (or use script_orchestrator's `use_local` mode) |
| Reproducibility | Code fetched from GitHub at run time | Code snapshotted into pex at deploy time | Image + repo SHA both versioned |
| **Multi-deployment topology** | Multiple Prefect workspaces (separate accounts / configs) | Same runner (pex) + different env vars per deployment | **Same runner image + different env vars per deployment** |
| **Branch deployments (per-PR previews)** | Manual per-PR setup | Manual per-PR setup | **Auto — Dagster+ spins up a branch deployment; env vars route the runner to the PR branch** |
| **Mixed agents in one deployment** | N/A | N/A | **Yes — one Dagster+ deployment can host Serverless + Hybrid code locations side by side** |

## Where each shines

### Prefect wins on

- **`flow.serve()`** — self-hosting a flow as a long-lived process. Cool for lightweight one-off pipelines. Dagster's model is a webserver that hosts definitions, not the code that serves itself.
- **CLI ubiquity** — `prefect` is a first-party pip-installable CLI. Dagster's `dg` is too, but the Serverless deploy path currently requires `dagster-cloud` (also pip-installable) which is a separate name.
- **Configuration-as-code** — Prefect's `prefect.yaml` centralizes all deployments in one config file. Dagster has `dagster_cloud.yaml` for the same purpose but it's minimal (just the location manifest).

### Dagster+ wins on

- **Assets, not just runs** — every step of your pipeline is a first-class, browsable, versioned artifact with typed metadata. Filter your entire org's catalog by kind, group, owner, etc. Prefect's model is runs and flows — you don't have "the customer_lifetime_value asset" as an object you can inspect independently of any specific run.
- **Rich metadata surface** — `MetadataValue.float / .int / .timestamp / .markdown / .json` render inline in the UI. Dagster+ Insights can promote any numeric metadata to a custom metric with alerts (a few UI clicks). Prefect logs are logs; you build the metrics pipeline yourself.
- **Partitions as a first-class concept** — every asset can be daily / hourly / static / dynamic partitioned. Backfill any date range. Time-travel to any partition's materialization to see what happened. Prefect has scheduling but no equivalent per-partition browsable state.
- **Lineage, natively** — the asset graph shows every dependency. Prefect flows are DAG blocks with less rich cross-flow lineage.
- **Component library** — Dagster's community components (~960) cover the long tail of integrations. Prefect has integrations but a smaller declarative catalog.
- **Reproducibility** — code snapshotted into a pex at deploy time = every run in a given deployment runs against the same code. Prefect Managed fetches from GitHub at runtime → theoretically faster iteration but harder to guarantee "this exact code ran that day."

## Both do

- **Zero-infra Serverless execution.** No pods to manage, no workers to keep running. Ephemeral containers per run.
- **Cloud UI with runs, logs, retries.** Both are production-grade.
- **CLI-first deploy path.** No clickops required.
- **Schedules + sensors.** Both cover time-based and event-based triggering.
- **Python-native.** No YAML DAG requirement (Dagster has YAML components as an *option* — not required).

## Trying it — 30 seconds each

### Prefect Cloud (Managed)

```bash
# One-time: sign up at app.prefect.cloud, create a Managed work pool.
# Then:
uvx prefect-cloud deploy my_flow.py:hello_world --from user/repo --name test-flow
```

### Dagster+ Serverless (with wrapper)

```bash
# One-time: sign up at dagster.cloud, run `dagster-cloud config setup`.
# Then:
curl -sL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/lib/dg_deploy_one_file.sh > dg-deploy
bash dg-deploy my_flow.py --location-name test-location
```

Both deploy in ~2 minutes. Both give you a Cloud URL with the flow / assets visible + runnable.

## Where to start

If you're evaluating both, the honest recommendation is:

1. **Try `serverless_minimal/`** — 2 assets, 3 files, deploys in 2 minutes. See if the assets model clicks with how you think about pipelines. [`serverless_minimal/`](./serverless_minimal/)

2. **Then try `agentic_tour_serverless/`** — 4 pipelines, 14 partitions, real content. Watch how per-partition metadata + Insights + lineage all work without extra setup. [`agentic_tour_serverless/`](./agentic_tour_serverless/)

3. **Then try `dg_deploy_one_file.sh`** — with a single-file `.py` of your own. Prove the Prefect-parity Serverless ergonomics for yourself. [`lib/dg_deploy_one_file.sh`](./lib/dg_deploy_one_file.sh)

4. **If you need Hybrid** — try `hybrid_git_runner/`. Deploy the runner container ONCE, then iterate on flows via `git push`. Matches Prefect's Method 2 git-driven pattern exactly. [`hybrid_git_runner/`](./hybrid_git_runner/)

If the assets model doesn't fit your problem and you're just chaining Python functions, Prefect might be a better fit. If you want your pipeline steps to become first-class objects in a versioned catalog with typed metadata and per-partition history — Dagster's model is what you want, at any tier.

## The multi-deployment story neither tool talks about

One capability Dagster+ has that Prefect doesn't cleanly match: **the same deployment can host multiple code locations across multiple agent types simultaneously**. A single "prod" deployment can have:

- A `Serverless` code location for a small self-contained pipeline
- A `Hybrid` code location for a workload that needs custom infra
- A Hybrid `hybrid_git_runner` code location that owns a git-driven flows repo
- Branch deployments per-PR that preview changes to any of the above

All in ONE Dagster+ UI. All in one unified asset catalog. All in one run history. Prefect's model is workspaces + work pools; you don't get this cross-tier composition in one workspace as cleanly.

## Verified

- **[`serverless_minimal/`](./serverless_minimal/)** deployed to a private Dagster+ prod deployment (location: `hello`) — 2026-08-07 ✓
- **[`dg_deploy_one_file.sh`](./lib/dg_deploy_one_file.sh)** deployed to same (location: `cli-verify`) — 2026-08-07 ✓, proving the CLI wrapper matches raw-command results
- **Auto-detect deps + `--from github` flags** verified via dry-run against real GitHub raw URLs ✓
