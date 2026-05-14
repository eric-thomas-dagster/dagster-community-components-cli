#!/usr/bin/env bash
# Docker container asset demo — run arbitrary container images as Dagster
# assets via dagster-docker. No SaaS, no auth, no managed cluster.
#
# WHAT THIS DEMONSTRATES
#   The `docker_container_asset` component, which wraps dagster-docker so
#   any container image becomes a first-class Dagster asset:
#     - container runs per materialization
#     - logs stream into the Dagster run log
#     - lineage works via `deps:`
#     - image / command / env_vars / network all declarative in YAML
#
#   Lighter-weight than `k8s_job_asset` for local-dev or single-host
#   pipelines. Right pattern when the work needs an isolated runtime
#   (a specific Python version, system libs, ML model weights, etc.)
#   but you don't want to build a Dockerfile into every project.
#
# Asset graph (2 declare-and-run assets, 2 different images):
#   alpine_hello       ← docker_container_asset (alpine:latest)
#   python_version     ← docker_container_asset (python:3.11-slim)
#
# REQUIRES: Docker daemon running.
# COST: \$0 — both images are tiny + cached locally after first pull.

set -euo pipefail

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running."
  echo "Start Docker Desktop (or 'colima start') and re-run."
  exit 1
fi

PROJECT_DIR="${1:-docker-container-demo}"

echo ">>> 1/4  Pulling images so first dg launch isn't waiting on a pull"
docker pull -q alpine:latest >/dev/null
docker pull -q python:3.11-slim >/dev/null
echo "    Images cached: alpine:latest, python:3.11-slim"

echo ">>> 2/4  Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add --dev -q dagster-dg-cli dagster-webserver
uv add -q dagster-docker

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> 3/4  Installing docker_container_asset"
$CLI add docker_container_asset --auto-install

echo ">>> 4/4  Writing 2 defs.yaml — one per container"

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# Drop the auto-installed placeholder example
rm -rf "src/$PKG/defs/docker_container_asset"

write_yaml "alpine_hello" "type: $PKG.components.docker_container_asset.component.DockerContainerAssetComponent
attributes:
  asset_name: alpine_hello
  image: alpine:latest
  command: ['sh', '-c', 'echo \"hello from alpine — \$(uname -a)\"']
  group_name: docker_demo"

write_yaml "python_version" "type: $PKG.components.docker_container_asset.component.DockerContainerAssetComponent
attributes:
  asset_name: python_version
  image: python:3.11-slim
  command: ['python', '-c', 'import sys, platform; print(f\"Python {sys.version} on {platform.system()}\")']
  deps: [alpine_hello]
  group_name: docker_demo"

cat <<MSG

>>> Setup complete.

Validate both assets load:
    cd $PROJECT_DIR
    uv run dg check defs
    uv run dg list defs

Materialize the asset graph (each container runs, logs stream into Dagster):
    uv run dg launch --assets '*'

Browse it in the UI:
    uv run dg dev   # http://localhost:3000 → Assets graph

To retarget at your real workload, change image / command / env_vars in YAML:
  - image: your-registry.example.com/team/etl-runner:v2.3
  - command: ['python', '-m', 'mypipeline.entry']
  - env_vars: { DATABASE_URL: '\${WAREHOUSE_URL}', RUN_MODE: 'batch' }
  - network: my-internal-net

Containers can also depend on other Dagster assets via deps:, so this is
right when you want to mix Python-asset work with containerized steps.
MSG
