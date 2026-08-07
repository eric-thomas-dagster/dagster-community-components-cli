# Prefect vs. Dagster+ Serverless — the single-file story

**Prefect's pitch:** one `.py` file, one `prefect deploy` command, done. **Where's Dagster+?**

Answer: with the [`dg_deploy_one_file.sh`](./lib/dg_deploy_one_file.sh) CLI wrapper, exactly the same shape. Without it, Dagster+ Serverless needs 3 files instead of 1, but the tradeoff comes with real capability differences worth understanding.

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

### Dagster+ Serverless — with the CLI wrapper

```bash
# 1. Write my_flow.py
# 2. Deploy:
bash dg_deploy_one_file.sh my_flow.py                          # local file
bash dg_deploy_one_file.sh --from user/repo/path/file.py       # pull from GitHub
```

Same shape. `dg_deploy_one_file.sh` auto-scaffolds the boilerplate, auto-detects Python deps from imports, and calls `dagster-cloud serverless deploy-python-executable ...` under the hood.

### Side-by-side

| Aspect | Prefect Cloud (Managed) | Dagster+ Serverless (with wrapper) |
|---|---|---|
| Files author needs to write | 1 | 1 |
| Deploy command | `uvx prefect-cloud deploy my_flow.py:hello_world --from user/repo --name X` | `bash dg-deploy my_flow.py --location-name X` |
| Deps declaration | Autodetected from imports OR `pip_packages` | **Auto-detected from imports** (via CLI wrapper) OR `--deps 'pkg1 pkg2'` |
| Pull from GitHub | `--from user/repo` required for Managed | **`--from user/repo/path/file.py`** (via CLI wrapper) OR use local file |
| One-time setup | Prefect Cloud account + work pool config | `dagster-cloud config setup` (single interactive command) |
| Runtime env | Prefect Managed workers (ephemeral) | Dagster+ Serverless agents (ephemeral) |
| Env vars on deploy | Cloud UI or `prefect variable set` | Cloud UI (Deployment settings → Environment variables) |
| Local dev | `python my_flow.py` runs the flow directly | `uv run dg dev` opens the asset UI |
| Debugging failed runs | Run details in Cloud UI + logs | Asset materialization view + full lineage + run logs |
| Reproducibility | Code fetched from GitHub at runtime | Code snapshotted into pex at deploy time |

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

3. **Then try `dg_deploy_one_file.sh`** — with a single-file `.py` of your own. Prove the Prefect-parity ergonomics for yourself. [`lib/dg_deploy_one_file.sh`](./lib/dg_deploy_one_file.sh)

If the assets model doesn't fit your problem and you're just chaining Python functions, Prefect might be a better fit. If you want your pipeline steps to become first-class objects in a versioned catalog with typed metadata and per-partition history — Dagster's model is what you want.

## Verified

- **[`serverless_minimal/`](./serverless_minimal/)** deployed to `ericthomas-dagster.dagster.cloud/prod` (location: `hello`) — 2026-08-07 ✓
- **[`dg_deploy_one_file.sh`](./lib/dg_deploy_one_file.sh)** deployed to same (location: `cli-verify`) — 2026-08-07 ✓, proving the CLI wrapper matches raw-command results
- **Auto-detect deps + `--from github` flags** verified via dry-run against real GitHub raw URLs ✓
