#!/usr/bin/env bash
# setup_firebase_emulator_demo.sh
#
# Live Firebase demo — no Firebase account needed. Uses the official
# Firebase Emulator Suite (Firestore + Storage + Auth all local).
#
# What it demonstrates
#   • FirebaseResourceComponent talking to a local emulator via env vars
#   • FirestoreCollectionIngestionComponent — reads a Firestore collection
#     into a pandas DataFrame with where / limit / order_by support
#
# Cost: $0. Everything local. No cloud, no keys.
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • node.js 18+ (for firebase-tools)
#   • Java 21+ (`brew install openjdk@21` — the emulator suite requires it)
#
# Usage
#   ./setup_firebase_emulator_demo.sh                     # → firebase_demo/
#   ./setup_firebase_emulator_demo.sh my_project          # custom name

set -eo pipefail

PROJECT_NAME="${1:-firebase_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"
FB_DIR="${BASE_DIR}/${PROJECT_NAME}_firebase"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; cleanup; exit 1; }

EMU_PID=""
cleanup() {
  if [ -n "$EMU_PID" ] && kill -0 "$EMU_PID" 2>/dev/null; then
    kill "$EMU_PID" 2>/dev/null || true
  fi
}
trap cleanup INT TERM

command -v uvx >/dev/null 2>&1 || fail "uvx not found. Install uv."
command -v firebase >/dev/null 2>&1 || fail "firebase CLI not found. Install: npm install -g firebase-tools"
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"
[ -d "$FB_DIR" ] && fail "Directory exists: $FB_DIR"

info "Target Dagster project: $PROJECT_DIR"
info "Firebase emulator dir:  $FB_DIR"

# Make sure Java 21 is on PATH (emulator suite requires it)
export PATH="/opt/homebrew/opt/openjdk@21/bin:$PATH"
JAVA_VER=$(java -version 2>&1 | head -1 | awk -F'"' '{print $2}' | cut -d. -f1)
[ "${JAVA_VER:-0}" -lt 21 ] && fail "Java 21+ required (found $JAVA_VER). Install: brew install openjdk@21"

# ── Init the Firebase emulator config ──────────────────────────────────────
mkdir -p "$FB_DIR" && cd "$FB_DIR"
cat > firebase.json <<'JSON'
{
  "emulators": {
    "auth":      {"port": 9099},
    "firestore": {"port": 8080},
    "storage":   {"port": 9199},
    "ui":        {"enabled": true, "port": 4000}
  },
  "storage":   {"rules": "storage.rules"},
  "firestore": {"rules": "firestore.rules"}
}
JSON
cat > storage.rules   <<'RULES'
rules_version = '2'; service firebase.storage   { match /b/{bucket}/o { match /{allPaths=**} { allow read, write: if true; } } }
RULES
cat > firestore.rules <<'RULES'
rules_version = '2'; service cloud.firestore    { match /databases/{database}/documents { match /{document=**} { allow read, write: if true; } } }
RULES

info "Starting Firebase emulators (Firestore + Storage + Auth)…"
firebase emulators:start --project demo-project --only firestore,storage,auth > "$FB_DIR/emulator.log" 2>&1 &
EMU_PID=$!
for i in $(seq 1 30); do
  if lsof -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1 && lsof -iTCP:9199 -sTCP:LISTEN >/dev/null 2>&1; then
    ok "Emulators up in ${i}s (Firestore :8080, Storage :9199, UI http://localhost:4000)"
    break
  fi
  sleep 1
  [ "$i" -eq 30 ] && fail "Emulators didn't come up in 30s. Check $FB_DIR/emulator.log"
done

# ── Generate a valid stub service-account JSON for the resource ────────────
info "Generating stub service-account (emulator ignores creds contents)…"
/opt/homebrew/bin/python3.11 -m pip install --quiet firebase-admin cryptography 2>&1 | tail -1
STUB_CREDS="$BASE_DIR/${PROJECT_NAME}_firebase_creds.json"
/opt/homebrew/bin/python3.11 - "$STUB_CREDS" <<'PY'
import json, sys
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
with open(sys.argv[1], "w") as f:
    json.dump({
        "type": "service_account",
        "project_id": "demo-project",
        "private_key_id": "0" * 40,
        "private_key": pem,
        "client_email": "stub@demo-project.iam.gserviceaccount.com",
        "client_id": "0" * 21,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/stub",
    }, f)
print("wrote stub creds")
PY

# ── Seed 3 Firestore docs ──────────────────────────────────────────────────
info "Seeding 3 Firestore docs into the 'users' collection…"
FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 /opt/homebrew/bin/python3.11 - "$STUB_CREDS" <<'PY'
import os, sys
import firebase_admin
from firebase_admin import credentials, firestore
os.environ["FIRESTORE_EMULATOR_HOST"] = "127.0.0.1:8080"
cred = credentials.Certificate(sys.argv[1])
app = firebase_admin.initialize_app(cred, {"projectId": "demo-project"}, name="seed")
db = firestore.client(app)
for d in db.collection("users").stream():
    db.collection("users").document(d.id).delete()
db.collection("users").document("u1").set({"name": "Alice", "plan": "premium", "signup_date": "2024-01-15"})
db.collection("users").document("u2").set({"name": "Bob",   "plan": "basic",   "signup_date": "2024-02-20"})
db.collection("users").document("u3").set({"name": "Carol", "plan": "premium", "signup_date": "2024-03-10"})
print("3 users seeded")
PY
ok "Seeded"

# ── Scaffold Dagster project ────────────────────────────────────────────────
info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" 'firebase-admin>=6.0.0' 'pandas>=1.5.0' 'tabulate>=0.9.0' || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' 'firebase-admin>=6.0.0' 'pandas>=1.5.0' 'tabulate>=0.9.0' || fail "uv add failed"
fi
ok "Deps installed"

# ── defs.yaml ───────────────────────────────────────────────────────────────
mkdir -p "src/${PROJECT_NAME}/defs/firebase_resource" "src/${PROJECT_NAME}/defs/users_snapshot"

cat > "src/${PROJECT_NAME}/defs/firebase_resource/defs.yaml" <<YAML
type: dagster_community_components.FirebaseResourceComponent
attributes:
  resource_key: firebase
  project_id_env_var: FIREBASE_PROJECT_ID
  credentials_path_env_var: FIREBASE_CREDENTIALS_PATH
YAML

cat > "src/${PROJECT_NAME}/defs/users_snapshot/defs.yaml" <<YAML
type: dagster_community_components.FirestoreCollectionIngestionComponent
attributes:
  asset_name: users_snapshot
  resource_name: firebase
  collection_path: users
  where_clauses:
    - field: plan
      op: "=="
      value: premium
  group_name: firebase
YAML

ok "Wrote defs.yaml"

# ── Materialize ─────────────────────────────────────────────────────────────
export FIRESTORE_EMULATOR_HOST="127.0.0.1:8080"
export FIREBASE_STORAGE_EMULATOR_HOST="127.0.0.1:9199"
export FIREBASE_PROJECT_ID="demo-project"
export FIREBASE_CREDENTIALS_PATH="$STUB_CREDS"

DM="${PROJECT_NAME}.definitions"
info "Materializing users_snapshot (filters to plan=premium)…"
uv run dagster asset materialize --select users_snapshot -m "$DM" 2>&1 | tail -6 || fail "materialize failed"

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. Firebase Emulator Suite up on 127.0.0.1 (Firestore :8080, Storage :9199)
  2. Seeded 3 users in the 'users' Firestore collection
  3. Materialized users_snapshot (filtered where plan == 'premium') into a
     pandas DataFrame — 2 rows returned (Alice + Carol)

Live services (still running):
  • Firebase UI:  http://localhost:4000
  • Firestore:    http://127.0.0.1:8080
  • Storage:      http://127.0.0.1:9199

Open the Dagster UI:
  cd $PROJECT_NAME
  FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 \\
  FIREBASE_STORAGE_EMULATOR_HOST=127.0.0.1:9199 \\
  FIREBASE_PROJECT_ID=demo-project \\
  FIREBASE_CREDENTIALS_PATH=$STUB_CREDS \\
  uv run dg dev

To stop the emulators:
  kill $EMU_PID

To move to production Firebase:
  Drop the four FIREBASE_* env vars, point FIREBASE_CREDENTIALS_PATH at a
  real service account JSON from https://console.firebase.google.com/. Same
  defs.yaml, no code changes.
EOF

trap - INT TERM
