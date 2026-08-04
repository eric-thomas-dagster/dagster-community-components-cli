# Docker container as a Dagster asset
> ❌ **Dagster+ Serverless / Hybrid:** local-only demo — requires container/server dependency.

Run any container image as a first-class Dagster asset via `dagster-docker`. No SaaS, no auth, no managed cluster — just the local Docker daemon. Right when the work needs an isolated runtime (a specific Python version, system libs, ML model weights, vendored CLI tools) but you don't want to build a Dockerfile into every Dagster project.

## Components used

| Component | Source | Role |
|---|---|---|
| `docker_container_asset` | community | Wraps `dagster_docker.execute_docker_container` — image / command / env_vars / network → Dagster asset |

## Architecture

```
   ┌───────────────────────────┐
   │ alpine_hello              │
   │   image: alpine:latest    │
   │   cmd:   sh -c "echo …"   │
   └────────────┬──────────────┘
                │ deps:
                ▼
   ┌───────────────────────────┐
   │ python_version            │
   │   image: python:3.11-slim │
   │   cmd:   python -c "…"    │
   └───────────────────────────┘
```

Two `docker_container_asset` instances, two different images, lineage wired with `deps:`.

## Prerequisites

- **Docker daemon running.** The setup script exits early with a clear message if it's not.

## Run

```bash
bash setup_docker_container_demo.sh
cd docker-container-demo

uv run dg check defs
uv run dg launch --assets '*'
```

Expect `RUN_SUCCESS`. Each container runs in ~300ms after the first pull. Both stdout streams land in the Dagster run log:

```
alpine_hello   → b'hello from alpine — Linux ... aarch64 Linux\n'
python_version → b'Python 3.11.15 (main, May  8 2026, ...) [GCC 14.2.0] on Linux\n'
```

## YAML shape

```yaml
type: dagster_component_templates.DockerContainerAssetComponent
attributes:
  asset_name: my_container_step
  image: my-registry.example.com/team/etl-runner:v2.3
  command: ['python', '-m', 'mypipeline.entry', '--mode=batch']
  env_vars:
    DATABASE_URL: "${WAREHOUSE_URL}"
    RUN_MODE: batch
    LOG_LEVEL: info
  network: my-internal-net      # optional — join an existing user-defined network
  deps: [upstream_asset]         # optional — Dagster asset lineage
  group_name: containers
```

Every field except `asset_name` + `image` is optional. The component:

1. Resolves the image (pulls if not cached locally)
2. Runs the container in the foreground
3. Streams the container's stdout/stderr into Dagster's run log
4. Marks the asset materialized when the container exits 0; fails the step otherwise

## When to use this vs alternatives

| Need | Use |
|---|---|
| Isolated runtime per asset, single host | `docker_container_asset` (this) |
| Kubernetes cluster you already run | `k8s_job_asset` |
| Pure Python in the same process | `@asset` directly, or `python_callable_job` for a job-style wrapper |
| Cloud-managed run (GCP / AWS) | `cloud_run_job_trigger_asset`, `google_cloud_run_jobs` |

`docker_container_asset` is **per-materialization**, not per-run — every asset launch spawns a fresh container. Good for stateless work; for long-running services (a Jupyter server, an MLflow tracking server), use a separate compose / Kubernetes manifest and declare the service as `external_*` instead.

## Trade-offs & gotchas

- **Pull latency on first run.** The setup script pre-pulls `alpine:latest` + `python:3.11-slim` so the demo's first launch isn't waiting on a pull. In production, either pre-pull on the run host or accept the cold-start cost per asset.
- **Command parsing.** `command:` is a list passed directly to `docker run image cmd...` — no shell. To use shell features (`$()`, pipes, redirects, env-var expansion), wrap with `['sh', '-c', '...']`.
- **No data passed between assets.** `deps:` provides ordering + lineage only. To pass a DataFrame between a `docker_container_asset` and a downstream Python asset, write to a shared volume / S3 / SQLite and have the downstream read it.
- **Daemon access.** The container's Docker daemon must be reachable from the Dagster process. Local dev: just runs. Remote Dagster (e.g., Dagster+ Hybrid agent): the agent host must have Docker + permissions.
- **Resource limits / GPU / volumes.** Not exposed in the component yet — extend with `host_config={...}` if needed.

## See also

- [`composition_primitives.md`](composition_primitives.md) — small jobs without containers (Python callables, HTTP, SQL)
- [`kafka.md`](kafka.md) — another Docker-backed local demo (Kafka in KRaft mode)
- [`s3_pipeline.md`](s3_pipeline.md) — Docker-backed Minio S3 demo
