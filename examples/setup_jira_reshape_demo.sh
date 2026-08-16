#!/usr/bin/env bash
# setup_jira_reshape_demo.sh
#
# Jira reshape demo — proves the resource + sink pattern for a third
# SaaS API (parallel to setup_notion_reshape_demo.sh + setup_github_reshape_demo.sh).
#
# Components exercised:
#   • JiraResourceComponent        (17-method resource, Basic auth)
#   • InlineDataframeComponent     (seed data)
#   • JiraIssueUpsertComponent     (DataFrame → Jira issues, label-keyed via JQL)
#
# Live-validated flow:
#   1. Verify token access to the target Jira project.
#   2. Scaffold Dagster project with defs.yaml wired to that project key.
#   3. Materialize the pipeline twice to prove idempotency.
#   4. Cleanup: closes all demo-labeled issues via transition to Done.
#
# COST: $0 (Jira Cloud free tier — 10 users, no time limit).
#
# REQUIREMENTS
#   • JIRA_EMAIL       — your Atlassian account email
#   • JIRA_API_TOKEN   — from id.atlassian.com/manage-profile/security/api-tokens
#   • JIRA_BASE_URL    — e.g. https://<workspace>.atlassian.net
#   • JIRA_PROJECT_KEY — target project key (e.g. SCRATCH). Must exist.
#   • uv, uvx
#
# USAGE
#   export JIRA_EMAIL=you@company.com
#   export JIRA_API_TOKEN=ATATT3xFf...
#   export JIRA_BASE_URL=https://your-workspace.atlassian.net
#   export JIRA_PROJECT_KEY=SCRATCH
#   ./setup_jira_reshape_demo.sh          # → jira_reshape_demo/

set -eo pipefail

PROJECT_NAME="${1:-jira_reshape_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${JIRA_EMAIL:-}" ] && fail "JIRA_EMAIL not set."
[ -z "${JIRA_API_TOKEN:-}" ] && fail "JIRA_API_TOKEN not set."
[ -z "${JIRA_BASE_URL:-}" ] && fail "JIRA_BASE_URL not set (e.g. https://your-workspace.atlassian.net)."
[ -z "${JIRA_PROJECT_KEY:-}" ] && fail "JIRA_PROJECT_KEY not set (e.g. SCRATCH)."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"

# ── Verify token has project access ────────────────────────────────────
info "Verifying Jira token access to project $JIRA_PROJECT_KEY…"
HTTP=$(curl -s -o /tmp/jira_project_check.json -w "%{http_code}" \
  -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
  -H "Accept: application/json" \
  "$JIRA_BASE_URL/rest/api/3/project/$JIRA_PROJECT_KEY" || echo "000")
[ "$HTTP" = "200" ] || fail "Jira /project/$JIRA_PROJECT_KEY returned HTTP $HTTP. Check credentials + project key."
ok "Token has access to project $JIRA_PROJECT_KEY"

# ── Scaffold project ───────────────────────────────────────────────────
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

# ── defs.yaml — 3 component instances ────────────────────────────────────
mkdir -p "src/${PROJECT_NAME}/defs/jira_resource" \
         "src/${PROJECT_NAME}/defs/incidents_seed" \
         "src/${PROJECT_NAME}/defs/incidents_mirror"

cat > "src/${PROJECT_NAME}/defs/jira_resource/defs.yaml" <<YAML
type: dagster_community_components.JiraResourceComponent
attributes:
  resource_key: jira
  email_env_var: JIRA_EMAIL
  api_token_env_var: JIRA_API_TOKEN
  base_url: ${JIRA_BASE_URL}
YAML

cat > "src/${PROJECT_NAME}/defs/incidents_seed/defs.yaml" <<YAML
type: dagster_community_components.InlineDataframeComponent
attributes:
  asset_name: incidents_seed
  columns: [incident_id, name, description, labels]
  rows:
    - [INC-1001, "Auth service 500s spike",     "5xx rate 12% for 8m starting 09:14 UTC",             "incident,severity-p1"]
    - [INC-1002, "S3 upload lambda timeouts",   "50% of uploads timing out on eu-west-1",             "incident,severity-p2"]
    - [INC-1003, "Slow dashboard queries",      "p95 latency spiked to 4.2s; rolled back optimizer.", "incident,severity-p3"]
    - [INC-1004, "Stripe webhook backlog",      "Webhooks queued >30m; missed invoicing.",            "incident,severity-p0"]
    - [INC-1005, "OpenAI quota exhausted",      "Hit monthly quota by mid-month; quota uplifted.",    "incident,severity-p2"]
  group_name: jira_demo
YAML

cat > "src/${PROJECT_NAME}/defs/incidents_mirror/defs.yaml" <<YAML
type: dagster_community_components.JiraIssueUpsertComponent
attributes:
  asset_name: jira_incidents_mirror
  upstream_asset_key: incidents_seed
  project_key: ${JIRA_PROJECT_KEY}
  resource_key: jira
  key_column: incident_id
  summary_column: name
  description_column: description
  labels_column: labels
  issue_type: Task
  default_labels: [dagster-demo]
  group_name: jira_demo
YAML

ok "Wrote defs.yaml (3 components)"

# ── Validate + materialize ─────────────────────────────────────────────
DM="${PROJECT_NAME}.definitions"

info "Running dg check…"
uv run dg check defs 2>&1 | tail -3 || fail "dg check failed"

info "Materializing pipeline (run 1 — expect 5 created)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 \
  | grep -E "Jira upsert|STEP_SUCCESS|RUN_SUCCESS" | head -10 || fail "materialize failed"

# Jira search is eventually consistent — new issues take a few seconds to be
# indexed by JQL. Real pipelines run minutes apart; the demo re-runs so waits.
info "Waiting 10s for Jira search to index the new issues…"
sleep 10

info "Materializing pipeline (run 2 — expect 5 updated, 0 duplicates)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 \
  | grep -E "Jira upsert|STEP_SUCCESS|RUN_SUCCESS" | head -10 || fail "materialize failed (run 2)"

# ── Verify content in Jira ─────────────────────────────────────────────
info "Verifying content in Jira…"
uv run python - <<PY || fail "Jira verification failed"
import os
import re
import requests
from requests.auth import HTTPBasicAuth

sess = requests.Session()
sess.auth = HTTPBasicAuth(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
sess.headers.update({"Accept": "application/json"})
base = os.environ["JIRA_BASE_URL"].rstrip("/")
project = os.environ["JIRA_PROJECT_KEY"]
sess.headers.update({"Content-Type": "application/json"})
r = sess.post(f"{base}/rest/api/3/search/jql", json={
    "jql": f'project = "{project}" AND labels = "dagster-demo"',
    "fields": ["summary", "labels", "status"],
    "maxResults": 100,
})
r.raise_for_status()
issues = r.json().get("issues", [])

by_key = {}
for issue in issues:
    labels = issue.get("fields", {}).get("labels", [])
    for label in labels:
        if label.startswith("dagsterkey-"):
            k = label[len("dagsterkey-"):]
            by_key[k] = issue
            break

print(f"  {len(by_key)} unique dagster-demo keys in {project} (out of {len(issues)} labeled issues)")
assert len(by_key) == 5, f"expected 5 unique keys, got {len(by_key)}: {sorted(by_key.keys())}"
print(f"  keys: {sorted(by_key.keys())}")
print("  ✓ all Jira content verified")
PY

# ── Cleanup: transition all demo issues to Done ────────────────────────
info "Cleaning up (transitioning demo issues to Done)…"
uv run python - <<PY 2>&1 | tail -3
import os, requests
from requests.auth import HTTPBasicAuth
sess = requests.Session()
sess.auth = HTTPBasicAuth(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
sess.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
base = os.environ["JIRA_BASE_URL"].rstrip("/")
project = os.environ["JIRA_PROJECT_KEY"]

r = sess.post(f"{base}/rest/api/3/search/jql", json={
    "jql": f'project = "{project}" AND labels = "dagster-demo" AND statusCategory != Done',
    "fields": ["summary", "status"],
    "maxResults": 100,
})
r.raise_for_status()
issues = r.json().get("issues", [])
n = 0
for issue in issues:
    key = issue["key"]
    trans = sess.get(f"{base}/rest/api/3/issue/{key}/transitions").json().get("transitions", [])
    done = next((t for t in trans if t.get("name", "").lower() == "done"), None)
    if done:
        sess.post(f"{base}/rest/api/3/issue/{key}/transitions", json={"transition": {"id": done["id"]}}).raise_for_status()
        n += 1
print(f"  transitioned {n} demo issues to Done")
PY

echo
ok "Demo complete."
echo
echo "  Project:  ${JIRA_BASE_URL}/jira/software/projects/${JIRA_PROJECT_KEY}"
echo "  Issues:   ${JIRA_BASE_URL}/issues/?jql=project%20=%20%22${JIRA_PROJECT_KEY}%22%20AND%20labels%20=%20%22dagster-demo%22"
echo
echo "  Dagster UI: uv run dg dev"
