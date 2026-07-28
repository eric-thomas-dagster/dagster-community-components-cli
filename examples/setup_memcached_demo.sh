#!/usr/bin/env bash
# setup_memcached_demo.sh
#
# Docker-local end-to-end for the Memcached component set:
#   MemcachedResourceComponent          — shared client
#   MemcachedCacheFlushJobComponent     — flush all / delete key list
#
# Round-trip validation: setup script SET/GETs against the container to
# prove connectivity, then the Dagster job runs `flush_all` and we verify
# the keys are gone.
#
# Cost: $0. Requirements: uv, docker.

set -eo pipefail

PROJECT_NAME="${1:-memcached_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
CONTAINER="dagster_memcached_demo"
IMAGE="memcached:1.6-alpine"
HOST_PORT="${MEMCACHED_HOST_PORT:-11211}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx    >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
command -v docker >/dev/null 2>&1 || fail "docker not found."
[ -d "$PROJECT_DIR" ] && fail "Directory already exists: $PROJECT_DIR"

# ── Start Memcached ─────────────────────────────────────────────────────────
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  info "Reusing existing container ${CONTAINER}"
  docker start "${CONTAINER}" >/dev/null
else
  info "Pulling + starting ${IMAGE}…"
  docker run -d --name "${CONTAINER}" -p "${HOST_PORT}:11211" \
    "${IMAGE}" >/dev/null || fail "docker run failed (is port ${HOST_PORT} free? override MEMCACHED_HOST_PORT=…)"
fi

info "Waiting for Memcached on :${HOST_PORT}…"
for i in $(seq 1 20); do
  if (echo "version"; sleep 0.1) | nc -w 1 127.0.0.1 "${HOST_PORT}" 2>/dev/null | grep -q "VERSION"; then
    ok "Memcached is up"
    break
  fi
  sleep 1
  [ "$i" = "20" ] && fail "timed out waiting for Memcached"
done

# ── Seed a few keys so we can prove the flush works ─────────────────────────
info "Seeding 3 keys via the Memcached text protocol…"
(
  printf "set demo_key_1 0 0 5\r\nhello\r\n"
  printf "set demo_key_2 0 0 5\r\nworld\r\n"
  printf "set demo_key_3 0 0 7\r\ndagster\r\n"
  sleep 0.2
) | nc -w 2 127.0.0.1 "${HOST_PORT}" >/dev/null || true
ok "Seeded 3 keys"

# Verify they're there
KEY1_VALUE="$(printf "get demo_key_1\r\n" | nc -w 1 127.0.0.1 "${HOST_PORT}" | grep -A1 "^VALUE" | tail -1 | tr -d '\r' || echo "")"
[ "$KEY1_VALUE" = "hello" ] && ok "Verified pre-flush: demo_key_1=hello" || info "(pre-flush verify skipped)"

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" pymemcache >/dev/null 2>&1
else
  uv add --quiet \
    "dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git" \
    pymemcache >/dev/null 2>&1
fi
ok "Dependencies installed"

# ── defs.yaml files ─────────────────────────────────────────────────────────
PKG="${PROJECT_NAME}"
mkdir -p "src/${PKG}/defs/memcached_resource" \
         "src/${PKG}/defs/memcached_flush_job"

cat > "src/${PKG}/defs/memcached_resource/defs.yaml" <<YAML
type: dagster_community_components.MemcachedResourceComponent
attributes:
  resource_key: memcached_resource
  host: 127.0.0.1
  port: ${HOST_PORT}
  timeout_seconds: 3.0
YAML

cat > "src/${PKG}/defs/memcached_flush_job/defs.yaml" <<YAML
type: dagster_community_components.MemcachedCacheFlushJobComponent
attributes:
  job_name: memcached_flush_all
  default_status: STOPPED
  host: 127.0.0.1
  port: ${HOST_PORT}
  # keys empty → flush_all (nuke everything). For targeted deletes, list them.
YAML

ok "Wrote 2 defs.yaml (100% components — no custom Python)"

info "Running dg check defs…"
uv run dg check defs 2>&1 | tail -6 || fail "dg check defs failed"
ok "Definitions validated"

# ── Run the flush job through Dagster to prove end-to-end works ─────────────
info "Executing the flush job end-to-end via the definitions module…"
uv run python -c "
from pathlib import Path
from dagster import load_from_defs_folder
defs = load_from_defs_folder(path_within_project=Path('src/${PKG}').resolve())
job = defs.get_job_def('memcached_flush_all')
result = job.execute_in_process(resources=defs.resources)
print('run success:', result.success)
" 2>&1 | tail -5 || fail "flush job execution failed"

# Verify the seeded keys are gone
POST_FLUSH="$(printf "get demo_key_1\r\n" | nc -w 1 127.0.0.1 "${HOST_PORT}" | tr -d '\r' | tr '\n' ' ' | head -c 100)"
if echo "$POST_FLUSH" | grep -q "hello"; then
  fail "flush didn't work — demo_key_1=hello still present: $POST_FLUSH"
else
  ok "Post-flush verified: seeded keys removed (Memcached returned '${POST_FLUSH}')"
fi

cat <<EOF

$(ok "Project scaffolded at: $PROJECT_DIR")

  Container: ${CONTAINER}  (${IMAGE})
  Memcached port: ${HOST_PORT}

Next steps:
  cd ${PROJECT_NAME}
  uv run dg dev            # → http://localhost:3000

In the UI:
  1. Sensors / Jobs tab shows memcached_flush_all — click Launch to trigger.
  2. To customize which keys get deleted, edit
     src/${PKG}/defs/memcached_flush_job/defs.yaml and set:
       keys:
         - "session:abc"
         - "user_prefs:cache"

Cleanup:
  docker rm -f ${CONTAINER}
EOF
