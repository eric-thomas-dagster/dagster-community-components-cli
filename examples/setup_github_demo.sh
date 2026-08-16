#!/usr/bin/env bash
# setup_github_reshape_demo.sh
#
# GitHub reshape demo — proves the resource + sink pattern for a REST-shaped
# SaaS API (parallel to setup_notion_reshape_demo.sh).
#
# Components exercised:
#   • GithubResourceComponent      (resource with 21 read + write methods)
#   • InlineDataframeComponent     (seed data)
#   • GitHubIssueUpsertComponent   (DataFrame → GitHub issues, marker-keyed)
#
# Live-validated flow:
#   1. Verify token access to the target repo.
#   2. Scaffold Dagster project with defs.yaml wired to that repo.
#   3. Materialize the pipeline; first run creates 5 issues, second run
#      updates them (proves idempotency via body-embedded key markers).
#   4. Cleanup: closes all demo-labeled issues.
#
# COST: $0 (GitHub API is free within the 5000/hr authenticated rate limit).
#
# REQUIREMENTS
#   • GITHUB_TOKEN — PAT with issues:write on the target repo
#   • GITHUB_REPO  — target repo in `owner/name` form (scratch repo recommended)
#   • uv, uvx
#
# USAGE
#   export GITHUB_TOKEN=ghp_...
#   export GITHUB_REPO=my-user/scratch-repo
#   ./setup_github_reshape_demo.sh          # → github_reshape_demo/

set -eo pipefail

PROJECT_NAME="${1:-github_reshape_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${GITHUB_TOKEN:-}" ] && fail "GITHUB_TOKEN not set. Create a PAT at https://github.com/settings/tokens"
[ -z "${GITHUB_REPO:-}" ] && fail "GITHUB_REPO not set. Pass owner/name of a repo you have write access to."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"

# ── Verify token has repo write access ─────────────────────────────────
info "Verifying GitHub token access to $GITHUB_REPO…"
HTTP=$(curl -s -o /tmp/gh_repo_check.json -w "%{http_code}" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$GITHUB_REPO" || echo "000")
[ "$HTTP" = "200" ] || fail "GitHub /repos/$GITHUB_REPO returned HTTP $HTTP. Check token scope + repo name."
ok "Token has access to $GITHUB_REPO"

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
mkdir -p "src/${PROJECT_NAME}/defs/github_resource" \
         "src/${PROJECT_NAME}/defs/incidents_seed" \
         "src/${PROJECT_NAME}/defs/incidents_mirror"

# 1) Register the GitHub resource once.
cat > "src/${PROJECT_NAME}/defs/github_resource/defs.yaml" <<YAML
type: dagster_community_components.GithubResourceComponent
attributes:
  resource_key: github
  token_env_var: GITHUB_TOKEN
YAML

# 2) Seed sample incident data.
cat > "src/${PROJECT_NAME}/defs/incidents_seed/defs.yaml" <<YAML
type: dagster_community_components.InlineDataframeComponent
attributes:
  asset_name: incidents_seed
  columns: [incident_id, name, description, labels, state]
  rows:
    - [INC-1001, "Auth service 500s spike",         "5xx rate 12% for 8m starting 09:14 UTC",              "incident,severity:p1",        open]
    - [INC-1002, "S3 upload lambda timeouts",       "50% of uploads timing out on eu-west-1",              "incident,severity:p2",        open]
    - [INC-1003, "Slow dashboard queries",          "p95 latency spiked to 4.2s; rolled back optimizer.",  "incident,severity:p3",        closed]
    - [INC-1004, "Stripe webhook backlog",          "Webhooks queued >30m; missed invoicing.",             "incident,severity:p0",        open]
    - [INC-1005, "OpenAI quota exhausted",          "Hit monthly quota by mid-month; quota uplifted.",     "incident,severity:p2",        closed]
  group_name: github_demo
YAML

# 3) Upsert into GitHub issues.
cat > "src/${PROJECT_NAME}/defs/incidents_mirror/defs.yaml" <<YAML
type: dagster_community_components.GitHubIssueUpsertComponent
attributes:
  asset_name: github_incidents_mirror
  upstream_asset_key: incidents_seed
  repo: ${GITHUB_REPO}
  resource_key: github
  key_column: incident_id
  title_column: name
  body_column: description
  labels_column: labels
  state_column: state
  default_labels: [dagster-demo]
  # close_missing: true    # uncomment for full-mirror mode
  group_name: github_demo
YAML

ok "Wrote defs.yaml (3 components)"

# ── Validate + materialize ─────────────────────────────────────────────
DM="${PROJECT_NAME}.definitions"

info "Running dg check…"
uv run dg check defs 2>&1 | tail -3 || fail "dg check failed"

info "Materializing pipeline (run 1 — expect 5 created)…"
GITHUB_TOKEN="$GITHUB_TOKEN" uv run dagster asset materialize --select '*' -m "$DM" 2>&1 \
  | grep -E "GitHub upsert|STEP_SUCCESS|RUN_SUCCESS" | head -10 || fail "materialize failed"

# GitHub's issue list endpoint is eventually consistent — brand-new issues
# take a few seconds to appear via /repos/{owner}/{name}/issues. Real
# pipelines run minutes apart and never hit this; the demo intentionally
# re-runs to prove idempotency, so we wait a few seconds.
info "Waiting 10s for GitHub to index the new issues…"
sleep 10

info "Materializing pipeline (run 2 — expect 5 updated, 0 duplicates)…"
GITHUB_TOKEN="$GITHUB_TOKEN" uv run dagster asset materialize --select '*' -m "$DM" 2>&1 \
  | grep -E "GitHub upsert|STEP_SUCCESS|RUN_SUCCESS" | head -10 || fail "materialize failed (run 2)"

# ── Verify content in GitHub ───────────────────────────────────────────
info "Verifying content in GitHub…"
uv run python - <<PY || fail "GitHub verification failed"
import os
import requests
token = os.environ["GITHUB_TOKEN"]
repo = os.environ["GITHUB_REPO"]
s = requests.Session()
s.headers.update({
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})
import re
r = s.get(f"https://api.github.com/repos/{repo}/issues", params={"labels": "dagster-demo", "state": "all", "per_page": 100})
r.raise_for_status()
issues = [i for i in r.json() if not i.get("pull_request")]
# Group by dagster-key marker — keep the most recently updated per key so any
# stray duplicates from an earlier buggy run don't confuse the check.
by_key = {}
key_re = re.compile(r"<!-- dagster-key: ([^>]+?) -->")
for it in issues:
    m = key_re.search(it.get("body") or "")
    if not m:
        continue
    k = m.group(1).strip()
    prev = by_key.get(k)
    if prev is None or it.get("updated_at", "") > prev.get("updated_at", ""):
        by_key[k] = it
print(f"  {len(by_key)} unique dagster-demo keys in {repo} (out of {len(issues)} labeled issues)")
assert len(by_key) == 5, f"expected 5 unique keys, got {len(by_key)}: {sorted(by_key.keys())}"
open_keys = [k for k, i in by_key.items() if i.get("state") == "open"]
closed_keys = [k for k, i in by_key.items() if i.get("state") == "closed"]
print(f"  open: {sorted(open_keys)}")
print(f"  closed: {sorted(closed_keys)}")
assert sorted(open_keys) == ["INC-1001", "INC-1002", "INC-1004"], f"open mismatch: {sorted(open_keys)}"
assert sorted(closed_keys) == ["INC-1003", "INC-1005"], f"closed mismatch: {sorted(closed_keys)}"
print("  ✓ all GitHub content verified")
PY

# ── Cleanup: close all demo issues ─────────────────────────────────────
info "Cleaning up (closing all dagster-demo issues)…"
uv run python - <<PY 2>&1 | tail -3
import os, requests
token = os.environ["GITHUB_TOKEN"]; repo = os.environ["GITHUB_REPO"]
s = requests.Session()
s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
r = s.get(f"https://api.github.com/repos/{repo}/issues", params={"labels": "dagster-demo", "state": "open", "per_page": 100})
r.raise_for_status()
n = 0
for issue in r.json():
    if issue.get("pull_request"): continue
    s.patch(f"https://api.github.com/repos/{repo}/issues/{issue['number']}", json={"state": "closed"}).raise_for_status()
    n += 1
print(f"  closed {n} demo issues")
PY

echo
ok "Demo complete."
echo
echo "  Repo:       https://github.com/${GITHUB_REPO}"
echo "  Issues:     https://github.com/${GITHUB_REPO}/issues?q=label%3Adagster-demo"
echo
echo "  Dagster UI: uv run dg dev"
