#!/usr/bin/env bash
# setup_crm_reconciliation_demo.sh
#
# The demo every RevOps team asks for: "we have both HubSpot AND Salesforce.
# How do we reconcile them?" This pipeline scaffolds the pattern end-to-end
# using synthetic data (so it runs live with no auth), and swaps the two
# ingestion assets for real HubSpot / Salesforce ingestion in production
# by editing one line each.
#
# What it demonstrates
#   • Two CRM-ingestion shapes side by side (`hubspot_ingestion`
#     and `salesforce_ingestion` — swap in for the synthetic sources
#     when you have real creds)
#   • DataframeJoin — outer join on email keys to catch:
#       - contacts in HubSpot only
#       - contacts in Salesforce only
#       - contacts in both (with columns from each)
#   • A unified customer view that downstream reverse-ETL, LLM
#     personalization, or attribution assets can consume
#
# Cost: $0. Synthetic data + local pandas join. Zero API calls.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#
# Usage
#   ./setup_crm_reconciliation_demo.sh                # → crm_reconciliation_demo/
#   ./setup_crm_reconciliation_demo.sh my_project     # custom name

set -eo pipefail

PROJECT_NAME="${1:-crm_reconciliation_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv: https://docs.astral.sh/uv/"
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"

# ── Scaffold ────────────────────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps (dagster-community-components + pandas)…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" 'pandas>=1.5.0' || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' 'pandas>=1.5.0' || fail "uv add failed"
fi
ok "Deps installed"

# ── Dagster defs ────────────────────────────────────────────────────────────
mkdir -p "src/${PROJECT_NAME}/defs/hubspot_contacts"
mkdir -p "src/${PROJECT_NAME}/defs/salesforce_contacts"
mkdir -p "src/${PROJECT_NAME}/defs/unified_customer_view"

# 1. HubSpot-side ingestion. In production, swap the `type:` line for
#    `dagster_community_components.HubSpotIngestionComponent` and pass
#    api_key + resources: [contacts]. See hubspot_ingestion in the registry.
cat > "src/${PROJECT_NAME}/defs/hubspot_contacts/defs.yaml" <<'YAML'
type: dagster_community_components.SyntheticDataGeneratorComponent
attributes:
  asset_name: hubspot_contacts
  schema_type: customers
  row_count: 200
  random_state: 100          # ← different seed than salesforce
  description: "Synthetic HubSpot contacts (swap for HubSpotIngestionComponent in prod)"
  group_name: crm
YAML

# 2. Salesforce-side ingestion. Same swap in prod for
#    `dagster_community_components.SalesforceIngestionComponent` with
#    username + password + security_token + sf_objects: [Contact, Lead].
cat > "src/${PROJECT_NAME}/defs/salesforce_contacts/defs.yaml" <<'YAML'
type: dagster_community_components.SyntheticDataGeneratorComponent
attributes:
  asset_name: salesforce_contacts
  schema_type: customers
  row_count: 200
  random_state: 200          # ← different seed to guarantee some non-overlap
  description: "Synthetic Salesforce contacts (swap for SalesforceIngestionComponent in prod)"
  group_name: crm
YAML

# 3. THE RECONCILIATION. Outer join on email — every row from either side
#    survives, with columns from both. Nulls tell you which system a
#    contact lives in.
cat > "src/${PROJECT_NAME}/defs/unified_customer_view/defs.yaml" <<'YAML'
type: dagster_community_components.DataframeJoin
attributes:
  asset_name: unified_customer_view
  left_asset_key: hubspot_contacts
  right_asset_key: salesforce_contacts
  how: outer                        # ← keep everyone from both sides
  "on":                             # quote the key — YAML 1.1 makes `on:` a boolean
    - email                         # match on stable business key
  suffixes:
    - _hubspot
    - _salesforce
  group_name: crm
  description: |
    Outer-join of HubSpot + Salesforce contacts on email. Downstream:
    - rows with all _hubspot cols null → SF-only contact
    - rows with all _salesforce cols null → HubSpot-only contact
    - both present → cross-system match; use for enrichment / dedup
YAML

ok "Wrote 3 defs.yaml (hubspot + salesforce + join)"

# ── Materialize ─────────────────────────────────────────────────────────────
info "Materializing hubspot_contacts + salesforce_contacts + unified_customer_view together…"
# All three in ONE run — Dagster's default IO manager is per-run, so the
# join asset can't load upstream artifacts from a separate materialize
# invocation. This is a general Dagster ephemeral-IO limitation.
DM="${PROJECT_NAME}.definitions"
uv run dagster asset materialize --select '+unified_customer_view' -m "$DM" 2>&1 | tail -10 || fail "materialize failed"

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. Synthesized 200 HubSpot contacts (seed=100)
  2. Synthesized 200 Salesforce contacts (seed=200)
  3. Outer-joined them on email → unified_customer_view

Inspect the reconciliation:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: hubspot_contacts + salesforce_contacts → unified_customer_view
    → unified_customer_view preview shows columns from both systems

Move to production (real HubSpot + Salesforce):
  1. Edit src/${PROJECT_NAME}/defs/hubspot_contacts/defs.yaml
       type: dagster_community_components.HubSpotIngestionComponent
       attributes:
         asset_name: hubspot_contacts
         api_key: "{{ env('HUBSPOT_API_KEY') }}"
         resources: [contacts]

  2. Edit src/${PROJECT_NAME}/defs/salesforce_contacts/defs.yaml
       type: dagster_community_components.SalesforceIngestionComponent
       attributes:
         asset_name: salesforce_contacts
         username: "{{ env('SALESFORCE_USERNAME') }}"
         password: "{{ env('SALESFORCE_PASSWORD') }}"
         security_token: "{{ env('SALESFORCE_SECURITY_TOKEN') }}"
         sf_objects: [Contact, Lead]

  3. Export the four env vars, rerun. The join asset needs zero changes.

Extension ideas:
  • Fuzzy match on name for contacts without email match — chain in
    'precision_match' or 'text_similarity' before the final join.
  • LLM-enrich the unified rows via 'langchain_chain_asset' — e.g.,
    reconcile the two systems' differing account descriptions into a
    canonical customer profile.
  • Reverse-ETL — pipe unified_customer_view to a sink component so
    the merged records flow back to HubSpot / Salesforce as the golden record.
EOF
