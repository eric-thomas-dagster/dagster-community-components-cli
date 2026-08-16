#!/usr/bin/env bash
# setup_stripe_demo.sh
#
# Stripe resource + sink demo — DataFrame → Stripe customers upsert
# with metadata-based dedup (Stripe doesn't have a native upsert endpoint;
# we search by `metadata.dagster_key` before creating).
#
# Components exercised:
#   • StripeResourceComponent            (~30-method resource, REST API)
#   • InlineDataframeComponent           (seed data)
#   • StripeCustomerUpsertComponent      (DataFrame → Stripe customers)
#
# Live-validated flow:
#   1. Verify token works (against Stripe test mode ideally — sk_test_...)
#   2. Scaffold Dagster project.
#   3. Materialize twice to prove idempotency.
#   4. Cleanup: delete every dagster-managed customer via metadata search.
#
# COST: $0 in test mode. Even in live mode, creating customers is free.
#
# REQUIREMENTS
#   • STRIPE_API_KEY — Stripe secret key. Use test mode (sk_test_...) for safety.
#   • uv, uvx
#
# USAGE
#   export STRIPE_API_KEY=sk_test_...
#   ./setup_stripe_demo.sh          # → stripe_demo/

set -eo pipefail

PROJECT_NAME="${1:-stripe_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${STRIPE_API_KEY:-}" ] && fail "STRIPE_API_KEY not set. Use sk_test_... from Stripe Dashboard → Developers → API Keys."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

if [[ "$STRIPE_API_KEY" == sk_live_* ]]; then
  echo -e "${C_RED}⚠${C_NC}  STRIPE_API_KEY is a LIVE key. This demo creates + deletes real customers."
  echo -e "${C_RED}   Ctrl-C now if you didn't mean it. Continuing in 5 seconds…${C_NC}"
  sleep 5
fi

info "Verifying Stripe token…"
ACCOUNT=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer $STRIPE_API_KEY" "https://api.stripe.com/v1/account")
HTTP=$(echo "$ACCOUNT" | tail -1)
[ "$HTTP" = "200" ] || fail "Stripe /account returned HTTP $HTTP. Check the key."
ok "Stripe token valid"

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

mkdir -p "src/${PROJECT_NAME}/defs/stripe_resource" \
         "src/${PROJECT_NAME}/defs/customers_seed" \
         "src/${PROJECT_NAME}/defs/customers_mirror"

cat > "src/${PROJECT_NAME}/defs/stripe_resource/defs.yaml" <<YAML
type: dagster_community_components.StripeResourceComponent
attributes:
  resource_key: stripe
  api_key_env_var: STRIPE_API_KEY
YAML

cat > "src/${PROJECT_NAME}/defs/customers_seed/defs.yaml" <<YAML
type: dagster_community_components.InlineDataframeComponent
attributes:
  asset_name: customers_seed
  columns: [customer_id, email, full_name, notes, plan_tier]
  rows:
    - [CUST-1001, "ada@example.com",     "Ada Lovelace",    "Prospect from Dagster webinar",   free]
    - [CUST-1002, "grace@example.com",   "Grace Hopper",    "Referred by ada@example.com",     pro]
    - [CUST-1003, "linus@example.com",   "Linus Torvalds",  "Signed up via marketing site",    free]
    - [CUST-1004, "guido@example.com",   "Guido van Rossum","Enterprise trial ends 2026-09-01",enterprise]
    - [CUST-1005, "brendan@example.com", "Brendan Eich",    "Churned Q2; re-engaged Aug 2026", pro]
  group_name: stripe_demo
YAML

cat > "src/${PROJECT_NAME}/defs/customers_mirror/defs.yaml" <<YAML
type: dagster_community_components.StripeCustomerUpsertComponent
attributes:
  asset_name: stripe_customers_mirror
  upstream_asset_key: customers_seed
  resource_key: stripe
  key_column: customer_id
  email_column: email
  name_column: full_name
  description_column: notes
  extra_metadata_columns: [plan_tier]
  metadata_key_field: dagster_key
  group_name: stripe_demo
YAML

ok "Wrote defs.yaml (3 components)"

DM="${PROJECT_NAME}.definitions"

info "Running dg check…"
uv run dg check defs 2>&1 | tail -3 || fail "dg check failed"

# Pre-clean any residual demo customers from earlier runs — the sink matches
# by email (immediately consistent) so residuals would show up as "updates"
# instead of "creates" on run 1, muddling the pass/fail signal.
info "Pre-cleanup: deleting any residual demo customers from earlier runs…"
uv run python - <<PY 2>&1 | tail -3
import os, requests
sess = requests.Session()
sess.headers.update({"Authorization": f"Bearer {os.environ['STRIPE_API_KEY']}"})
n = 0
for email in ("ada@example.com", "grace@example.com", "linus@example.com", "guido@example.com", "brendan@example.com"):
    r = sess.get("https://api.stripe.com/v1/customers", params={"email": email, "limit": 100})
    r.raise_for_status()
    for c in r.json().get("data", []):
        if (c.get("metadata") or {}).get("dagster_key", "").startswith("CUST-"):
            sess.delete(f"https://api.stripe.com/v1/customers/{c['id']}").raise_for_status()
            n += 1
print(f"  pre-cleaned {n} residual demo customers")
PY

info "Materializing pipeline (run 1 — expect 5 created)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 \
  | grep -E "Stripe upsert|STEP_SUCCESS|RUN_SUCCESS" | head -10 || fail "materialize failed"

info "Materializing pipeline (run 2 — expect 0 created, 5 updated)…"
uv run dagster asset materialize --select '*' -m "$DM" 2>&1 \
  | grep -E "Stripe upsert|STEP_SUCCESS|RUN_SUCCESS" | head -10 || fail "materialize failed (run 2)"

# ── Verify content in Stripe ────────────────────────────────────────────
info "Verifying content in Stripe…"
uv run python - <<PY || fail "Stripe verification failed"
import os, requests
sess = requests.Session()
sess.headers.update({"Authorization": f"Bearer {os.environ['STRIPE_API_KEY']}"})
# Use customers.list?email=X — immediately consistent (unlike /search which
# indexes eventually).
emails = ["ada@example.com", "grace@example.com", "linus@example.com", "guido@example.com", "brendan@example.com"]
by_key = {}
total_matched = 0
for email in emails:
    r = sess.get("https://api.stripe.com/v1/customers", params={"email": email, "limit": 100})
    r.raise_for_status()
    for c in r.json().get("data", []):
        k = (c.get("metadata") or {}).get("dagster_key", "")
        if not k.startswith("CUST-"):
            continue
        total_matched += 1
        prev = by_key.get(k)
        if prev is None or c.get("created", 0) >= prev.get("created", 0):
            by_key[k] = c
print(f"  {len(by_key)} unique CUST-* keys (out of {total_matched} matched customers)")
for k in sorted(by_key.keys()):
    c = by_key[k]
    tier = (c.get("metadata") or {}).get("plan_tier", "?")
    print(f"    {k}: {c['id']} email={c['email']!r} tier={tier}")
assert len(by_key) == 5, f"expected 5 unique keys, got {len(by_key)}"
assert total_matched == 5, f"expected 5 total matches (0 dupes), got {total_matched}"
print("  ✓ all Stripe content verified (5 unique keys, 0 duplicates)")
PY

# ── Cleanup: delete every dagster-managed customer ────────────────────
info "Cleaning up (deleting dagster-managed customers)…"
uv run python - <<PY 2>&1 | tail -3
import os, requests
sess = requests.Session()
sess.headers.update({"Authorization": f"Bearer {os.environ['STRIPE_API_KEY']}"})
# Use list?email=X (immediately consistent) — search is eventually consistent
# and often misses recently-created customers.
seen = set()
for email in ("ada@example.com","grace@example.com","linus@example.com","guido@example.com","brendan@example.com"):
    r = sess.get("https://api.stripe.com/v1/customers", params={"email": email, "limit": 100})
    r.raise_for_status()
    for c in r.json().get("data", []):
        dk = (c.get("metadata") or {}).get("dagster_key", "")
        if dk.startswith("CUST-") and c["id"] not in seen:
            sess.delete(f"https://api.stripe.com/v1/customers/{c['id']}").raise_for_status()
            seen.add(c["id"])
print(f"  deleted {len(seen)} demo customers")
PY

echo
ok "Demo complete."
echo
if [[ "$STRIPE_API_KEY" == sk_test_* ]]; then
  echo "  Stripe test dashboard: https://dashboard.stripe.com/test/customers"
else
  echo "  Stripe dashboard: https://dashboard.stripe.com/customers"
fi
echo
echo "  Dagster UI: uv run dg dev"
