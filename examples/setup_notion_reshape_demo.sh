#!/usr/bin/env bash
# setup_notion_reshape_demo.sh
#
# Notion reshape demo — proves the "resource + sinks" pattern that replaces
# the old workspace-enumeration shape for SaaS APIs.
#
# Components exercised:
#   • NotionResourceComponent    (resource with read + write convenience methods)
#   • InlineDataframeComponent   (seed data)
#   • NotionDatabaseUpsertComponent  (DataFrame → Notion DB rows)
#   • NotionPageSyncComponent    (DataFrame row → Notion page properties)
#
# Live-validated flow:
#   1. Bootstrap: script uses notion-client to create a scratch "Incidents"
#      database and a scratch "KPI Dashboard" database under a parent page
#      you control. Both DBs live under the parent page, so cleanup is one
#      click.
#   2. Scaffold Dagster project with defs.yaml wired to those IDs.
#   3. Materialize the pipeline; verify content actually landed in Notion.
#
# COST: $0 (Notion API is free within rate limits).
#
# REQUIREMENTS
#   • NOTION_TOKEN   — internal-integration token (secret_...)
#   • NOTION_PARENT_PAGE_ID — page UUID shared with the integration
#   • uv, uvx, python
#
# USAGE
#   export NOTION_TOKEN=secret_...
#   export NOTION_PARENT_PAGE_ID=3b318b92e46280ed81fbe57953414122
#   ./setup_notion_reshape_demo.sh          # → notion_reshape_demo/

set -eo pipefail

PROJECT_NAME="${1:-notion_reshape_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${NOTION_TOKEN:-}" ] && fail "NOTION_TOKEN not set. Create an integration at https://www.notion.so/my-integrations"
[ -z "${NOTION_PARENT_PAGE_ID:-}" ] && fail "NOTION_PARENT_PAGE_ID not set. Pass the UUID of a page you've shared with the integration."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" \
    'notion-client>=3.0.0' 'pandas>=1.5.0' \
    || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'notion-client>=3.0.0' 'pandas>=1.5.0' \
    || fail "uv add failed"
fi
ok "Deps installed"

# ── Bootstrap scratch Notion entities under the parent page ─────────────
# Idempotent: reuses existing DBs + KPI row if a prior demo run created them
# under the same parent page. Re-running never creates duplicates.
info "Bootstrapping / reusing scratch Notion entities under parent page…"
BOOTSTRAP_OUT=$(uv run python - <<'PY'
import os, json
from notion_client import Client

token = os.environ["NOTION_TOKEN"]
parent = os.environ["NOTION_PARENT_PAGE_ID"]
c = Client(auth=token)

INCIDENTS_TITLE = "Dagster Demo — Incidents"
KPI_TITLE = "Dagster Demo — KPI Dashboard"
KPI_ROW_TITLE = "Weekly Summary"

def find_db_under_parent(title: str):
    """Search for a database with this title parented under our target page."""
    parent_norm = parent.replace("-", "")
    r = c.search(query=title, filter={"value": "data_source", "property": "object"}, page_size=25)
    for ds in r.get("results", []):
        ds_title = "".join(t.get("plain_text", "") for t in ds.get("title", []))
        if ds_title != title:
            continue
        parent_db_id = ds.get("parent", {}).get("database_id")
        if not parent_db_id:
            continue
        db = c.databases.retrieve(database_id=parent_db_id)
        db_parent = db.get("parent", {})
        if (db_parent.get("type") == "page_id"
            and db_parent.get("page_id", "").replace("-", "") == parent_norm
            and not db.get("in_trash") and not db.get("archived")):
            return db
    return None

# 1) Incidents DB
incidents_db = find_db_under_parent(INCIDENTS_TITLE)
if incidents_db:
    print(f"  reusing Incidents DB: {incidents_db['id']}", flush=True)
else:
    incidents_db = c.databases.create(
        parent={"type": "page_id", "page_id": parent},
        title=[{"type": "text", "text": {"content": INCIDENTS_TITLE}}],
        initial_data_source={"properties": {
            "Name":        {"title": {}},
            "Severity":    {"select": {"options": [{"name": "P0"}, {"name": "P1"}, {"name": "P2"}, {"name": "P3"}]}},
            "Status":      {"select": {"options": [{"name": "Open"}, {"name": "Investigating"}, {"name": "Resolved"}]}},
            "Description": {"rich_text": {}},
            "Priority":    {"number": {}},
        }},
    )
    print(f"  created Incidents DB: {incidents_db['id']}", flush=True)

# 2) KPI Dashboard DB
kpi_db = find_db_under_parent(KPI_TITLE)
if kpi_db:
    print(f"  reusing KPI DB: {kpi_db['id']}", flush=True)
else:
    kpi_db = c.databases.create(
        parent={"type": "page_id", "page_id": parent},
        title=[{"type": "text", "text": {"content": KPI_TITLE}}],
        initial_data_source={"properties": {
            "Name":            {"title": {}},
            "Open Incidents":  {"number": {}},
            "P0 Count":        {"number": {}},
            "Last Refresh":    {"date": {}},
            "Health":          {"select": {"options": [{"name": "🟢 Healthy"}, {"name": "🟡 Degraded"}, {"name": "🔴 Critical"}]}},
        }},
    )
    print(f"  created KPI DB: {kpi_db['id']}", flush=True)

# 3) KPI summary row — reuse by title if it already exists in the KPI DB
kpi_ds_id = (kpi_db.get("data_sources") or [{}])[0].get("id")
kpi_row = None
if kpi_ds_id:
    q = c.data_sources.query(
        data_source_id=kpi_ds_id,
        filter={"property": "Name", "title": {"equals": KPI_ROW_TITLE}},
    )
    if q.get("results"):
        kpi_row = q["results"][0]
        print(f"  reusing KPI row:     {kpi_row['id']}", flush=True)

if not kpi_row:
    kpi_row = c.pages.create(
        parent={"database_id": kpi_db["id"]},
        properties={"Name": {"title": [{"type": "text", "text": {"content": KPI_ROW_TITLE}}]}},
    )
    print(f"  created KPI row:     {kpi_row['id']}", flush=True)

print("---JSON---")
print(json.dumps({
    "incidents_db_id": incidents_db["id"],
    "kpi_db_id":       kpi_db["id"],
    "kpi_row_id":      kpi_row["id"],
    "incidents_url":   incidents_db.get("url") or f"https://notion.so/{incidents_db['id'].replace('-', '')}",
    "kpi_url":         kpi_db.get("url") or f"https://notion.so/{kpi_db['id'].replace('-', '')}",
    "kpi_row_url":     kpi_row.get("url") or f"https://notion.so/{kpi_row['id'].replace('-', '')}",
}))
PY
) || fail "Notion bootstrap failed"

# Show the progress lines before the JSON marker; parse the JSON that follows.
echo "$BOOTSTRAP_OUT" | sed '/^---JSON---$/,$d'
BOOTSTRAP_JSON=$(echo "$BOOTSTRAP_OUT" | sed -n '/^---JSON---$/,$p' | tail -n +2)
INCIDENTS_DB_ID=$(echo "$BOOTSTRAP_JSON" | uv run python -c "import sys,json; print(json.loads(sys.stdin.read())['incidents_db_id'])")
KPI_ROW_ID=$(echo "$BOOTSTRAP_JSON" | uv run python -c "import sys,json; print(json.loads(sys.stdin.read())['kpi_row_id'])")
INCIDENTS_URL=$(echo "$BOOTSTRAP_JSON" | uv run python -c "import sys,json; print(json.loads(sys.stdin.read())['incidents_url'])")
KPI_URL=$(echo "$BOOTSTRAP_JSON" | uv run python -c "import sys,json; print(json.loads(sys.stdin.read())['kpi_url'])")
KPI_ROW_URL=$(echo "$BOOTSTRAP_JSON" | uv run python -c "import sys,json; print(json.loads(sys.stdin.read())['kpi_row_url'])")

ok "Incidents DB:    $INCIDENTS_URL"
ok "KPI Dashboard:   $KPI_URL"
ok "KPI Summary row: $KPI_ROW_URL"

# ── Wire definitions.py ─────────────────────────────────────────────────
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

# ── defs.yaml — 5 component instances in one manifest ────────────────────
mkdir -p "src/${PROJECT_NAME}/defs/notion_resource" \
         "src/${PROJECT_NAME}/defs/incidents_seed" \
         "src/${PROJECT_NAME}/defs/incidents_mirror" \
         "src/${PROJECT_NAME}/defs/kpi_rollup" \
         "src/${PROJECT_NAME}/defs/kpi_page_sync"

# 1) Register the Notion resource once — everything downstream uses it.
cat > "src/${PROJECT_NAME}/defs/notion_resource/defs.yaml" <<YAML
type: dagster_community_components.NotionResourceComponent
attributes:
  resource_key: notion_resource
  token_env_var: NOTION_TOKEN
YAML

# 2) Seed sample incident data (would normally come from PagerDuty / Sentry / etc.)
cat > "src/${PROJECT_NAME}/defs/incidents_seed/defs.yaml" <<YAML
type: dagster_community_components.InlineDataframeComponent
attributes:
  asset_name: incidents_seed
  columns: [incident_id, name, severity, status, description, priority]
  rows:
    - [INC-1001, "Auth service 500s spike", P1, Investigating, "5xx rate 12% for 8m starting 09:14 UTC", 800]
    - [INC-1002, "S3 upload lambda timeouts", P2, Open,          "50% of uploads timing out on eu-west-1", 400]
    - [INC-1003, "Slow dashboard queries",   P3, Resolved,       "p95 latency spiked to 4.2s, rolled back query optimizer",  100]
    - [INC-1004, "Stripe webhook backlog",   P0, Open,           "Webhooks queued >30m; missed invoicing", 1000]
    - [INC-1005, "OpenAI quota exhausted",   P2, Resolved,       "Hit org monthly quota by mid-month, uplifted",  200]
  group_name: notion_demo
YAML

# 3) Multi-row upsert: mirror the DataFrame into the Notion Incidents DB.
cat > "src/${PROJECT_NAME}/defs/incidents_mirror/defs.yaml" <<YAML
type: dagster_community_components.NotionDatabaseUpsertComponent
attributes:
  asset_name: notion_incidents_mirror
  upstream_asset_key: incidents_seed
  resource_key: notion_resource
  database_id: "${INCIDENTS_DB_ID}"
  key_property: Name
  key_column: name
  properties_map:
    name: Name
    severity: Severity
    status: Status
    description: Description
    priority: Priority
  group_name: notion_demo
YAML

# 4) Roll up the incidents into a single-row KPI summary.
cat > "src/${PROJECT_NAME}/defs/kpi_rollup/defs.yaml" <<YAML
type: dagster_community_components.InlineDataframeComponent
attributes:
  asset_name: kpi_rollup
  columns: [name, open_incidents, p0_count, last_refresh, health]
  rows:
    - ["Weekly Summary", 3, 1, "2026-08-15", "🔴 Critical"]
  group_name: notion_demo
  deps: [notion_incidents_mirror]
YAML

# 5) Single-page sync: patch the KPI row's properties from the rollup.
cat > "src/${PROJECT_NAME}/defs/kpi_page_sync/defs.yaml" <<YAML
type: dagster_community_components.NotionPageSyncComponent
attributes:
  asset_name: notion_kpi_page_sync
  upstream_asset_key: kpi_rollup
  resource_key: notion_resource
  page_id: "${KPI_ROW_ID}"
  properties_map:
    open_incidents: Open Incidents
    p0_count: P0 Count
    last_refresh: Last Refresh
    health: Health
  group_name: notion_demo
YAML

ok "Wrote defs.yaml with 5 components"

# ── Materialize ─────────────────────────────────────────────────────────
DM="${PROJECT_NAME}.definitions"

info "Running dg check…"
uv run dg check defs 2>&1 | tail -5 || fail "dg check failed"

info "Materializing pipeline…"
NOTION_TOKEN="$NOTION_TOKEN" uv run dagster asset materialize \
  --select '*' -m "$DM" 2>&1 | grep -E "Notion|STEP_SUCCESS|RUN_SUCCESS" | head -20 || fail "materialize failed"

# ── Verify content actually landed in Notion ────────────────────────────
info "Verifying content in Notion…"
uv run python - <<PY || fail "Notion verification failed"
import os, sys
from notion_client import Client
c = Client(auth=os.environ["NOTION_TOKEN"])

# The Notion 2025 API requires querying via data_source_id, not database_id.
db = c.databases.retrieve(database_id="${INCIDENTS_DB_ID}")
ds_id = db["data_sources"][0]["id"]
rows = c.data_sources.query(data_source_id=ds_id).get("results", [])
print(f"  Incidents DB rows: {len(rows)}")
assert len(rows) == 5, f"expected 5 rows, got {len(rows)}"

kpi = c.pages.retrieve(page_id="${KPI_ROW_ID}")
props = kpi["properties"]
p0 = props["P0 Count"]["number"]
health = (props["Health"]["select"] or {}).get("name")
open_ct = props["Open Incidents"]["number"]
print(f"  KPI page: Open={open_ct}, P0={p0}, Health={health}")
assert p0 == 1 and open_ct == 3 and health == "🔴 Critical", f"KPI mismatch"
print("  ✓ all Notion content verified")
PY

echo
ok "Demo complete."
echo
echo "  Incidents DB:    $INCIDENTS_URL"
echo "  KPI Dashboard:   $KPI_URL"
echo "  KPI Summary row: $KPI_ROW_URL"
echo
echo "  Dagster UI:  uv run dg dev"
echo "  Cleanup:     archive the two DBs in Notion (they're under the parent page you passed)."
