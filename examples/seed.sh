#!/usr/bin/env bash
# seed.sh
#
# Provision a Snowflake demo environment + (optional) AWS Iceberg external
# volume for a Dagster demo. Writes a .env file that bootstrap.sh consumes.
#
# Designed for two audiences:
#   • Eric (Dagster Labs) re-running against his own demo Snowflake account
#   • Snowflake customers replicating the demo in their own account
#
# What this does, in order:
#   1. Prompts for Snowflake creds (account, user, auth method) — supports
#      keypair / SSO / password / PAT / password+MFA. Warns on the methods
#      that can't be used by Dagster's daemon (sensors/schedules) headlessly.
#   2. Preflight: connects, verifies role + warehouse visible, checks for
#      Day-0 account governance gaps (account-level NETWORK_POLICY, user
#      DEFAULT_WAREHOUSE) — reports them but only auto-fixes if you pass
#      --demo-account (this is the flag you set when running against your
#      OWN throwaway account; customers leave it unset).
#   3. Detects collisions with any objects that already exist under the
#      target database and asks how to proceed.
#   4. Seeds the Snowflake side: DAGSTER_DEMO.{RAW,STAGING,ANALYTICS,AI}
#      with source tables (RAW), a dynamic table (STAGING) for the
#      snowflake_workspace component to import, the ANALYTICS landing
#      schema for downstream Dagster-managed assets, and an AI table for
#      Cortex demos.
#   5. (Optional, default-on if AWS CLI is authed) Provisions an S3 bucket
#      and IAM role, then performs the EXTERNAL VOLUME trust-policy dance
#      ENTIRELY PROGRAMMATICALLY — no copy-paste step. Ends with
#      SYSTEM$VERIFY_EXTERNAL_VOLUME and fails if any check ≠ PASSED.
#   6. Writes .env containing every value bootstrap.sh needs.
#
# Idempotent — re-running with the same database name and bucket name
# reuses existing resources. Use --reset to drop and recreate the database.
#
# Usage:
#   ./seed.sh                          # defaults: interactive, Iceberg auto-on if AWS authed
#   ./seed.sh --demo-account           # auto-fix Day-0 governance gaps (only against your own demo account)
#   ./seed.sh --with-iceberg=false     # skip Iceberg even if AWS CLI is authed
#   ./seed.sh --with-iceberg           # require Iceberg (fail if AWS CLI missing)
#   ./seed.sh --target-db MY_DEMO      # use a different database name
#   ./seed.sh --reset                  # drop and recreate target database
#   ./seed.sh --no-dagster-runner      # skip creating the scoped DAGSTER_RUNNER role
#   ./seed.sh --runner-role MY_ROLE    # use a different name for the scoped role
#   ./seed.sh --bucket my-iceberg-bkt  # override S3 bucket name (default: dagster-iceberg-<whoami>-<suffix>)
#   ./seed.sh --iam-role my-sf-role    # override IAM role name (default: snowflake-iceberg-role-<suffix>)
#   ./seed.sh --volume-name MY_VOL     # override Snowflake external volume name (default: DAGSTER_DEMO_VOLUME)
#   ./seed.sh --help                   # this message

set -eo pipefail

# Short-circuit --help before the TTY guard so the script is documentable
# without standing up a terminal.
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      sed -n '2,44p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
  esac
done

# ─── 0. Non-interactive guard ──────────────────────────────────────────────
if [ ! -t 0 ]; then
  cat <<'NONINTERACTIVE_GUARD'
════════════════════════════════════════════════════════════════════════
  seed.sh is interactive — it prompts for Snowflake credentials and
  cannot run from a curl|bash pipe.

  Download first, then run from a terminal:

    curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/seed.sh -o seed.sh
    chmod +x seed.sh
    ./seed.sh
════════════════════════════════════════════════════════════════════════
NONINTERACTIVE_GUARD
  exit 1
fi

# ─── 1. Flag parsing ───────────────────────────────────────────────────────
DEMO_ACCOUNT=false
WITH_ICEBERG="auto"          # auto | true | false
RESET_DB=false
TARGET_DB="${SNOWFLAKE_TARGET_DATABASE:-DAGSTER_DEMO}"
ENV_OUT="${SEED_ENV_OUT:-./.env}"
ENV_EXAMPLE_OUT="${ENV_OUT}.example"
CREATE_RUNNER_ROLE=true       # create + grant a scoped DAGSTER_RUNNER role
RUNNER_ROLE_NAME="DAGSTER_RUNNER"

while [ $# -gt 0 ]; do
  case "$1" in
    --demo-account)        DEMO_ACCOUNT=true; shift ;;
    --with-iceberg)        WITH_ICEBERG=true; shift ;;
    --with-iceberg=true)   WITH_ICEBERG=true; shift ;;
    --with-iceberg=false)  WITH_ICEBERG=false; shift ;;
    --reset)               RESET_DB=true; shift ;;
    --target-db)           TARGET_DB="$2"; shift 2 ;;
    --target-db=*)         TARGET_DB="${1#*=}"; shift ;;
    --env-out)             ENV_OUT="$2"; ENV_EXAMPLE_OUT="${2}.example"; shift 2 ;;
    --env-out=*)           ENV_OUT="${1#*=}"; ENV_EXAMPLE_OUT="${ENV_OUT}.example"; shift ;;
    --no-dagster-runner)   CREATE_RUNNER_ROLE=false; shift ;;
    --runner-role)         RUNNER_ROLE_NAME="$2"; shift 2 ;;
    --runner-role=*)       RUNNER_ROLE_NAME="${1#*=}"; shift ;;
    --bucket)              ICEBERG_S3_BUCKET_NAME="$2"; shift 2 ;;
    --bucket=*)            ICEBERG_S3_BUCKET_NAME="${1#*=}"; shift ;;
    --iam-role)            ICEBERG_ROLE_NAME="$2"; shift 2 ;;
    --iam-role=*)          ICEBERG_ROLE_NAME="${1#*=}"; shift ;;
    --volume-name)         ICEBERG_VOLUME_NAME="$2"; shift 2 ;;
    --volume-name=*)       ICEBERG_VOLUME_NAME="${1#*=}"; shift ;;
    -h|--help)
      sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

# ─── 2. uv guard (auto-install) ────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  echo "uv (Python package manager) is required and not installed."
  read -r -p "Install uv now? [Y/n] " ans
  case "${ans:-y}" in
    y|Y|yes) curl -fsSL https://astral.sh/uv/install.sh | sh
             . "$HOME/.local/bin/env" 2>/dev/null || true
             export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" ;;
    *)       echo "Aborted. Install uv from https://docs.astral.sh/uv/"; exit 1 ;;
  esac
fi

# ─── Banner ────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════════════"
echo "  Dagster Snowflake demo — environment seed"
echo "═══════════════════════════════════════════════════════════════════════"
echo "Creates database $TARGET_DB and (optionally) an AWS-backed Iceberg"
echo "external volume. Writes $ENV_OUT for bootstrap.sh to consume."
echo
echo "Mode:    $([ "$DEMO_ACCOUNT" = "true" ] && echo "DEMO ACCOUNT (auto-fix Day-0 gaps)" || echo "CUSTOMER (Day-0 gaps will prompt before fixing)")"
echo "Iceberg: $WITH_ICEBERG     Target DB: $TARGET_DB     Reset: $RESET_DB"
echo "Runner:  $([ "$CREATE_RUNNER_ROLE" = "true" ] && echo "create scoped role $RUNNER_ROLE_NAME" || echo "skipped — use prompted role for Dagster too")"
echo
read -r -p "Continue? [Y/n] " GO; case "${GO:-y}" in y|Y|yes) ;; *) echo "Aborted."; exit 0 ;; esac

# ─── 3. Snowflake creds + auth method ──────────────────────────────────────
echo
echo "─────────────────────────────────────────────────────────────────────"
echo "  Snowflake connection"
echo "─────────────────────────────────────────────────────────────────────"

prompt_default() {
  local prompt="$1" var="$2" def="$3" val
  if [ -n "$def" ]; then read -r -p "$prompt [$def]: " val; eval "$var=\"\${val:-$def}\""
  else                  read -r -p "$prompt: " val; eval "$var=\"$val\""
  fi
}

prompt_default "Snowflake account (e.g. xy12345.us-east-1 or org-account)" SNOW_ACCOUNT "${SNOWFLAKE_ACCOUNT:-}"
prompt_default "Username"                                                  SNOW_USER    "${SNOWFLAKE_USER:-}"

if [ -z "$SNOW_ACCOUNT" ] || [ -z "$SNOW_USER" ]; then
  echo "  ⚠ Account and username are required."; exit 1
fi

cat <<'AUTH_HELP'

Authentication method:
  [1] Keypair (RSA)            ✅ local + ✅ headless daemon  (RECOMMENDED)
  [2] PAT (programmatic token) ✅ local + ✅ headless daemon  (use if keypair blocked)
  [3] Password                 ✅ local + ✅ headless daemon  (universal fallback)
  [4] Password + MFA (TOTP)    ✅ local + ⚠  daemon only on machine with cached MFA token
  [5] SSO (externalbrowser)    ✅ local + ❌ daemon CANNOT drive a browser
AUTH_HELP
read -r -p "Choice [1]: " AUTH_CHOICE
AUTH_CHOICE="${AUTH_CHOICE:-1}"

SNOW_AUTH_METHOD="" SNOW_PASS="" SNOW_KEY_FILE="" SNOW_KEY_PWD=""
SNOW_PAT="" SNOW_MFA_PASSCODE=""
HEADLESS_OK=true   # tracked so we can warn in the .env file
case "$AUTH_CHOICE" in
  1|keypair)
    SNOW_AUTH_METHOD="keypair"
    prompt_default "Path to RSA private key (PEM)" SNOW_KEY_FILE \
      "${SNOWFLAKE_PRIVATE_KEY_FILE:-$HOME/.ssh/snowflake_rsa_key.p8}"
    [ -f "$SNOW_KEY_FILE" ] || { echo "  ⚠ Key file not found: $SNOW_KEY_FILE"; exit 1; }
    if [ -n "${SNOWFLAKE_PRIVATE_KEY_FILE_PWD:-}" ]; then
      SNOW_KEY_PWD="$SNOWFLAKE_PRIVATE_KEY_FILE_PWD"
      echo "Key passphrase: [using \$SNOWFLAKE_PRIVATE_KEY_FILE_PWD from env]"
    else
      read -r -s -p "Key passphrase (hidden; blank if key is unencrypted): " SNOW_KEY_PWD; echo
    fi ;;
  2|pat|PAT)
    SNOW_AUTH_METHOD="pat"
    if [ -n "${SNOWFLAKE_PAT:-}" ]; then SNOW_PAT="$SNOWFLAKE_PAT"; echo "PAT: [from env]"
    else read -r -s -p "Programmatic Access Token (hidden): " SNOW_PAT; echo
    fi
    [ -n "$SNOW_PAT" ] || { echo "  ⚠ PAT required."; exit 1; } ;;
  3|password)
    SNOW_AUTH_METHOD="password"
    if [ -n "${SNOWFLAKE_PASSWORD:-}" ]; then SNOW_PASS="$SNOWFLAKE_PASSWORD"; echo "Password: [from env]"
    else read -r -s -p "Password (hidden): " SNOW_PASS; echo
    fi
    [ -n "$SNOW_PASS" ] || { echo "  ⚠ Password required."; exit 1; } ;;
  4|mfa|password_mfa)
    SNOW_AUTH_METHOD="password_mfa"
    HEADLESS_OK=false
    if [ -n "${SNOWFLAKE_PASSWORD:-}" ]; then SNOW_PASS="$SNOWFLAKE_PASSWORD"; echo "Password: [from env]"
    else read -r -s -p "Password (hidden): " SNOW_PASS; echo
    fi
    echo "  Enter your 6-digit TOTP code (or press Enter to use Duo Push)."
    read -r -p "  MFA code: " SNOW_MFA_PASSCODE
    [ -n "$SNOW_PASS" ] || { echo "  ⚠ Password required."; exit 1; }
    echo "  ⚠ password+MFA caches an MFA token in the OS keychain. dg dev"
    echo "    on THIS machine will work, but the production Dagster daemon"
    echo "    needs to run on the same machine to reuse the cached token." ;;
  5|sso)
    SNOW_AUTH_METHOD="sso"
    HEADLESS_OK=false
    echo "  (SSO uses externalbrowser — a browser tab opens for auth)"
    echo "  ⚠ SSO works for dg dev on your laptop but the Dagster daemon"
    echo "    CANNOT drive a browser. Switch to keypair/PAT/password before"
    echo "    deploying schedules or sensors." ;;
  *) echo "  ⚠ Invalid choice."; exit 1 ;;
esac

prompt_default "Warehouse" SNOW_WAREHOUSE "${SNOWFLAKE_WAREHOUSE:-COMPUTE_WH}"
prompt_default "Role"      SNOW_ROLE      "${SNOWFLAKE_ROLE:-SYSADMIN}"

# Target schema for the demo objects (workspace component will point here).
SNOW_SCHEMA="STAGING"

# ─── 4. Preflight via Python ──────────────────────────────────────────────
# Single Python block does: connect → CURRENT_VERSION → role visible →
# warehouse visible → Day-0 governance (account network policy, user
# default warehouse) → target DB exists + collision detection.

echo
echo ">>> Preflight (connection, role/warehouse visibility, Day-0 governance, collisions) ..."

PRECHECK_OUT="$(mktemp -t sf_preflight.XXXXXX).json"

# Export everything the inline Python needs
export SF_ACCOUNT="$SNOW_ACCOUNT" SF_USER="$SNOW_USER" SF_PASS="$SNOW_PASS"
export SF_WAREHOUSE="$SNOW_WAREHOUSE" SF_ROLE="$SNOW_ROLE"
export SF_TARGET_DB="$TARGET_DB"
export SF_AUTH_METHOD="$SNOW_AUTH_METHOD"
export SF_KEY_FILE="$SNOW_KEY_FILE" SF_KEY_PWD="$SNOW_KEY_PWD"
export SF_PAT="$SNOW_PAT" SF_MFA_PASSCODE="$SNOW_MFA_PASSCODE"
export PRECHECK_OUT="$PRECHECK_OUT"
export SF_DEMO_ACCOUNT="$DEMO_ACCOUNT"

uv run --quiet --with 'snowflake-connector-python[secure-local-storage]' --no-project python - <<'PYEOF'
import json, os, sys
import snowflake.connector as sc

result = {
    "connected": False, "snowflake_version": None,
    "role_visible": False, "warehouse_visible": False,
    "current_role": None, "available_roles": [],
    "current_ip": None,
    "account_network_policy": None,
    "user_default_warehouse": None,
    "db_exists": False, "collisions": {},
    "errors": [],
}

ck = dict(
    account=os.environ['SF_ACCOUNT'],
    user=os.environ['SF_USER'],
    warehouse=os.environ['SF_WAREHOUSE'],
    role=os.environ.get('SF_ROLE') or None,
)
auth = os.environ.get('SF_AUTH_METHOD', 'password')
if auth == 'keypair':
    ck['authenticator'] = 'SNOWFLAKE_JWT'
    ck['private_key_file'] = os.environ['SF_KEY_FILE']
    if os.environ.get('SF_KEY_PWD'):
        ck['private_key_file_pwd'] = os.environ['SF_KEY_PWD']
elif auth == 'sso':
    ck['authenticator'] = 'externalbrowser'
elif auth == 'pat':
    ck['authenticator'] = 'PROGRAMMATIC_ACCESS_TOKEN'
    ck['token'] = os.environ.get('SF_PAT', '')
elif auth == 'password_mfa':
    ck['authenticator'] = 'username_password_mfa'
    ck['password'] = os.environ.get('SF_PASS', '')
    if os.environ.get('SF_MFA_PASSCODE'):
        ck['passcode'] = os.environ['SF_MFA_PASSCODE']
    ck['client_request_mfa_token'] = True
else:
    ck['password'] = os.environ.get('SF_PASS', '')

try:
    conn = sc.connect(**ck)
    result["connected"] = True
except Exception as e:
    result["errors"].append(f"connect: {e}")
    json.dump(result, open(os.environ['PRECHECK_OUT'], 'w'))
    sys.exit(0)

cur = conn.cursor()

def safe(q, label):
    try:
        cur.execute(q)
        return cur.fetchall()
    except Exception as e:
        result["errors"].append(f"{label}: {str(e)[:200]}")
        return []

rows = safe("SELECT CURRENT_VERSION(), CURRENT_ROLE(), CURRENT_AVAILABLE_ROLES(), CURRENT_IP_ADDRESS()", "session_info")
if rows:
    result["snowflake_version"] = rows[0][0]
    result["current_role"]      = rows[0][1]
    try:
        result["available_roles"] = json.loads(rows[0][2]) if rows[0][2] else []
    except Exception:
        result["available_roles"] = []
    result["current_ip"]        = rows[0][3]

# Role + warehouse visibility (separate from "can use" — connect() already proved that)
result["role_visible"]      = bool(safe(f"SHOW ROLES LIKE '{os.environ['SF_ROLE']}'", "role_check"))
result["warehouse_visible"] = bool(safe(f"SHOW WAREHOUSES LIKE '{os.environ['SF_WAREHOUSE']}'", "wh_check"))

# Day-0 governance
np = safe("SHOW PARAMETERS LIKE 'network_policy' IN ACCOUNT", "network_policy_check")
if np:
    # Row: [key, value, default, level, description, type]
    val = np[0][1] if len(np[0]) > 1 else ""
    result["account_network_policy"] = val if val else None

dw = safe(f"SHOW PARAMETERS LIKE 'default_warehouse' IN USER {os.environ['SF_USER']}", "default_wh_check")
if dw:
    val = dw[0][1] if len(dw[0]) > 1 else ""
    result["user_default_warehouse"] = val if val else None

# Target DB + collisions
db = os.environ['SF_TARGET_DB']
result["db_exists"] = bool(safe(f"SHOW DATABASES LIKE '{db}'", "db_check"))

if result["db_exists"]:
    SEED_OBJECTS = {
        "RAW.tables":             ["ORDERS", "CUSTOMERS", "PRODUCTS", "EVENTS"],
        "AI.tables":              ["CUSTOMER_FEEDBACK"],
        "STAGING.tables":         ["ORDERS_INGESTED"],
        "ANALYTICS.tables":       ["DAILY_REVENUE", "ALERT_LOG"],
        "STAGING.dynamic_tables": ["EVENTS_CLEANED_DT", "PAID_ORDERS_DT", "CUSTOMER_360_DT"],
        "STAGING.tasks":          ["DAILY_ORDERS_ROLLUP", "HOURLY_CUSTOMER_METRICS"],
    }
    queries = {
        "tables":         f"SELECT table_schema, table_name FROM {db}.INFORMATION_SCHEMA.TABLES WHERE table_schema IN ('RAW','STAGING','ANALYTICS','AI') AND table_type='BASE TABLE'",
        "dynamic_tables": f"SELECT schema_name, name FROM {db}.INFORMATION_SCHEMA.DYNAMIC_TABLES WHERE schema_name='STAGING'",
        "tasks":          f"SELECT schema_name, name FROM {db}.INFORMATION_SCHEMA.TASKS WHERE schema_name='STAGING'",
    }
    existing = {}
    for kind, q in queries.items():
        try:
            cur.execute(q)
            existing[kind] = {f"{r[0]}.{r[1]}" for r in cur.fetchall()}
        except Exception:
            existing[kind] = set()
    for slot, names in SEED_OBJECTS.items():
        schema, kind = slot.split(".", 1)
        live = existing.get(kind, set())
        hits = sorted(n for n in names if f"{schema}.{n}" in live)
        if hits:
            result["collisions"][slot] = hits

cur.close(); conn.close()
json.dump(result, open(os.environ['PRECHECK_OUT'], 'w'))
PYEOF

if [ ! -s "$PRECHECK_OUT" ]; then
  echo "  ⚠ Preflight returned no result. Aborting."
  exit 1
fi

# Pretty-print findings and decide what to do.
PYTHONIOENCODING=utf-8 python3 - "$PRECHECK_OUT" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1]))

def line(prefix, ok, val):
    mark = "✓" if ok else "✗"
    print(f"    {mark} {prefix:34}  {val}")

if r["errors"]:
    print("  Errors during preflight:")
    for e in r["errors"]:
        print(f"    ! {e}")

if not r["connected"]:
    print("  ✗ Could not connect to Snowflake. See errors above. Aborting.")
    sys.exit(2)

print("  Connection:")
line("Snowflake version",     True, r["snowflake_version"])
line("Active role",           bool(r["current_role"]),            r["current_role"])
line(f"Role visible (SHOW)",  r["role_visible"],                  "yes" if r["role_visible"] else "NO — pick a different role")
line("Warehouse visible",     r["warehouse_visible"],             "yes" if r["warehouse_visible"] else "NO — pick a different warehouse")
line("Available roles",       bool(r["available_roles"]),         ", ".join(r["available_roles"]) or "(none seen)")
line("Your IP (per Snowflake)", bool(r["current_ip"]),             r["current_ip"])

print("  Day-0 governance:")
np_ok = bool(r["account_network_policy"])
line("Account network policy", np_ok,
     r["account_network_policy"] or "NOT SET (CIS 3.1 fail)")
dw_ok = bool(r["user_default_warehouse"])
line("User default warehouse", dw_ok,
     r["user_default_warehouse"] or "NOT SET (sessions need explicit USE WAREHOUSE)")

print("  Target database:")
line(f"DAGSTER_DEMO exists?", True, "yes" if r["db_exists"] else "no")
if r["collisions"]:
    print("  Collisions detected (these objects will be overwritten by CREATE OR REPLACE):")
    for slot, hits in r["collisions"].items():
        print(f"      {slot}: {', '.join(hits)}")

# Decision codes printed last for the bash side to grep.
codes = []
if not r["role_visible"]:        codes.append("ROLE_MISSING")
if not r["warehouse_visible"]:   codes.append("WAREHOUSE_MISSING")
if not r["account_network_policy"]: codes.append("NO_NETWORK_POLICY")
if not r["user_default_warehouse"]: codes.append("NO_DEFAULT_WAREHOUSE")
if r["collisions"]:              codes.append(f"COLLISIONS:{sum(len(v) for v in r['collisions'].values())}")
print("VERDICT:" + (",".join(codes) if codes else "OK"))
PYEOF
PRECHECK_RC=$?

VERDICT=$(grep '^VERDICT:' "$PRECHECK_OUT.parsed" 2>/dev/null || \
          PYTHONIOENCODING=utf-8 python3 -c "
import json,sys
r=json.load(open('$PRECHECK_OUT'))
c=[]
if not r['role_visible']: c.append('ROLE_MISSING')
if not r['warehouse_visible']: c.append('WAREHOUSE_MISSING')
if not r['account_network_policy']: c.append('NO_NETWORK_POLICY')
if not r['user_default_warehouse']: c.append('NO_DEFAULT_WAREHOUSE')
if r['collisions']: c.append('COLLISIONS:'+str(sum(len(v) for v in r['collisions'].values())))
print('VERDICT:'+(','.join(c) if c else 'OK'))
")
VERDICT="${VERDICT#VERDICT:}"

echo
echo "  Verdict: $VERDICT"

# Hard-stops + offers
CREATE_WAREHOUSE_FIRST=false
case ",$VERDICT," in
  *,ROLE_MISSING,*)
    echo "  ✗ Role '$SNOW_ROLE' isn't visible to your user."
    echo "    Available roles: $(python3 -c "import json;print(','.join(json.load(open('$PRECHECK_OUT'))['available_roles']))")"
    echo "    Re-run and pick a visible role at the prompt."
    exit 1 ;;
esac
case ",$VERDICT," in
  *,WAREHOUSE_MISSING,*)
    echo
    echo "  ⚠ Warehouse '$SNOW_WAREHOUSE' isn't visible (or doesn't exist)."
    echo "    Options:"
    echo "      [c] Create it now — XSMALL, AUTO_SUSPEND=60s, AUTO_RESUME=TRUE"
    echo "          (effectively free when idle; ~1 credit/hour when running)"
    echo "      [r] Re-run and pick a different warehouse"
    echo "      [q] Quit"
    DEFAULT_WH_ANSWER=$([ "$DEMO_ACCOUNT" = "true" ] && echo "c" || echo "c")
    while :; do
      read -r -p "    [$DEFAULT_WH_ANSWER]: " WH_ANS
      WH_ANS="${WH_ANS:-$DEFAULT_WH_ANSWER}"
      case "$WH_ANS" in
        c|C) CREATE_WAREHOUSE_FIRST=true; echo "    → will CREATE WAREHOUSE $SNOW_WAREHOUSE during seed"; break ;;
        r|R) echo "    → exiting; re-run with the correct warehouse"; exit 0 ;;
        q|Q) echo "  Aborted."; exit 0 ;;
        *)   echo "    Pick c, r, or q." ;;
      esac
    done ;;
esac

# Collision handling
case ",$VERDICT," in
  *,COLLISIONS:*)
    echo
    echo "  Some seed object names are already in use under $TARGET_DB."
    if [ "$RESET_DB" = "true" ]; then
      echo "  --reset specified → will DROP DATABASE $TARGET_DB before seeding."
      DROP_FIRST=true
    else
      while :; do
        read -r -p "  [o]verwrite (CREATE OR REPLACE) / [d]rop database first / [c]hange target db / [q]uit: " CH
        case "${CH:-q}" in
          o|O) echo "  → overwriting"; break ;;
          d|D) echo "  → DROP DATABASE first"; DROP_FIRST=true; break ;;
          c|C) prompt_default "New target database name" TARGET_DB "DAGSTER_DEMO_$(date +%s)"
               export SF_TARGET_DB="$TARGET_DB"
               echo "  → using $TARGET_DB; re-running preflight on next iteration not implemented in v1, proceeding"
               break ;;
          q|Q) echo "  Aborted."; exit 0 ;;
          *) echo "    Pick o, d, c, or q." ;;
        esac
      done
    fi ;;
esac

# Day-0 governance handling
NEED_NETWORK_POLICY=false
NEED_DEFAULT_WAREHOUSE=false
case ",$VERDICT," in
  *,NO_NETWORK_POLICY,*)   NEED_NETWORK_POLICY=true ;;
esac
case ",$VERDICT," in
  *,NO_DEFAULT_WAREHOUSE,*) NEED_DEFAULT_WAREHOUSE=true ;;
esac

if [ "$NEED_NETWORK_POLICY" = "true" ] || [ "$NEED_DEFAULT_WAREHOUSE" = "true" ]; then
  echo
  echo "  ⚠ Day-0 account governance gaps detected:"
  [ "$NEED_NETWORK_POLICY" = "true" ]    && echo "      • No account-level network policy (CIS Snowflake 3.1 violation)."
  [ "$NEED_DEFAULT_WAREHOUSE" = "true" ] && echo "      • User $SNOW_USER has no DEFAULT_WAREHOUSE set"
  [ "$NEED_DEFAULT_WAREHOUSE" = "true" ] && echo "        (sessions need an explicit USE WAREHOUSE statement to run anything)"

  if [ "$DEMO_ACCOUNT" = "true" ]; then
    echo
    echo "    --demo-account is set → these WILL be auto-fixed during seeding."
  else
    echo
    echo "    These are ACCOUNT-LEVEL changes that affect every user on this account."
    echo "    The script can fix them, but only with your consent. What it would do:"
    [ "$NEED_NETWORK_POLICY" = "true" ] && cat <<'NP'
      • CREATE OR REPLACE NETWORK POLICY DAGSTER_DEMO_NETWORK_POLICY
            ALLOWED_IP_LIST = ('0.0.0.0/0')   -- permissive default; tighten for prod
        ALTER ACCOUNT SET NETWORK_POLICY = DAGSTER_DEMO_NETWORK_POLICY;
NP
    [ "$NEED_DEFAULT_WAREHOUSE" = "true" ] && cat <<DW
      • ALTER USER $SNOW_USER SET DEFAULT_WAREHOUSE = $SNOW_WAREHOUSE;
DW
    echo
    while :; do
      read -r -p "    Apply these account-level fixes? [y/N] " GOV_ANS
      case "${GOV_ANS:-n}" in
        y|Y|yes) echo "    → will apply during seed (requires ACCOUNTADMIN)"; DEMO_ACCOUNT=true; break ;;
        n|N|no|"")
          echo "    → skipping. Re-run any time with --demo-account, or apply the SQL above manually."
          NEED_NETWORK_POLICY=false
          NEED_DEFAULT_WAREHOUSE=false
          break ;;
        *) echo "    Pick y or n." ;;
      esac
    done
  fi
fi

# ─── 5. Seed Snowflake ─────────────────────────────────────────────────────
echo
echo ">>> Seeding $TARGET_DB ..."

export SF_DROP_FIRST="${DROP_FIRST:-false}"
export SF_NEED_NETWORK_POLICY="$NEED_NETWORK_POLICY"
export SF_NEED_DEFAULT_WAREHOUSE="$NEED_DEFAULT_WAREHOUSE"
export SF_CREATE_WAREHOUSE_FIRST="$CREATE_WAREHOUSE_FIRST"
export SF_CREATE_RUNNER_ROLE="$CREATE_RUNNER_ROLE"
export SF_RUNNER_ROLE_NAME="$RUNNER_ROLE_NAME"
SEED_STATE_FILE="$(mktemp -t sf_seed_state.XXXXXX).json"
export SF_SEED_STATE_FILE="$SEED_STATE_FILE"

# Resolve seed.sql — prefer the one next to this script.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SQL_FILE="$SCRIPT_DIR/seed.sql"
if [ ! -f "$SQL_FILE" ]; then
  echo "  ✗ seed.sql not found at $SQL_FILE."
  echo "    The seed Snowflake objects are defined there. Make sure you have both"
  echo "    seed.sh AND seed.sql in the same directory."
  exit 1
fi
echo "    Using SQL file: $SQL_FILE ($(wc -l < "$SQL_FILE" | tr -d ' ') lines)"

# Substitute the target database name + warehouse so all references match.
SUBST_SQL="$(mktemp -t sf_seed_subst.XXXXXX).sql"
python3 -c "
import re, sys
src = open(sys.argv[1]).read()
target_db = sys.argv[2]; target_wh = sys.argv[3]
out = src.replace('DAGSTER_DEMO', target_db)
if target_wh != 'COMPUTE_WH':
    out = re.sub(r'\bWAREHOUSE\s*=\s*COMPUTE_WH\b', f'WAREHOUSE = {target_wh}', out)
open(sys.argv[4], 'w').write(out)
" "$SQL_FILE" "$TARGET_DB" "$SNOW_WAREHOUSE" "$SUBST_SQL"
export SF_SQL_FILE="$SUBST_SQL"

uv run --quiet --with 'snowflake-connector-python[secure-local-storage]' --no-project python - <<'PYEOF'
"""
Seed the Snowflake side of the demo. One statement per execute() call so we
get clean per-statement error reporting.

Connected-DAG backbone:
    RAW.EVENTS              (source — Dagster Python asset writes here)
    RAW.ORDERS, CUSTOMERS, PRODUCTS  (additional source tables, pre-seeded)
    STAGING.EVENTS_CLEANED_DT       (DYNAMIC TABLE — imported by snowflake_workspace)
    STAGING.DAILY_ORDERS_ROLLUP     (TASK — also imported by snowflake_workspace)
    ANALYTICS                       (empty — Dagster warehouse_pipeline writes here)
    AI.CUSTOMER_FEEDBACK            (for snowflake_cortex_asset demos)
"""
import os, sys, time
import snowflake.connector as sc

ck = dict(
    account=os.environ['SF_ACCOUNT'], user=os.environ['SF_USER'],
    warehouse=os.environ['SF_WAREHOUSE'], role=os.environ.get('SF_ROLE') or None,
)
auth = os.environ['SF_AUTH_METHOD']
if   auth == 'keypair':      ck.update(authenticator='SNOWFLAKE_JWT', private_key_file=os.environ['SF_KEY_FILE'])
elif auth == 'sso':          ck.update(authenticator='externalbrowser')
elif auth == 'pat':          ck.update(authenticator='PROGRAMMATIC_ACCESS_TOKEN', token=os.environ['SF_PAT'])
elif auth == 'password_mfa': ck.update(authenticator='username_password_mfa',
                                        password=os.environ.get('SF_PASS',''),
                                        passcode=os.environ.get('SF_MFA_PASSCODE') or None,
                                        client_request_mfa_token=True)
else:                        ck['password'] = os.environ.get('SF_PASS','')
if auth == 'keypair' and os.environ.get('SF_KEY_PWD'):
    ck['private_key_file_pwd'] = os.environ['SF_KEY_PWD']

db          = os.environ['SF_TARGET_DB']
wh          = os.environ['SF_WAREHOUSE']
role        = os.environ.get('SF_ROLE') or 'SYSADMIN'
demo        = os.environ.get('SF_DEMO_ACCOUNT') == 'true'
drop_first  = os.environ.get('SF_DROP_FIRST') == 'true'
create_wh   = os.environ.get('SF_CREATE_WAREHOUSE_FIRST') == 'true'
make_runner = os.environ.get('SF_CREATE_RUNNER_ROLE') == 'true'
runner_role = os.environ.get('SF_RUNNER_ROLE_NAME', 'DAGSTER_RUNNER')
runner_created = False

conn = sc.connect(**ck)
cur  = conn.cursor()

def run(sql, label=None):
    try:
        t0 = time.time()
        cur.execute(sql)
        try: cur.fetchall()
        except Exception: pass
        print(f"    ✓ {label or sql[:80]}  ({time.time()-t0:.1f}s)")
    except Exception as e:
        msg = str(e).split('\n')[0]
        print(f"    ✗ {label or sql[:80]}  → {msg}")
        return False
    return True

# ── Day-0 fixes (only if --demo-account) ────────────────────────────────
if demo and os.environ.get('SF_NEED_NETWORK_POLICY') == 'true':
    print("  Day-0: creating + attaching DAGSTER_DEMO_NETWORK_POLICY ...")
    run("USE ROLE ACCOUNTADMIN", "USE ROLE ACCOUNTADMIN (day-0)")
    cur.execute("SELECT CURRENT_ROLE()")
    if cur.fetchone()[0] != "ACCOUNTADMIN":
        print("    ⚠ Couldn't switch to ACCOUNTADMIN; skipping network policy. Grant it and re-run with --demo-account.")
    else:
        run("""CREATE OR REPLACE NETWORK POLICY DAGSTER_DEMO_NETWORK_POLICY
                 ALLOWED_IP_LIST = ('0.0.0.0/0')
                 COMMENT = 'Permissive default. Tighten ALLOWED_IP_LIST for production.'""",
            "CREATE NETWORK POLICY")
        run("ALTER ACCOUNT SET NETWORK_POLICY = DAGSTER_DEMO_NETWORK_POLICY",
            "ALTER ACCOUNT SET NETWORK_POLICY")

if demo and os.environ.get('SF_NEED_DEFAULT_WAREHOUSE') == 'true':
    print(f"  Day-0: setting DEFAULT_WAREHOUSE on user {os.environ['SF_USER']} ...")
    run("USE ROLE ACCOUNTADMIN", "USE ROLE ACCOUNTADMIN (day-0)")
    cur.execute("SELECT CURRENT_ROLE()")
    if cur.fetchone()[0] != "ACCOUNTADMIN":
        print("    ⚠ Need ACCOUNTADMIN; skipping default warehouse.")
    else:
        run(f"ALTER USER {os.environ['SF_USER']} SET DEFAULT_WAREHOUSE = {wh}",
            f"ALTER USER … SET DEFAULT_WAREHOUSE = {wh}")

# ── Switch back to working role + warehouse before DDL ─────────────────
run(f"USE ROLE {role}",          f"USE ROLE {role}")
cur.execute("SELECT CURRENT_ROLE()")
actual = cur.fetchone()[0]
if actual != role:
    print(f"    ⚠ USE ROLE {role} silently no-op'd. Active role is {actual}.")
    print(f"      Available roles for this user (from preflight): see above.")
    print(f"      Falling back to {actual} — some DDL may fail if it requires {role}.")

# Create the warehouse if the user opted in at the preflight prompt.
if create_wh:
    run(f"""CREATE WAREHOUSE IF NOT EXISTS {wh}
              WAREHOUSE_SIZE = XSMALL
              AUTO_SUSPEND = 60
              AUTO_RESUME = TRUE
              INITIALLY_SUSPENDED = FALSE
              COMMENT = 'Created by seed.sh for Dagster demo'""",
        f"CREATE WAREHOUSE {wh} (XSMALL, AUTO_SUSPEND=60s)")

run(f"USE WAREHOUSE {wh}",       f"USE WAREHOUSE {wh}")
cur.execute("SELECT CURRENT_WAREHOUSE()")
if not cur.fetchone()[0]:
    print(f"    ✗ No active warehouse after USE WAREHOUSE {wh}.")
    sys.exit(1)

# ── Optional drop ──────────────────────────────────────────────────────
if drop_first:
    run(f"DROP DATABASE IF EXISTS {db}", f"DROP DATABASE IF EXISTS {db}")

# ── Execute seed.sql statement-by-statement ────────────────────────────
# seed.sql contains the full comprehensive seed (4 RAW tables, 4 dynamic
# tables, 6 tasks, 3 stored procs, 2 streams, 1 MV, 1 stage, 1 snowpipe,
# 1 alert, ANALYTICS sink, AI table). Parse it with a $$-aware splitter
# so Snowpark Python procedures (which embed `;` inside $$ blocks) don't
# trip the tokenizer.
import re
with open(os.environ['SF_SQL_FILE']) as f:
    sql_text = f.read()

statements, buf, in_dollar = [], [], False
for line in sql_text.splitlines():
    stripped = line.strip()
    if not in_dollar and (stripped.startswith('--') or not stripped):
        continue
    buf.append(line)
    if line.count('$$') % 2 == 1:
        in_dollar = not in_dollar
    if not in_dollar and stripped.endswith(';'):
        statements.append('\n'.join(buf))
        buf = []
if buf:
    statements.append('\n'.join(buf))

print(f"  Running {len(statements)} statements from seed.sql ...")
failures = 0
for i, stmt in enumerate(statements, 1):
    s = stmt.strip()
    if not s or s == ';': continue
    label_match = re.search(
        r'^\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|TASK|DYNAMIC\s+TABLE|STREAM|STAGE|PROCEDURE|MATERIALIZED\s+VIEW|PIPE|ALERT|DATABASE|SCHEMA|FUNCTION|VIEW|FILE\s+FORMAT)\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+))',
        s, re.IGNORECASE | re.MULTILINE)
    label = (label_match.group(0).strip().replace('\n', ' ')[:90]
             if label_match else s.split('\n')[0][:90])
    try:
        t0 = time.time()
        cur.execute(s)
        try: cur.fetchall()
        except Exception: pass
        marker = "✓" if time.time()-t0 < 5 else "✓✓"
        print(f"    [{i:3}/{len(statements)}] {marker} {label}  ({time.time()-t0:.1f}s)")
    except Exception as e:
        failures += 1
        msg = str(e).split('\n')[0][:140]
        print(f"    [{i:3}/{len(statements)}] ✗ {label}  → {msg}")

if failures:
    print(f"  ⚠ {failures} statement(s) failed — most likely a privilege issue on '{role}'.")
    print(f"    Try SYSADMIN at the prompt next run, or grant the missing privileges.")

# ── DAGSTER_RUNNER: scoped runtime role for Dagster ─────────────────────
# Creating the role itself requires USERADMIN; granting roles to users
# requires SECURITYADMIN. We try ACCOUNTADMIN here because it has both
# (and is in CURRENT_AVAILABLE_ROLES for any account where you're an admin).
# If the user doesn't have ACCOUNTADMIN, we fall back to whatever role they
# came in with and skip parts we can't do — the .env writer will detect
# whether the role got created.
if make_runner:
    print(f"  Creating scoped runtime role {runner_role} ...")
    cur.execute("USE ROLE ACCOUNTADMIN")
    cur.execute("SELECT CURRENT_ROLE()")
    current = cur.fetchone()[0]
    if current != "ACCOUNTADMIN":
        print(f"    ⚠ USE ROLE ACCOUNTADMIN didn't take effect (active role: {current}).")
        print(f"      Skipping {runner_role}. Dagster will use {role} — works, just not least-privilege.")
        print("RUNNER_ROLE_CREATED=false")
    else:
        # Re-assert ACCOUNTADMIN right before each grant to defend against
        # any session-state weirdness. Snowflake records `granted_by` as the
        # role at GRANT time — and we've seen this silently slide back to
        # the DDL role on some accounts, producing surprising INFORMATION_SCHEMA
        # visibility issues for DAGSTER_RUNNER.
        run(f"CREATE ROLE IF NOT EXISTS {runner_role} COMMENT = 'Scoped runtime role for Dagster demo'",
            f"CREATE ROLE {runner_role}")
        # Grant role to the user running this seed so they can switch into it.
        run(f"GRANT ROLE {runner_role} TO USER {os.environ['SF_USER']}",
            f"GRANT ROLE {runner_role} TO USER {os.environ['SF_USER']}")
        # Warehouse usage.
        run(f"GRANT USAGE ON WAREHOUSE {wh} TO ROLE {runner_role}",
            f"GRANT USAGE ON WAREHOUSE {wh}")
        # Database usage + schema usage on the four demo schemas.
        run(f"GRANT USAGE ON DATABASE {db} TO ROLE {runner_role}",
            f"GRANT USAGE ON DATABASE {db}")
        for s in ("RAW", "STAGING", "ANALYTICS", "AI"):
            run(f"GRANT USAGE ON SCHEMA {db}.{s} TO ROLE {runner_role}", f"GRANT USAGE ON SCHEMA {s}")
        # Source data — read-only.
        for s in ("RAW", "AI"):
            run(f"GRANT SELECT ON ALL TABLES IN SCHEMA {db}.{s} TO ROLE {runner_role}",
                f"GRANT SELECT ON ALL TABLES IN {s}")
            run(f"GRANT SELECT ON FUTURE TABLES IN SCHEMA {db}.{s} TO ROLE {runner_role}",
                f"GRANT SELECT ON FUTURE TABLES IN {s}")
        # STAGING — read existing (workspace component imports tasks / dynamic tables / etc.)
        # plus operate on tasks (so Dagster can trigger them) and refresh dynamic tables.
        run(f"GRANT SELECT ON ALL TABLES IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
            "GRANT SELECT ON ALL TABLES IN STAGING")
        run(f"GRANT SELECT ON FUTURE TABLES IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
            "GRANT SELECT ON FUTURE TABLES IN STAGING")
        run(f"GRANT SELECT ON ALL DYNAMIC TABLES IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
            "GRANT SELECT ON ALL DYNAMIC TABLES IN STAGING")
        run(f"GRANT SELECT ON FUTURE DYNAMIC TABLES IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
            "GRANT SELECT ON FUTURE DYNAMIC TABLES IN STAGING")
        # MONITOR — required for INFORMATION_SCHEMA.DYNAMIC_TABLES to expose
        # the DT to this role. The workspace component reads metadata from
        # INFORMATION_SCHEMA after every refresh; without MONITOR it fails
        # with "Object DAGSTER_DEMO.INFORMATION_SCHEMA.DYNAMIC_TABLES does
        # not exist or not authorized" even though SELECT was granted.
        run(f"GRANT MONITOR ON ALL DYNAMIC TABLES IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
            "GRANT MONITOR ON ALL DYNAMIC TABLES IN STAGING")
        run(f"GRANT MONITOR ON FUTURE DYNAMIC TABLES IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
            "GRANT MONITOR ON FUTURE DYNAMIC TABLES IN STAGING")
        run(f"GRANT OPERATE ON ALL TASKS IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
            "GRANT OPERATE ON ALL TASKS IN STAGING")
        run(f"GRANT OPERATE ON FUTURE TASKS IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
            "GRANT OPERATE ON FUTURE TASKS IN STAGING")
        # Same MONITOR rationale for tasks (workspace reads task metadata
        # from INFORMATION_SCHEMA.TASKS after EXECUTE TASK).
        run(f"GRANT MONITOR ON ALL TASKS IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
            "GRANT MONITOR ON ALL TASKS IN STAGING")
        run(f"GRANT MONITOR ON FUTURE TASKS IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
            "GRANT MONITOR ON FUTURE TASKS IN STAGING")
        # ── Bulk-grantable object types — procedures / streams / MVs /
        # stages / alerts all accept `GRANT ... ON ALL ... IN SCHEMA`.
        # PIPES do NOT — Snowflake restricts bulk grants on pipes
        # ("Bulk grant on objects of type PIPE to ROLE is restricted").
        # Pipes are granted per-object below.
        for kind, privs in [
            ("PROCEDURES",          "USAGE"),
            ("STREAMS",             "SELECT"),
            ("MATERIALIZED VIEWS",  "SELECT, REFERENCES"),
            ("STAGES",              "USAGE, READ"),
            ("ALERTS",              "OPERATE, MONITOR"),
        ]:
            run(f"GRANT {privs} ON ALL {kind} IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
                f"GRANT {privs} ON ALL {kind} IN STAGING")
            run(f"GRANT {privs} ON FUTURE {kind} IN SCHEMA {db}.STAGING TO ROLE {runner_role}",
                f"GRANT {privs} ON FUTURE {kind} IN STAGING")

        # Pipes — must be granted per-pipe. Enumerate what the seed created.
        for pipe in ("ORDERS_MANUAL_INGEST_PIPE", "ORDERS_AUTO_INGEST_PIPE"):
            run(f"GRANT MONITOR, OPERATE ON PIPE {db}.STAGING.{pipe} TO ROLE {runner_role}",
                f"GRANT MONITOR, OPERATE ON PIPE {pipe}")
        # ANALYTICS — Dagster's read+write playground. CREATE/INSERT/UPDATE/DELETE.
        for priv in ("CREATE TABLE", "CREATE DYNAMIC TABLE", "CREATE ICEBERG TABLE", "CREATE VIEW"):
            run(f"GRANT {priv} ON SCHEMA {db}.ANALYTICS TO ROLE {runner_role}",
                f"GRANT {priv} ON ANALYTICS")
        run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {db}.ANALYTICS TO ROLE {runner_role}",
            "GRANT SELECT/INSERT/UPDATE/DELETE ON ALL TABLES IN ANALYTICS")
        run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON FUTURE TABLES IN SCHEMA {db}.ANALYTICS TO ROLE {runner_role}",
            "GRANT SELECT/INSERT/UPDATE/DELETE ON FUTURE TABLES IN ANALYTICS")
        # Compute pools for Snowpark (if/when used) — skipped here; bootstrap can grant later.
        print(f"    ✓ Scoped role {runner_role} ready. Bootstrap will write it to .env.")
        runner_created = True
        # Switch back to the working role so subsequent statements (if any)
        # don't run as ACCOUNTADMIN.
        run(f"USE ROLE {role}", f"USE ROLE {role} (revert from ACCOUNTADMIN)")

# Write final state for the bash side + later Iceberg / .env steps.
import json
state = {
    "runner_role_created": bool(runner_created),
    "runner_role_name":    runner_role if runner_created else role,  # role Dagster should use
    "ddl_role":            role,
    "warehouse_created":   create_wh,
}
with open(os.environ['SF_SEED_STATE_FILE'], 'w') as f:
    json.dump(state, f)

cur.close(); conn.close()
print("  ✓ Snowflake seed complete.")
PYEOF
SEED_RC=$?
if [ $SEED_RC -ne 0 ]; then
  echo "  ✗ Seed failed. Fix the error above and re-run."
  exit $SEED_RC
fi

# Read final state from the seed Python.
if [ -s "$SEED_STATE_FILE" ]; then
  RUNNER_ROLE_CREATED=$(python3 -c "import json; print(json.load(open('$SEED_STATE_FILE'))['runner_role_created'])")
  DAGSTER_RUNTIME_ROLE=$(python3 -c "import json; print(json.load(open('$SEED_STATE_FILE'))['runner_role_name'])")
else
  RUNNER_ROLE_CREATED=false
  DAGSTER_RUNTIME_ROLE="$SNOW_ROLE"
fi

# ─── 6. Iceberg (optional) ────────────────────────────────────────────────
ICEBERG_VOLUME=""
ICEBERG_S3_BUCKET=""
ICEBERG_ROLE_ARN=""

if [ "$WITH_ICEBERG" = "false" ]; then
  echo
  echo ">>> Skipping Iceberg setup (--with-iceberg=false)."
elif [ "$WITH_ICEBERG" = "true" ] || ([ "$WITH_ICEBERG" = "auto" ] && command -v aws >/dev/null 2>&1); then
  if ! command -v aws >/dev/null 2>&1; then
    echo
    echo "  ⚠ --with-iceberg=true but AWS CLI not installed. Skipping."
    echo "    Install with 'brew install awscli', run 'aws configure', and re-run."
  elif ! aws sts get-caller-identity >/dev/null 2>&1; then
    echo
    echo "  ⚠ AWS CLI installed but not authenticated. Skipping Iceberg."
    echo "    Run 'aws configure' or 'aws sso login' and re-run."
  else
    echo
    echo ">>> Iceberg setup: provisioning S3 bucket + IAM role + external volume ..."

    AWS_ACCT=$(aws sts get-caller-identity --query Account --output text)
    AWS_REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
    AWS_IDENT=$(aws sts get-caller-identity --query Arn --output text)
    SUFFIX=$(date +%s | tail -c 5)
    ICEBERG_S3_BUCKET="${ICEBERG_S3_BUCKET_NAME:-dagster-iceberg-$(whoami | tr -d ' ' | tr '[:upper:]' '[:lower:]')-$SUFFIX}"
    ICEBERG_ROLE_NAME="${ICEBERG_ROLE_NAME:-snowflake-iceberg-role-$SUFFIX}"
    ICEBERG_VOLUME="${ICEBERG_VOLUME_NAME:-DAGSTER_DEMO_VOLUME}"
    ICEBERG_EXTERNAL_ID="$(echo "$ICEBERG_VOLUME" | tr '[:upper:]' '[:lower:]')-external-id"

    # Show planned resources + confirm before any AWS API calls.
    echo
    echo "    AWS account:   $AWS_ACCT (authed as $AWS_IDENT)"
    echo "    AWS region:    $AWS_REGION"
    echo "    S3 bucket:     $ICEBERG_S3_BUCKET"
    echo "    IAM role:      $ICEBERG_ROLE_NAME"
    echo "    SF volume:     $ICEBERG_VOLUME"
    echo "    External ID:   $ICEBERG_EXTERNAL_ID"
    echo
    echo "    To use different names, exit and re-run with --bucket / --iam-role / --volume-name."
    read -r -p "    Proceed with these AWS resources? [Y/n] " ICEBERG_GO
    case "${ICEBERG_GO:-y}" in
      y|Y|yes) ;;
      *) echo "    Iceberg setup skipped. Re-run with --with-iceberg=true to retry."; ICEBERG_VOLUME=""; WITH_ICEBERG=false ;;
    esac

    # All AWS work below is gated on the confirmation above. If the user
    # declined, ICEBERG_VOLUME was cleared and the rest of the block no-ops.
    if [ -n "$ICEBERG_VOLUME" ]; then

    # 6a. S3 bucket
    if aws s3api head-bucket --bucket "$ICEBERG_S3_BUCKET" 2>/dev/null; then
      echo "    ✓ S3 bucket already exists, reusing"
    else
      if [ "$AWS_REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket "$ICEBERG_S3_BUCKET" --region us-east-1 >/dev/null
      else
        aws s3api create-bucket --bucket "$ICEBERG_S3_BUCKET" --region "$AWS_REGION" \
          --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
      fi
      echo "    ✓ Created s3://$ICEBERG_S3_BUCKET/"
    fi

    # 6b. IAM role with placeholder trust + S3 permissions policy
    TRUST_FILE=$(mktemp -t sf_trust.XXXX).json
    POLICY_FILE=$(mktemp -t sf_policy.XXXX).json
    cat > "$TRUST_FILE" <<EOF
{ "Version": "2012-10-17", "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::${AWS_ACCT}:root"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "$ICEBERG_EXTERNAL_ID"}}
}] }
EOF
    cat > "$POLICY_FILE" <<EOF
{ "Version": "2012-10-17", "Statement": [
  {"Effect":"Allow","Action":["s3:GetObject","s3:GetObjectVersion","s3:PutObject","s3:DeleteObject","s3:DeleteObjectVersion"],"Resource":"arn:aws:s3:::${ICEBERG_S3_BUCKET}/*"},
  {"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation"],"Resource":"arn:aws:s3:::${ICEBERG_S3_BUCKET}"}
] }
EOF
    if aws iam get-role --role-name "$ICEBERG_ROLE_NAME" >/dev/null 2>&1; then
      aws iam update-assume-role-policy --role-name "$ICEBERG_ROLE_NAME" \
        --policy-document file://"$TRUST_FILE" >/dev/null
      echo "    ✓ IAM role exists, updated placeholder trust"
    else
      aws iam create-role --role-name "$ICEBERG_ROLE_NAME" \
        --assume-role-policy-document file://"$TRUST_FILE" >/dev/null
      echo "    ✓ Created IAM role $ICEBERG_ROLE_NAME"
    fi
    aws iam put-role-policy --role-name "$ICEBERG_ROLE_NAME" \
      --policy-name iceberg-s3-access --policy-document file://"$POLICY_FILE" >/dev/null
    echo "    ✓ Attached S3 permissions policy"

    ICEBERG_ROLE_ARN="arn:aws:iam::${AWS_ACCT}:role/${ICEBERG_ROLE_NAME}"

    # 6c. Create external volume via Python, extract principal, re-patch trust, verify
    export SF_ICEBERG_VOLUME="$ICEBERG_VOLUME"
    export SF_ICEBERG_BUCKET="$ICEBERG_S3_BUCKET"
    export SF_ICEBERG_ROLE_ARN="$ICEBERG_ROLE_ARN"
    export SF_ICEBERG_EXTERNAL_ID="$ICEBERG_EXTERNAL_ID"
    export SF_DAGSTER_RUNTIME_ROLE="$DAGSTER_RUNTIME_ROLE"
    ICEBERG_OUT=$(mktemp -t sf_iceberg.XXXX).json
    export ICEBERG_OUT

    uv run --quiet --with 'snowflake-connector-python[secure-local-storage]' --no-project python - <<'PYEOF'
import json, os, sys
import snowflake.connector as sc

ck = dict(account=os.environ['SF_ACCOUNT'], user=os.environ['SF_USER'],
          warehouse=os.environ['SF_WAREHOUSE'], role=os.environ.get('SF_ROLE') or None)
auth = os.environ['SF_AUTH_METHOD']
if   auth == 'keypair':      ck.update(authenticator='SNOWFLAKE_JWT', private_key_file=os.environ['SF_KEY_FILE'])
elif auth == 'sso':          ck.update(authenticator='externalbrowser')
elif auth == 'pat':          ck.update(authenticator='PROGRAMMATIC_ACCESS_TOKEN', token=os.environ['SF_PAT'])
elif auth == 'password_mfa': ck.update(authenticator='username_password_mfa',
                                        password=os.environ.get('SF_PASS',''),
                                        passcode=os.environ.get('SF_MFA_PASSCODE') or None,
                                        client_request_mfa_token=True)
else:                        ck['password'] = os.environ.get('SF_PASS','')
if auth == 'keypair' and os.environ.get('SF_KEY_PWD'):
    ck['private_key_file_pwd'] = os.environ['SF_KEY_PWD']

vol      = os.environ['SF_ICEBERG_VOLUME']
bucket   = os.environ['SF_ICEBERG_BUCKET']
role_arn = os.environ['SF_ICEBERG_ROLE_ARN']
ext_id   = os.environ['SF_ICEBERG_EXTERNAL_ID']

conn = sc.connect(**ck); cur = conn.cursor()

# Switch to ACCOUNTADMIN — required to create external volumes.
try:
    cur.execute("USE ROLE ACCOUNTADMIN")
    cur.execute("SELECT CURRENT_ROLE()")
    if cur.fetchone()[0] != "ACCOUNTADMIN":
        print("    ✗ Couldn't switch to ACCOUNTADMIN. Iceberg setup needs it. Skipping.")
        json.dump({"ok": False, "reason": "no_accountadmin"}, open(sys.argv[1] if len(sys.argv)>1 else '/dev/null','w'))
        sys.exit(2)
except Exception as e:
    print(f"    ✗ USE ROLE ACCOUNTADMIN failed: {e}")
    sys.exit(2)

print(f"    ✓ Switched to ACCOUNTADMIN. Creating external volume {vol} ...")
cur.execute(f"""
CREATE OR REPLACE EXTERNAL VOLUME {vol}
  STORAGE_LOCATIONS = ((
    NAME = 'dagster-demo-iceberg'
    STORAGE_PROVIDER = 'S3'
    STORAGE_BASE_URL = 's3://{bucket}/'
    STORAGE_AWS_ROLE_ARN = '{role_arn}'
    STORAGE_AWS_EXTERNAL_ID = '{ext_id}'
  ))
  ALLOW_WRITES = TRUE
""")

# DESC and parse JSON to find STORAGE_AWS_IAM_USER_ARN
cur.execute(f"DESC EXTERNAL VOLUME {vol}")
rows = cur.fetchall()
principal = None
for r in rows:
    # row schema: [parent_property, property, property_type, property_value, property_default]
    if len(r) >= 4 and r[1] and r[1].startswith("STORAGE_LOCATION_"):
        try:
            blob = json.loads(r[3])
            if blob.get("STORAGE_AWS_IAM_USER_ARN"):
                principal = blob["STORAGE_AWS_IAM_USER_ARN"]
                break
        except Exception:
            pass

if not principal:
    print("    ✗ Could not find STORAGE_AWS_IAM_USER_ARN in DESC EXTERNAL VOLUME output.")
    print("      Rows returned:")
    for r in rows: print(f"        {r}")
    sys.exit(3)

print(f"    ✓ Snowflake principal: {principal}")
json.dump({"ok": True, "principal": principal}, open(os.environ['ICEBERG_OUT'], 'w'))
cur.close(); conn.close()
PYEOF
    ICEBERG_PHASE1_RC=$?
    if [ $ICEBERG_PHASE1_RC -ne 0 ]; then
      echo "    ✗ Phase 1 of Iceberg setup failed. Resources in AWS are intact —"
      echo "      re-run with --with-iceberg=true to retry, or --with-iceberg=false to skip."
      ICEBERG_VOLUME=""   # so .env reflects that Iceberg didn't complete
    else
      SF_PRINCIPAL=$(python3 -c "import json;print(json.load(open('$ICEBERG_OUT'))['principal'])")

      # 6d. Patch AWS trust policy with the real Snowflake principal
      cat > "$TRUST_FILE" <<EOF
{ "Version": "2012-10-17", "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "$SF_PRINCIPAL"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "$ICEBERG_EXTERNAL_ID"}}
}] }
EOF
      aws iam update-assume-role-policy --role-name "$ICEBERG_ROLE_NAME" \
        --policy-document file://"$TRUST_FILE" >/dev/null
      echo "    ✓ Updated IAM role trust to allow $SF_PRINCIPAL"

      # 6e. SYSTEM$VERIFY_EXTERNAL_VOLUME with retry — IAM trust-policy
      # updates can take 10-60s to propagate to STS. Retry on AssumeRole
      # errors with increasing waits before giving up.
      echo "    Waiting for IAM propagation + verifying (this can take 30-60s) ..."

      uv run --quiet --with 'snowflake-connector-python[secure-local-storage]' --no-project python - <<'PYEOF'
import json, os, sys, time
import snowflake.connector as sc

ck = dict(account=os.environ['SF_ACCOUNT'], user=os.environ['SF_USER'],
          warehouse=os.environ['SF_WAREHOUSE'], role=os.environ.get('SF_ROLE') or None)
auth = os.environ['SF_AUTH_METHOD']
if   auth == 'keypair':      ck.update(authenticator='SNOWFLAKE_JWT', private_key_file=os.environ['SF_KEY_FILE'])
elif auth == 'sso':          ck.update(authenticator='externalbrowser')
elif auth == 'pat':          ck.update(authenticator='PROGRAMMATIC_ACCESS_TOKEN', token=os.environ['SF_PAT'])
elif auth == 'password_mfa': ck.update(authenticator='username_password_mfa',
                                        password=os.environ.get('SF_PASS',''),
                                        passcode=os.environ.get('SF_MFA_PASSCODE') or None,
                                        client_request_mfa_token=True)
else:                        ck['password'] = os.environ.get('SF_PASS','')
if auth == 'keypair' and os.environ.get('SF_KEY_PWD'):
    ck['private_key_file_pwd'] = os.environ['SF_KEY_PWD']

conn = sc.connect(**ck); cur = conn.cursor()
cur.execute("USE ROLE ACCOUNTADMIN")
vol = os.environ['SF_ICEBERG_VOLUME']

# Try a few times. Snowflake updated their endpoints recently; STS
# propagation seems closer to 30-60s than 5s.
attempts = [10, 15, 25, 30]  # waits (s) before each attempt; total ~80s
j = None
for i, wait in enumerate(attempts, 1):
    time.sleep(wait)
    cur.execute(f"SELECT SYSTEM$VERIFY_EXTERNAL_VOLUME('{vol}')")
    raw = cur.fetchone()[0]
    try:
        j = json.loads(raw)
    except Exception:
        print(f"    ✗ VERIFY returned non-JSON: {raw}")
        sys.exit(4)
    failures = [k for k, v in j.items() if k.endswith("Result") and v not in ("PASSED", "SKIPPED")]
    if j.get("success") is True and not failures:
        print(f"    ✓ VERIFY external volume — read/write/list/delete all PASSED (attempt {i}, ~{sum(attempts[:i])}s in).")
        break
    write_result = j.get("writeResult", "")
    if "AssumeRole" in write_result or "not authorized" in write_result.lower():
        print(f"    ⏳ attempt {i}/{len(attempts)}: IAM not yet propagated, retrying ...")
        continue
    # Non-propagation failure — bail
    break

if j is None or j.get("success") is not True:
    print("    ✗ VERIFY external volume failed after retries:")
    for k in ("readResult","writeResult","listResult","deleteResult","awsRoleArnValidationResult"):
        v = j.get(k, "?") if j else "?"
        m = "✓" if v == "PASSED" else "✗"
        print(f"        {m} {k}: {v}")
    if j:
        print(f"      Full response: {json.dumps(j)}")
    print()
    print("    The AWS resources are intact; IAM propagation can take longer than 80s")
    print("    occasionally. You can re-verify manually any time:")
    print(f"        USE ROLE ACCOUNTADMIN;")
    print(f"        SELECT SYSTEM$VERIFY_EXTERNAL_VOLUME('{vol}');")
    print("    Once that returns success: true, also run:")
    runtime_role = os.environ.get('SF_DAGSTER_RUNTIME_ROLE') or os.environ.get('SF_ROLE') or 'SYSADMIN'
    print(f"        GRANT USAGE ON EXTERNAL VOLUME {vol} TO ROLE {runtime_role};")
    sys.exit(5)

# Grant USAGE to the runtime role so Dagster (via snowflake_iceberg_table) can
# create Iceberg tables against this volume without needing ACCOUNTADMIN.
runtime_role = os.environ.get('SF_DAGSTER_RUNTIME_ROLE') or os.environ.get('SF_ROLE') or 'SYSADMIN'
cur.execute(f"GRANT USAGE ON EXTERNAL VOLUME {vol} TO ROLE {runtime_role}")
print(f"    ✓ GRANTED USAGE on external volume to role {runtime_role}")
cur.close(); conn.close()
PYEOF
      ICEBERG_VERIFY_RC=$?
      if [ $ICEBERG_VERIFY_RC -ne 0 ]; then
        echo "    ✗ Iceberg verification failed. The volume exists but isn't ready."
        echo "      AWS resources retained — re-run later (no cleanup needed)."
        ICEBERG_VOLUME=""
      fi
    fi
    rm -f "$TRUST_FILE" "$POLICY_FILE" "$ICEBERG_OUT"

    fi  # end "if ICEBERG_VOLUME was confirmed"
  fi
else
  echo
  echo ">>> Iceberg setup: AWS CLI not detected (and --with-iceberg=auto). Skipping."
  echo "    Re-run with --with-iceberg=true after 'aws configure' to add Iceberg."
fi

# ─── 6.5 Snowpipe S3 wiring (optional, requires Iceberg block to have run) ──
# Promotes STAGING.LANDING_STAGE from internal → external (S3-backed) and
# re-creates ORDERS_AUTO_INGEST_PIPE against it. Then configures S3 PUT
# notifications on the bucket's `orders/` prefix to publish to the pipe's
# Snowflake-managed SQS queue, so files dropped by Dagster's
# orders_to_s3 asset land in STAGING.ORDERS_INGESTED automatically.
#
# Reuses the Iceberg bucket (subprefix orders/) and the existing IAM role.
# Adds the storage integration's STS principal to the IAM trust policy
# alongside the external volume's principal (same External ID).
SNOWPIPE_INTEGRATION_NAME=""
SNOWPIPE_S3_PREFIX="orders/"
if [ -n "$ICEBERG_VOLUME" ] && [ -n "$ICEBERG_S3_BUCKET" ] && [ -n "$ICEBERG_ROLE_ARN" ]; then
  echo
  echo ">>> Snowpipe S3 setup: wiring LANDING_STAGE + ORDERS_AUTO_INGEST_PIPE to S3 ..."

  SNOWPIPE_INTEGRATION_NAME="DAGSTER_DEMO_S3_INTEGRATION"
  echo "    S3 staging:        s3://${ICEBERG_S3_BUCKET}/${SNOWPIPE_S3_PREFIX}"
  echo "    Storage integration: $SNOWPIPE_INTEGRATION_NAME"

  SNOWPIPE_OUT=$(mktemp -t sf_snowpipe.XXXX).json
  export SNOWPIPE_OUT
  export SF_ICEBERG_VOLUME="$ICEBERG_VOLUME"
  export SF_ICEBERG_BUCKET="$ICEBERG_S3_BUCKET"
  export SF_ICEBERG_EXTERNAL_ID="$ICEBERG_EXTERNAL_ID"
  export SF_SNOWPIPE_INTEGRATION="$SNOWPIPE_INTEGRATION_NAME"
  export SF_SNOWPIPE_PREFIX="$SNOWPIPE_S3_PREFIX"
  export SF_TARGET_DB="$TARGET_DB"

  # Phase 1: CREATE STORAGE INTEGRATION; DESC to find its STS principal.
  uv run --quiet --with 'snowflake-connector-python[secure-local-storage]' --no-project python - <<'PYEOF'
import json, os, sys
import snowflake.connector as sc

ck = dict(account=os.environ['SF_ACCOUNT'], user=os.environ['SF_USER'],
          warehouse=os.environ['SF_WAREHOUSE'], role=os.environ.get('SF_ROLE') or None)
auth = os.environ['SF_AUTH_METHOD']
if   auth == 'keypair':      ck.update(authenticator='SNOWFLAKE_JWT', private_key_file=os.environ['SF_KEY_FILE'])
elif auth == 'sso':          ck.update(authenticator='externalbrowser')
elif auth == 'pat':          ck.update(authenticator='PROGRAMMATIC_ACCESS_TOKEN', token=os.environ['SF_PAT'])
elif auth == 'password_mfa': ck.update(authenticator='username_password_mfa',
                                        password=os.environ.get('SF_PASS',''),
                                        passcode=os.environ.get('SF_MFA_PASSCODE') or None,
                                        client_request_mfa_token=True)
else:                        ck['password'] = os.environ.get('SF_PASS','')
if auth == 'keypair' and os.environ.get('SF_KEY_PWD'):
    ck['private_key_file_pwd'] = os.environ['SF_KEY_PWD']

conn = sc.connect(**ck); cur = conn.cursor()
cur.execute("USE ROLE ACCOUNTADMIN")

integ  = os.environ['SF_SNOWPIPE_INTEGRATION']
bucket = os.environ['SF_ICEBERG_BUCKET']
prefix = os.environ['SF_SNOWPIPE_PREFIX']
role_arn = os.environ['SF_ICEBERG_ROLE_ARN']
ext_id   = os.environ['SF_ICEBERG_EXTERNAL_ID']

cur.execute(f"""
CREATE OR REPLACE STORAGE INTEGRATION {integ}
  TYPE = EXTERNAL_STAGE
  STORAGE_PROVIDER = 'S3'
  ENABLED = TRUE
  STORAGE_AWS_ROLE_ARN = '{role_arn}'
  STORAGE_ALLOWED_LOCATIONS = ('s3://{bucket}/{prefix}')
""")
print(f"    ✓ Created STORAGE INTEGRATION {integ}")

# DESC INTEGRATION → STORAGE_AWS_IAM_USER_ARN (principal Snowflake will
# assume FROM) and STORAGE_AWS_EXTERNAL_ID (the value Snowflake passes as
# sts:ExternalId, auto-generated and NOT settable on CREATE). We need both
# to build a trust policy that AWS will actually honor.
cur.execute(f"DESC INTEGRATION {integ}")
principal = None
sp_external_id = None
for row in cur.fetchall():
    if row[0] == "STORAGE_AWS_IAM_USER_ARN":
        principal = row[2]
    elif row[0] == "STORAGE_AWS_EXTERNAL_ID":
        sp_external_id = row[2]
if not principal or not sp_external_id:
    print(f"    ✗ DESC INTEGRATION missing fields — principal={principal!r} external_id={sp_external_id!r}")
    sys.exit(2)
print(f"    ✓ Storage integration principal: {principal}")
print(f"    ✓ Storage integration external ID: {sp_external_id}")

json.dump({"principal": principal, "external_id": sp_external_id},
          open(os.environ['SNOWPIPE_OUT'], 'w'))
cur.close(); conn.close()
PYEOF
  SNOWPIPE_PHASE1_RC=$?

  if [ $SNOWPIPE_PHASE1_RC -ne 0 ]; then
    echo "    ✗ Snowpipe Phase 1 failed. Skipping S3 wiring; pipe remains decorative."
    SNOWPIPE_INTEGRATION_NAME=""
    rm -f "$SNOWPIPE_OUT"
  else
    SNOWPIPE_PRINCIPAL=$(python3 -c "import json;print(json.load(open('$SNOWPIPE_OUT'))['principal'])")
    SNOWPIPE_EXTERNAL_ID=$(python3 -c "import json;print(json.load(open('$SNOWPIPE_OUT'))['external_id'])")

    # Phase 2: rebuild trust policy with TWO statements — one per principal,
    # each scoped to its own ExternalId. The volume's principal passes the
    # ID we set at volume creation; the integration's principal passes the
    # ID Snowflake auto-generated (read back in Phase 1 via DESC).
    SP_TRUST_FILE=$(mktemp -t sf_sp_trust.XXXX).json
    cat > "$SP_TRUST_FILE" <<EOF
{ "Version": "2012-10-17", "Statement": [
  {
    "Effect": "Allow",
    "Principal": {"AWS": "$SF_PRINCIPAL"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "$ICEBERG_EXTERNAL_ID"}}
  },
  {
    "Effect": "Allow",
    "Principal": {"AWS": "$SNOWPIPE_PRINCIPAL"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "$SNOWPIPE_EXTERNAL_ID"}}
  }
] }
EOF
    aws iam update-assume-role-policy --role-name "$ICEBERG_ROLE_NAME" \
      --policy-document file://"$SP_TRUST_FILE" >/dev/null
    echo "    ✓ Updated IAM trust: Iceberg + Snowpipe principals, each with own External ID"

    # Phase 3: DROP and CREATE LANDING_STAGE as EXTERNAL; recreate auto pipe;
    # DESC PIPE to capture NOTIFICATION_CHANNEL (SQS ARN).
    export SF_SNOWPIPE_PRINCIPAL="$SNOWPIPE_PRINCIPAL"
    echo "    Waiting ~30s for IAM propagation, then promoting stage + pipe ..."

    uv run --quiet --with 'snowflake-connector-python[secure-local-storage]' --no-project python - <<'PYEOF'
import json, os, sys, time
import snowflake.connector as sc

ck = dict(account=os.environ['SF_ACCOUNT'], user=os.environ['SF_USER'],
          warehouse=os.environ['SF_WAREHOUSE'], role=os.environ.get('SF_ROLE') or None)
auth = os.environ['SF_AUTH_METHOD']
if   auth == 'keypair':      ck.update(authenticator='SNOWFLAKE_JWT', private_key_file=os.environ['SF_KEY_FILE'])
elif auth == 'sso':          ck.update(authenticator='externalbrowser')
elif auth == 'pat':          ck.update(authenticator='PROGRAMMATIC_ACCESS_TOKEN', token=os.environ['SF_PAT'])
elif auth == 'password_mfa': ck.update(authenticator='username_password_mfa',
                                        password=os.environ.get('SF_PASS',''),
                                        passcode=os.environ.get('SF_MFA_PASSCODE') or None,
                                        client_request_mfa_token=True)
else:                        ck['password'] = os.environ.get('SF_PASS','')
if auth == 'keypair' and os.environ.get('SF_KEY_PWD'):
    ck['private_key_file_pwd'] = os.environ['SF_KEY_PWD']

conn = sc.connect(**ck); cur = conn.cursor()
cur.execute("USE ROLE ACCOUNTADMIN")

db     = os.environ['SF_TARGET_DB']
integ  = os.environ['SF_SNOWPIPE_INTEGRATION']
bucket = os.environ['SF_ICEBERG_BUCKET']
prefix = os.environ['SF_SNOWPIPE_PREFIX']

cur.execute(f"USE DATABASE {db}")
cur.execute("USE SCHEMA STAGING")

# Wait briefly for IAM propagation, then DROP and CREATE the stage as
# external. If the create fails on AssumeRole, retry a couple of times.
time.sleep(20)
attempts = [0, 15, 20, 30]
created = False
for i, wait in enumerate(attempts, 1):
    if wait:
        time.sleep(wait)
    try:
        cur.execute("DROP STAGE IF EXISTS LANDING_STAGE")
        cur.execute(f"""
CREATE STAGE LANDING_STAGE
  URL = 's3://{bucket}/{prefix}'
  STORAGE_INTEGRATION = {integ}
  FILE_FORMAT = (TYPE = CSV SKIP_HEADER = 1)
  COMMENT = 'EXTERNAL stage on s3://{bucket}/{prefix}. Drives ORDERS_AUTO_INGEST_PIPE via SQS event notifications.'
""")
        created = True
        print(f"    ✓ LANDING_STAGE promoted to EXTERNAL (attempt {i})")
        break
    except Exception as e:
        msg = str(e)
        if "AssumeRole" in msg or "not authorized" in msg.lower():
            print(f"    ⏳ attempt {i}/{len(attempts)}: IAM not yet propagated, retrying ...")
            continue
        print(f"    ✗ LANDING_STAGE create failed: {msg}")
        sys.exit(3)

if not created:
    print("    ✗ LANDING_STAGE create timed out waiting on IAM propagation")
    sys.exit(4)

# Recreate AUTO_INGEST pipe against the external stage. CREATE PIPE
# eagerly lists the stage location (unlike CREATE STAGE which is lazy),
# so it can hit the same IAM-propagation race the stage did. Retry on
# AssumeRole failures with the same backoff.
pipe_created = False
for i, wait in enumerate(attempts, 1):
    if wait:
        time.sleep(wait)
    try:
        cur.execute(f"""
CREATE OR REPLACE PIPE ORDERS_AUTO_INGEST_PIPE
  AUTO_INGEST = TRUE
  COMMENT = 'AUTO_INGEST pipe — fires on S3 PUTs into s3://{bucket}/{prefix} (Snowflake-managed SQS).'
AS
COPY INTO {db}.STAGING.ORDERS_INGESTED
FROM @{db}.STAGING.LANDING_STAGE
FILE_FORMAT = (TYPE = CSV SKIP_HEADER = 1)
ON_ERROR = 'CONTINUE'
""")
        pipe_created = True
        print(f"    ✓ ORDERS_AUTO_INGEST_PIPE rebuilt against external stage (attempt {i})")
        break
    except Exception as e:
        msg = str(e)
        if "AssumeRole" in msg or "not authorized" in msg.lower():
            print(f"    ⏳ pipe attempt {i}/{len(attempts)}: IAM not yet propagated, retrying ...")
            continue
        print(f"    ✗ PIPE create failed: {msg}")
        sys.exit(6)

if not pipe_created:
    print("    ✗ PIPE create timed out waiting on IAM propagation")
    sys.exit(7)

# Per-pipe grants for the runner role (bulk grants don't cover pipes).
runner = os.environ.get('SF_DAGSTER_RUNTIME_ROLE') or os.environ.get('SF_ROLE') or 'SYSADMIN'
try:
    cur.execute(f"GRANT MONITOR, OPERATE ON PIPE {db}.STAGING.ORDERS_AUTO_INGEST_PIPE TO ROLE {runner}")
    cur.execute(f"GRANT USAGE ON STAGE {db}.STAGING.LANDING_STAGE TO ROLE {runner}")
    cur.execute(f"GRANT USAGE ON INTEGRATION {integ} TO ROLE {runner}")
    print(f"    ✓ Granted pipe/stage/integration USAGE to role {runner}")
except Exception as e:
    print(f"    ⚠ pipe/stage grants failed (continuing): {e}")

# DESC PIPE → NOTIFICATION_CHANNEL (SQS ARN). Write to file for bash side.
cur.execute("DESC PIPE ORDERS_AUTO_INGEST_PIPE")
cols = [d[0] for d in cur.description]
row  = cur.fetchone()
info = dict(zip(cols, row))
sqs_arn = info.get("notification_channel") or info.get("NOTIFICATION_CHANNEL")
if not sqs_arn:
    print(f"    ✗ DESC PIPE did not return notification_channel. Columns: {cols}")
    sys.exit(5)
print(f"    ✓ Pipe SQS ARN: {sqs_arn}")

json.dump({"sqs_arn": sqs_arn}, open(os.environ['SNOWPIPE_OUT'], 'w'))
cur.close(); conn.close()
PYEOF
    SNOWPIPE_PHASE2_RC=$?

    if [ $SNOWPIPE_PHASE2_RC -ne 0 ]; then
      echo "    ✗ Snowpipe Phase 2 failed. Stage/pipe may be partially created."
      SNOWPIPE_INTEGRATION_NAME=""
    else
      SNOWPIPE_SQS_ARN=$(python3 -c "import json;print(json.load(open('$SNOWPIPE_OUT'))['sqs_arn'])")

      # Phase 4: configure S3 bucket notification.
      # MERGE with any existing notification configuration on the bucket
      # (Iceberg setup itself doesn't add one, but a user-supplied bucket
      # might already have config we shouldn't blow away).
      SP_NOTIF_FILE=$(mktemp -t sf_sp_notif.XXXX).json
      # AWS returns an EMPTY BODY (not '{}') with zero exit when a bucket
      # has no existing notifications — the '|| echo' fallback alone isn't
      # enough. Normalize an empty/missing file to '{}' for the merge step.
      aws s3api get-bucket-notification-configuration --bucket "$ICEBERG_S3_BUCKET" \
        > "$SP_NOTIF_FILE" 2>/dev/null || true
      if [ ! -s "$SP_NOTIF_FILE" ]; then
        echo '{}' > "$SP_NOTIF_FILE"
      fi

      export SP_NOTIF_FILE SNOWPIPE_SQS_ARN ICEBERG_S3_BUCKET SNOWPIPE_S3_PREFIX
      python3 - <<'PYEOF'
import json, os
path   = os.environ['SP_NOTIF_FILE']
arn    = os.environ['SNOWPIPE_SQS_ARN']
prefix = os.environ['SNOWPIPE_S3_PREFIX']

cfg = json.load(open(path))
# Drop AWS response metadata keys boto returns alongside the config.
cfg.pop('ResponseMetadata', None)

queues = cfg.get('QueueConfigurations', []) or []
# Remove any prior config we owned (same ARN + same prefix), so re-runs
# don't accumulate duplicates.
def _matches(q):
    if q.get('QueueArn') != arn:
        return False
    flt = q.get('Filter', {}).get('Key', {}).get('FilterRules', [])
    return any(r.get('Name','').lower() == 'prefix' and r.get('Value') == prefix for r in flt)
queues = [q for q in queues if not _matches(q)]

queues.append({
    "Id": f"dagster-snowpipe-{prefix.strip('/')}",
    "QueueArn": arn,
    "Events": ["s3:ObjectCreated:*"],
    "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": prefix}]}},
})
cfg['QueueConfigurations'] = queues
json.dump(cfg, open(path, 'w'))
PYEOF

      aws s3api put-bucket-notification-configuration \
        --bucket "$ICEBERG_S3_BUCKET" \
        --notification-configuration file://"$SP_NOTIF_FILE" >/dev/null
      echo "    ✓ Configured S3 PUT notifications on ${SNOWPIPE_S3_PREFIX} → Snowflake SQS"

      rm -f "$SP_NOTIF_FILE"
    fi

    rm -f "$SP_TRUST_FILE" "$SNOWPIPE_OUT"
  fi
else
  echo
  echo ">>> Snowpipe S3 setup: skipped (Iceberg block didn't complete)."
  echo "    LANDING_STAGE remains internal; ORDERS_AUTO_INGEST_PIPE is decorative."
fi

# ─── 7. Write .env and .env.example ───────────────────────────────────────
echo
echo ">>> Writing $ENV_OUT and $ENV_EXAMPLE_OUT ..."

# Decide which role Dagster will use: the scoped runner if it was created,
# otherwise the role the operator picked at the prompt.
FINAL_DAGSTER_ROLE="$DAGSTER_RUNTIME_ROLE"

# Build the real .env (with values; secrets omitted).
{
  echo "# Generated by seed.sh on $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  echo "# Source this file (set -a; . ./.env; set +a) before running bootstrap.sh or dg dev."
  echo "# Secrets (password, PAT, key passphrase) are deliberately NOT written here —"
  echo "# export them yourself, or wire a secret manager."
  echo ""
  echo "# ── Snowflake connection ────────────────────────────────────────"
  echo "SNOWFLAKE_ACCOUNT=$SNOW_ACCOUNT"
  echo "SNOWFLAKE_USER=$SNOW_USER"
  echo "SNOWFLAKE_WAREHOUSE=$SNOW_WAREHOUSE"
  echo "SNOWFLAKE_ROLE=$FINAL_DAGSTER_ROLE"
  if [ "$RUNNER_ROLE_CREATED" = "True" ] || [ "$RUNNER_ROLE_CREATED" = "true" ]; then
    echo "# ↑ Scoped runtime role created by seed.sh. Has only the grants Dagster needs."
    echo "#   Switch to $SNOW_ROLE (the DDL role used for seeding) if you need broader access."
  fi
  echo "SNOWFLAKE_DATABASE=$TARGET_DB"
  echo "SNOWFLAKE_SCHEMA=$SNOW_SCHEMA"
  echo "SNOWFLAKE_AUTH_METHOD=$SNOW_AUTH_METHOD"
  case "$SNOW_AUTH_METHOD" in
    keypair)
      echo "SNOWFLAKE_PRIVATE_KEY_FILE=$SNOW_KEY_FILE"
      [ -n "$SNOW_KEY_PWD" ] && echo "# SNOWFLAKE_PRIVATE_KEY_FILE_PWD intentionally NOT written; export it yourself if needed."
      ;;
    password)     echo "# SNOWFLAKE_PASSWORD intentionally NOT written; export it yourself or use a secret manager." ;;
    password_mfa) echo "# SNOWFLAKE_PASSWORD intentionally NOT written; password+MFA isn't headless-safe." ;;
    pat)          echo "# SNOWFLAKE_PAT intentionally NOT written; export it yourself." ;;
    sso)          echo "# SSO has no static secret; dg dev will pop a browser." ;;
  esac
  if [ "$HEADLESS_OK" != "true" ]; then
    echo ""
    echo "# ⚠ Selected auth method ($SNOW_AUTH_METHOD) cannot drive the Dagster daemon"
    echo "#   headlessly. dg dev on this machine works; production schedules/sensors do not."
    echo "#   Switch to keypair/PAT/password before deploying."
  fi
  echo ""
  echo "# ── Iceberg ─────────────────────────────────────────────────────"
  if [ -n "$ICEBERG_VOLUME" ]; then
    echo "ICEBERG_EXTERNAL_VOLUME=$ICEBERG_VOLUME"
    echo "ICEBERG_S3_BUCKET=$ICEBERG_S3_BUCKET"
    echo "ICEBERG_ROLE_ARN=$ICEBERG_ROLE_ARN"
  else
    echo "# Iceberg not configured by this run."
    echo "# ICEBERG_EXTERNAL_VOLUME="
  fi
  echo ""
  echo "# ── Snowpipe S3 staging (used by dataframe_to_s3 sink) ──────────"
  if [ -n "$SNOWPIPE_INTEGRATION_NAME" ]; then
    echo "S3_STAGING_BUCKET=$ICEBERG_S3_BUCKET"
    echo "S3_STAGING_PREFIX=$SNOWPIPE_S3_PREFIX"
    echo "SNOWPIPE_STORAGE_INTEGRATION=$SNOWPIPE_INTEGRATION_NAME"
  else
    echo "# Snowpipe S3 wiring not configured by this run."
    echo "# S3_STAGING_BUCKET="
    echo "# S3_STAGING_PREFIX=orders/"
  fi
  echo ""
  echo "# ── Demo content metadata (for bootstrap.sh) ────────────────────"
  echo "SEED_HAS_DYNAMIC_TABLE=true       # STAGING.EVENTS_CLEANED_DT depends on RAW.EVENTS"
  echo "SEED_HAS_TASK=true                # STAGING.DAILY_ORDERS_ROLLUP writes to ANALYTICS.DAILY_REVENUE"
  echo "SEED_HAS_AI_TABLE=true            # AI.CUSTOMER_FEEDBACK for cortex demo"
  [ -n "$ICEBERG_VOLUME" ] && echo "SEED_HAS_ICEBERG_VOLUME=true     # backbone can extend into Iceberg sink"
} > "$ENV_OUT"
chmod 600 "$ENV_OUT"
echo "    ✓ Wrote $ENV_OUT (mode 600)"

# Build the .env.example — same shape, with placeholder values. Safe to
# commit to a repo because it contains no real account/user/key/principal.
{
  echo "# Example .env for the Dagster Snowflake demo. SAFE TO COMMIT."
  echo "# Copy to .env and replace the placeholders, OR re-run seed.sh."
  echo ""
  echo "# ── Snowflake connection ────────────────────────────────────────"
  echo "SNOWFLAKE_ACCOUNT=<your-account-locator>          # e.g. xy12345.us-east-1 or org-account"
  echo "SNOWFLAKE_USER=<your-username>"
  echo "SNOWFLAKE_WAREHOUSE=COMPUTE_WH"
  echo "SNOWFLAKE_ROLE=DAGSTER_RUNNER                     # scoped role seed.sh creates"
  echo "SNOWFLAKE_DATABASE=DAGSTER_DEMO"
  echo "SNOWFLAKE_SCHEMA=STAGING"
  echo "SNOWFLAKE_AUTH_METHOD=keypair                     # keypair | pat | password | password_mfa | sso"
  echo ""
  echo "# Pick ONE auth-credentials block depending on SNOWFLAKE_AUTH_METHOD:"
  echo "# (a) keypair — recommended for production/dg dev daemon"
  echo "SNOWFLAKE_PRIVATE_KEY_FILE=~/.ssh/snowflake_rsa_key.p8"
  echo "# SNOWFLAKE_PRIVATE_KEY_FILE_PWD=<passphrase-if-encrypted>"
  echo ""
  echo "# (b) password"
  echo "# SNOWFLAKE_PASSWORD=<password>"
  echo ""
  echo "# (c) PAT"
  echo "# SNOWFLAKE_PAT=<programmatic-access-token>"
  echo ""
  echo "# ── Iceberg (optional) ──────────────────────────────────────────"
  echo "# ICEBERG_EXTERNAL_VOLUME=DAGSTER_DEMO_VOLUME"
  echo "# ICEBERG_S3_BUCKET=<your-bucket>"
  echo "# ICEBERG_ROLE_ARN=arn:aws:iam::<account>:role/<role-name>"
  echo ""
  echo "# ── Snowpipe S3 staging (optional, set by seed.sh when AWS+Iceberg) ─"
  echo "# S3_STAGING_BUCKET=<same-bucket-as-iceberg>"
  echo "# S3_STAGING_PREFIX=orders/"
  echo "# SNOWPIPE_STORAGE_INTEGRATION=DAGSTER_DEMO_S3_INTEGRATION"
  echo ""
  echo "# ── Demo content metadata (set by seed.sh; bootstrap.sh reads) ──"
  echo "SEED_HAS_DYNAMIC_TABLE=true"
  echo "SEED_HAS_TASK=true"
  echo "SEED_HAS_AI_TABLE=true"
  echo "# SEED_HAS_ICEBERG_VOLUME=true"
} > "$ENV_EXAMPLE_OUT"
echo "    ✓ Wrote $ENV_EXAMPLE_OUT (safe to commit)"

# ─── 8. Trailing summary ──────────────────────────────────────────────────
cat <<DONE

═══════════════════════════════════════════════════════════════════════
  Seed complete.
═══════════════════════════════════════════════════════════════════════
Provisioned in $SNOW_ACCOUNT:
  • Database $TARGET_DB
      RAW       — EVENTS (5k rows), ORDERS (10k), CUSTOMERS (500)
      STAGING   — EVENTS_CLEANED_DT (dynamic table off RAW.EVENTS)
                  DAILY_ORDERS_ROLLUP (task → ANALYTICS.DAILY_REVENUE)
      ANALYTICS — DAILY_REVENUE (downstream landing)
      AI        — CUSTOMER_FEEDBACK (for cortex demos)
DONE
if [ "$RUNNER_ROLE_CREATED" = "True" ] || [ "$RUNNER_ROLE_CREATED" = "true" ]; then
  cat <<DONE
  • Role $RUNNER_ROLE_NAME — least-privilege runtime role for Dagster
      USAGE on warehouse + database + all four schemas
      SELECT on RAW/STAGING/AI; SELECT+INSERT+UPDATE+DELETE on ANALYTICS
      OPERATE on STAGING tasks; granted to $SNOW_USER
DONE
else
  cat <<DONE
  • Runtime role: $FINAL_DAGSTER_ROLE  (the role you picked at the prompt)
      Note: not a scoped role — Dagster will have whatever privileges this role has.
      Re-run without --no-dagster-runner to create a scoped DAGSTER_RUNNER instead.
DONE
fi
if [ -n "$ICEBERG_VOLUME" ]; then
  cat <<DONE
  • Iceberg external volume $ICEBERG_VOLUME
      backed by s3://$ICEBERG_S3_BUCKET/
      assumed via $ICEBERG_ROLE_ARN
      verified: read/write/list/delete all PASSED
      USAGE granted to role $SNOW_ROLE
DONE
fi

cat <<DONE

Next: scaffold the Dagster project.

    ./bootstrap.sh                 # reads $ENV_OUT, scaffolds the project
    ./bootstrap.sh --help          # see flags (--with-cortex, --with-dbt, etc.)

To rebuild this environment from scratch:
    ./seed.sh --reset              # drop and recreate $TARGET_DB
DONE

# Cleanup of intermediate state files (preserve .env / .env.example).
rm -f "$PRECHECK_OUT" "$SEED_STATE_FILE" "${SUBST_SQL:-}"
