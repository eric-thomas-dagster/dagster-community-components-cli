#!/usr/bin/env bash
# setup_airtable_demo.sh
#
# Airtable resource + sink demo — DataFrame → Airtable records upsert
# using Airtable's NATIVE server-side upsert endpoint.
#
# Components exercised:
#   • AirtableResourceComponent            (~10-method resource)
#   • InlineDataframeComponent             (seed data)
#   • AirtableRecordUpsertComponent        (DataFrame → records w/ performUpsert)
#
# Live-validated flow:
#   1. Verify token + list bases; find target base + table.
#   2. Scaffold Dagster project.
#   3. Materialize twice — should be idempotent because Airtable's
#      performUpsert is server-side.
#   4. Cleanup: delete every demo record from the table.
#
# COST: $0 (Airtable's free tier — 1200 records / base).
#
# REQUIREMENTS
#   • AIRTABLE_API_KEY   — Personal Access Token (starts with `pat`)
#   • AIRTABLE_BASE_ID   — target base ID (starts with `app`)
#   • AIRTABLE_TABLE     — target table name or ID (default: "Table 1")
#   • uv, uvx
#
# USAGE
#   export AIRTABLE_API_KEY=patXXX...
#   export AIRTABLE_BASE_ID=appXXX...
#   export AIRTABLE_TABLE="Table 1"      # optional; default "Table 1"
#   ./setup_airtable_demo.sh          # → airtable_demo/

set -eo pipefail

PROJECT_NAME="${1:-airtable_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
: "${AIRTABLE_TABLE:=Table 1}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${AIRTABLE_API_KEY:-}" ] && fail "AIRTABLE_API_KEY not set."
[ -z "${AIRTABLE_BASE_ID:-}" ] && fail "AIRTABLE_BASE_ID not set."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Verifying Airtable token + base access…"
HTTP=$(curl -s -o /tmp/at_probe.json -w "%{http_code}" \
  -H "Authorization: Bearer $AIRTABLE_API_KEY" \
  "https://api.airtable.com/v0/meta/bases/$AIRTABLE_BASE_ID/tables")
[ "$HTTP" = "200" ] || fail "Airtable base check returned HTTP $HTTP. Check token + base ID."
ok "Base $AIRTABLE_BASE_ID accessible"

# Confirm the target table exists
python3 -c "
import json, sys
d = json.load(open('/tmp/at_probe.json'))
tables = d.get('tables', [])
names = [t['name'] for t in tables]
target = '$AIRTABLE_TABLE'
if target not in names and not any(t['id'] == target for t in tables):
    sys.stderr.write(f'ERROR: table {target!r} not found. Available: {names}\n')
    sys.exit(1)
" || fail "Target table not found in base."
ok "Target table $AIRTABLE_TABLE exists"

info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" 'requests>=2.28.0' 'pandas>=1.5.0' \
    || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'requests>=2.28.0' 'pandas>=1.5.0' \
    || fail "uv add failed"
fi
ok "Deps installed"

mkdir -p "$PROJECT_DIR/.dagster_storage"
cat > "src/${PROJECT_NAME}/definitions.py" <<PY
from pathlib import Path
from dagster import definitions, load_from_defs_folder, FilesystemIOManager

@definitions
def defs():
    root = Path(__file__).resolve().parent.parent.parent
    storage = root / ".dagster_storage"; storage.mkdir(exist_ok=True)
    return load_from_defs_folder(path_within_project=Path(__file__).parent).with_resources(
        resources={"io_manager": FilesystemIOManager(base_dir=str(storage))},
    )
PY

mkdir -p "src/${PROJECT_NAME}/defs/at_resource" \
         "src/${PROJECT_NAME}/defs/tasks_seed" \
         "src/${PROJECT_NAME}/defs/tasks_mirror"

cat > "src/${PROJECT_NAME}/defs/at_resource/defs.yaml" <<YAML
type: dagster_community_components.AirtableResourceComponent
attributes:
  resource_key: airtable
  api_key_env_var: AIRTABLE_API_KEY
YAML

cat > "src/${PROJECT_NAME}/defs/tasks_seed/defs.yaml" <<YAML
type: dagster_community_components.InlineDataframeComponent
attributes:
  asset_name: tasks_seed
  columns: [task_id, name, description]
  rows:
    - [TASK-1001, "dagster-demo: Migrate auth service to OIDC",   "Coordinate with security team; test in staging first."]
    - [TASK-1002, "dagster-demo: Add rate limiting to /api/v1/*", "Use token bucket; 100 req/min default."]
    - [TASK-1003, "dagster-demo: Deprecate legacy webhook path",  "Send 90-day sunset notice to integrators."]
    - [TASK-1004, "dagster-demo: Investigate slow dashboard query", "p95 spiked to 4.2s Aug 12; roll back optimizer?"]
    - [TASK-1005, "dagster-demo: Refresh customer NPS survey",    "Trigger via Delighted; batch of 500."]
  group_name: airtable_demo
YAML

cat > "src/${PROJECT_NAME}/defs/tasks_mirror/defs.yaml" <<YAML
type: dagster_community_components.AirtableRecordUpsertComponent
attributes:
  asset_name: airtable_tasks_mirror
  upstream_asset_key: tasks_seed
  resource_key: airtable
  base_id: ${AIRTABLE_BASE_ID}
  table: "${AIRTABLE_TABLE}"
  key_fields: [Name]
  fields_map:
    name: Name
    description: Notes
  typecast: true
  group_name: airtable_demo
YAML

ok "Wrote defs.yaml (3 components)"

DM="${PROJECT_NAME}.definitions"

info "Running dg check…"
uv run dg check defs 2>&1 | tail -3 || fail "dg check failed"

# Pre-clean any prior demo records so the pass/fail signal is unmuddied
info "Pre-cleanup: deleting any prior dagster-demo records…"
uv run python - <<PY 2>&1 | tail -3
import os, requests
sess = requests.Session()
sess.headers.update({"Authorization": f"Bearer {os.environ['AIRTABLE_API_KEY']}"})
base = os.environ["AIRTABLE_BASE_ID"]; table = os.environ["AIRTABLE_TABLE"]
# List and filter to dagster-demo names
r = sess.get(f"https://api.airtable.com/v0/{base}/{table}", params={
    "filterByFormula": "FIND('dagster-demo:', {Name}) > 0",
    "pageSize": 100,
})
r.raise_for_status()
records = r.json().get("records", [])
if records:
    # Batch-delete 10 at a time
    n = 0
    for i in range(0, len(records), 10):
        chunk = records[i:i+10]
        params = [("records[]", rec["id"]) for rec in chunk]
        sess.delete(f"https://api.airtable.com/v0/{base}/{table}", params=params).raise_for_status()
        n += len(chunk)
    print(f"  pre-cleaned {n} dagster-demo records")
else:
    print(f"  pre-cleaned 0 records")
PY

info "Materializing pipeline (run 1 — expect 5 created)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 \
  | grep -E "Airtable upsert|STEP_SUCCESS|RUN_SUCCESS" | head -10 || fail "materialize failed"

info "Materializing pipeline (run 2 — expect 0 created, 5 updated)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 \
  | grep -E "Airtable upsert|STEP_SUCCESS|RUN_SUCCESS" | head -10 || fail "materialize failed (run 2)"

info "Verifying content in Airtable…"
uv run python - <<PY || fail "Airtable verification failed"
import os, requests
sess = requests.Session()
sess.headers.update({"Authorization": f"Bearer {os.environ['AIRTABLE_API_KEY']}"})
base = os.environ["AIRTABLE_BASE_ID"]; table = os.environ["AIRTABLE_TABLE"]
r = sess.get(f"https://api.airtable.com/v0/{base}/{table}", params={
    "filterByFormula": "FIND('dagster-demo:', {Name}) > 0",
    "pageSize": 100,
})
r.raise_for_status()
records = r.json().get("records", [])
names = sorted(rec["fields"].get("Name", "") for rec in records)
print(f"  {len(records)} dagster-demo records:")
for n in names:
    print(f"    {n}")
assert len(records) == 5, f"expected 5 records, got {len(records)}"
print("  ✓ all Airtable content verified (5 records, 0 duplicates)")
PY

info "Cleaning up (deleting dagster-demo records)…"
uv run python - <<PY 2>&1 | tail -3
import os, requests
sess = requests.Session()
sess.headers.update({"Authorization": f"Bearer {os.environ['AIRTABLE_API_KEY']}"})
base = os.environ["AIRTABLE_BASE_ID"]; table = os.environ["AIRTABLE_TABLE"]
r = sess.get(f"https://api.airtable.com/v0/{base}/{table}", params={
    "filterByFormula": "FIND('dagster-demo:', {Name}) > 0",
    "pageSize": 100,
})
r.raise_for_status()
records = r.json().get("records", [])
n = 0
for i in range(0, len(records), 10):
    chunk = records[i:i+10]
    params = [("records[]", rec["id"]) for rec in chunk]
    sess.delete(f"https://api.airtable.com/v0/{base}/{table}", params=params).raise_for_status()
    n += len(chunk)
print(f"  deleted {n} demo records")
PY

echo
ok "Demo complete."
echo
echo "  Airtable base: https://airtable.com/${AIRTABLE_BASE_ID}"
echo
echo "  Dagster UI: uv run dg dev"
