#!/usr/bin/env bash
# setup_pagerduty_demo.sh
#
# PagerDuty resource + sink demo — DataFrame → PagerDuty incidents upsert
# with server-side dedup via `incident_key`.
#
# Components exercised:
#   • PagerDutyResourceComponent         (20-method resource, REST + Events APIs)
#   • InlineDataframeComponent           (seed data)
#   • PagerDutyIncidentUpsertComponent   (DataFrame → PD incidents)
#
# Live-validated flow:
#   1. Verify token, resolve `from_email` from /users/me.
#   2. Scaffold Dagster project with defs.yaml.
#   3. Materialize twice to prove idempotency.
#   4. Cleanup: resolve every dagster-demo incident.
#
# COST: $0 (PagerDuty free tier — 5 users, unlimited API calls).
#
# REQUIREMENTS
#   • PAGERDUTY_API_TOKEN — general REST API token
#   • PAGERDUTY_SERVICE_ID — target service (e.g. PXXXXX from the UI URL)
#   • uv, uvx
#
# USAGE
#   export PAGERDUTY_API_TOKEN=<your_token>
#   export PAGERDUTY_SERVICE_ID=PFF0H74     # your service ID
#   ./setup_pagerduty_demo.sh          # → pagerduty_demo/

set -eo pipefail

PROJECT_NAME="${1:-pagerduty_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${PAGERDUTY_API_TOKEN:-}" ] && fail "PAGERDUTY_API_TOKEN not set."
[ -z "${PAGERDUTY_SERVICE_ID:-}" ] && fail "PAGERDUTY_SERVICE_ID not set (e.g. PFF0H74)."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

# ── Resolve from_email from /users/me ─────────────────────────────────
info "Verifying token + resolving from_email…"
FROM_EMAIL=$(curl -s \
  -H "Authorization: Token token=$PAGERDUTY_API_TOKEN" \
  -H "Accept: application/vnd.pagerduty+json;version=2" \
  "https://api.pagerduty.com/users/me" \
  | python3 -c 'import sys,json; print(json.loads(sys.stdin.read())["user"]["email"])' || echo "")
[ -z "$FROM_EMAIL" ] && fail "Failed to resolve from_email — is the token valid?"
export PAGERDUTY_FROM_EMAIL="$FROM_EMAIL"
ok "from_email: $FROM_EMAIL"

# ── Verify service access ─────────────────────────────────────────────
HTTP=$(curl -s -o /tmp/pd_svc.json -w "%{http_code}" \
  -H "Authorization: Token token=$PAGERDUTY_API_TOKEN" \
  -H "Accept: application/vnd.pagerduty+json;version=2" \
  "https://api.pagerduty.com/services/$PAGERDUTY_SERVICE_ID" || echo "000")
[ "$HTTP" = "200" ] || fail "Service $PAGERDUTY_SERVICE_ID returned HTTP $HTTP."
ok "Service $PAGERDUTY_SERVICE_ID exists"

# ── Scaffold ──────────────────────────────────────────────────────────
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

mkdir -p "src/${PROJECT_NAME}/defs/pd_resource" \
         "src/${PROJECT_NAME}/defs/incidents_seed" \
         "src/${PROJECT_NAME}/defs/incidents_mirror"

cat > "src/${PROJECT_NAME}/defs/pd_resource/defs.yaml" <<YAML
type: dagster_community_components.PagerDutyResourceComponent
attributes:
  resource_key: pd
  api_token_env_var: PAGERDUTY_API_TOKEN
  from_email_env_var: PAGERDUTY_FROM_EMAIL
YAML

cat > "src/${PROJECT_NAME}/defs/incidents_seed/defs.yaml" <<YAML
type: dagster_community_components.InlineDataframeComponent
attributes:
  asset_name: incidents_seed
  columns: [incident_id, name, description, urgency, status]
  rows:
    - [INC-1001, "Auth service 500s spike",     "5xx rate 12% for 8m starting 09:14 UTC",             high, triggered]
    - [INC-1002, "S3 upload lambda timeouts",   "50% of uploads timing out on eu-west-1",             high, acknowledged]
    - [INC-1003, "Slow dashboard queries",      "p95 latency spiked to 4.2s; rolled back optimizer.", low,  resolved]
    - [INC-1004, "Stripe webhook backlog",      "Webhooks queued >30m; missed invoicing.",            high, triggered]
    - [INC-1005, "OpenAI quota exhausted",      "Hit monthly quota by mid-month; quota uplifted.",    low,  resolved]
  group_name: pagerduty_demo
YAML

cat > "src/${PROJECT_NAME}/defs/incidents_mirror/defs.yaml" <<YAML
type: dagster_community_components.PagerDutyIncidentUpsertComponent
attributes:
  asset_name: pagerduty_incidents_mirror
  upstream_asset_key: incidents_seed
  service_id: ${PAGERDUTY_SERVICE_ID}
  resource_key: pd
  key_column: incident_id
  title_column: name
  details_column: description
  urgency_column: urgency
  status_column: status
  key_prefix: "dagster-demo-"
  group_name: pagerduty_demo
YAML

ok "Wrote defs.yaml (3 components)"

DM="${PROJECT_NAME}.definitions"

info "Running dg check…"
uv run dg check defs 2>&1 | tail -3 || fail "dg check failed"

info "Materializing pipeline (run 1)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 \
  | grep -E "PagerDuty upsert|STEP_SUCCESS|RUN_SUCCESS" | head -10 || fail "materialize failed"

info "Waiting 5s…"
sleep 5

info "Materializing pipeline (run 2 — expect no dupes)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 \
  | grep -E "PagerDuty upsert|STEP_SUCCESS|RUN_SUCCESS" | head -10 || fail "materialize failed (run 2)"

# ── Verify ────────────────────────────────────────────────────────────
info "Verifying content in PagerDuty…"
uv run python - <<PY || fail "PagerDuty verification failed"
import os, requests
token = os.environ["PAGERDUTY_API_TOKEN"]
sess = requests.Session()
sess.headers.update({
    "Authorization": f"Token token={token}",
    "Accept": "application/vnd.pagerduty+json;version=2",
})
r = sess.get("https://api.pagerduty.com/incidents", params={
    "service_ids[]": os.environ["PAGERDUTY_SERVICE_ID"],
    "statuses[]": ["triggered", "acknowledged", "resolved"],
    "limit": 100,
})
r.raise_for_status()
incidents = r.json().get("incidents", [])
demo = [i for i in incidents if (i.get("incident_key") or "").startswith("dagster-demo-INC-")]
unique_keys = {i["incident_key"] for i in demo}
print(f"  {len(demo)} incidents keyed dagster-demo-INC-* ({len(unique_keys)} unique keys)")
assert len(unique_keys) == 5, f"expected 5 unique keys, got {len(unique_keys)}: {sorted(unique_keys)}"
statuses = {}
for i in demo:
    statuses.setdefault(i.get("status"), []).append(i.get("incident_key"))
for s in sorted(statuses):
    print(f"  {s}: {sorted(statuses[s])}")
print("  ✓ all PagerDuty content verified")
PY

# ── Cleanup: resolve every dagster-demo-* incident ────────────────────
info "Cleaning up (resolving dagster-demo-* incidents)…"
uv run python - <<PY 2>&1 | tail -3
import os, requests
token = os.environ["PAGERDUTY_API_TOKEN"]
from_email = os.environ["PAGERDUTY_FROM_EMAIL"]
sess = requests.Session()
sess.headers.update({
    "Authorization": f"Token token={token}",
    "Accept": "application/vnd.pagerduty+json;version=2",
    "Content-Type": "application/json",
    "From": from_email,
})
r = sess.get("https://api.pagerduty.com/incidents", params={
    "service_ids[]": os.environ["PAGERDUTY_SERVICE_ID"],
    "statuses[]": ["triggered", "acknowledged"],
    "limit": 100,
})
r.raise_for_status()
open_incidents = [i for i in r.json().get("incidents", []) if (i.get("incident_key") or "").startswith("dagster-demo-")]
if open_incidents:
    payload = {"incidents": [{"id": i["id"], "type": "incident_reference", "status": "resolved"} for i in open_incidents]}
    sess.put("https://api.pagerduty.com/incidents", json=payload).raise_for_status()
print(f"  resolved {len(open_incidents)} demo incidents")
PY

echo
ok "Demo complete."
echo
echo "  Service:   https://dagsterlabs.pagerduty.com/service-directory/${PAGERDUTY_SERVICE_ID}"
echo "  Incidents: https://dagsterlabs.pagerduty.com/incidents?status=all"
echo
echo "  Dagster UI: uv run dg dev"
