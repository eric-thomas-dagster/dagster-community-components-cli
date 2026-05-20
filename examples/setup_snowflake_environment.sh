#!/usr/bin/env bash
# Run setup_snowflake_environment.sql against your Snowflake account.
#
# Creates DAGSTER_DEMO.{RAW,STAGING,ANALYTICS,AI} with seeded base tables
# and a wide variety of orchestratable entities — tasks, dynamic tables,
# stored procs (SQL + Snowpark Python), streams, materialized view,
# internal stages, a snowpipe, and an alert — so the
# setup_snowflake_workspace_demo.sh script has plenty to discover.
#
# IDEMPOTENT — re-running drops and recreates. Pair with
# teardown_snowflake_environment.sql to clean up entirely.

set -eo pipefail

if [ ! -t 0 ]; then
  cat <<'NONINTERACTIVE_GUARD'
════════════════════════════════════════════════════════════════════
  This script is interactive — it can't run via `curl | bash`.

  Download first, then run from a terminal:

    curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_environment.sh -o setup_snowflake_environment.sh
    chmod +x setup_snowflake_environment.sh
    ./setup_snowflake_environment.sh
════════════════════════════════════════════════════════════════════
NONINTERACTIVE_GUARD
  cat >/dev/null 2>&1 || true
  exit 1
fi

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

# Locate the SQL file: prefer same dir as this script, fall back to the
# raw GitHub URL.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SQL_FILE="$SCRIPT_DIR/setup_snowflake_environment.sql"
if [ ! -f "$SQL_FILE" ]; then
  SQL_FILE="$(mktemp -t sf_env.XXXXXX).sql"
  echo ">>> Fetching setup_snowflake_environment.sql ..."
  curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_environment.sql -o "$SQL_FILE"
fi

echo "═══════════════════════════════════════════════════════════════════════"
echo "  Snowflake environment setup"
echo "═══════════════════════════════════════════════════════════════════════"
echo "This will CREATE a database named DAGSTER_DEMO in your Snowflake account"
echo "and populate it with seeded tables + tasks + dynamic tables + stored"
echo "procedures + streams + MV + stages + snowpipe + alert."
echo
echo "Idempotent — safe to re-run. ~10k orders + 1k customers + 200 products"
echo "+ 50k events get seeded. Costs ~a few credits on an XS warehouse."
echo
read -r -p "Continue? [Y/n] " GO
case "${GO:-y}" in y|Y|yes) ;; *) echo "Aborted."; exit 0 ;; esac

# ── Credentials prompt ─────────────────────────────────────────────────
echo
echo "─────────────────────────────────────────────────────────────────────"
echo "  Snowflake connection"
echo "─────────────────────────────────────────────────────────────────────"

prompt_default() {
  local prompt="$1" var="$2" def="$3"
  if [ -n "$def" ]; then
    read -r -p "$prompt [$def]: " val
    eval "$var=\"\${val:-$def}\""
  else
    read -r -p "$prompt: " val
    eval "$var=\"$val\""
  fi
}

prompt_default "Snowflake account (e.g. xy12345.us-east-1 or org-account)" SNOW_ACCOUNT "${SNOWFLAKE_ACCOUNT:-}"
prompt_default "Username" SNOW_USER "${SNOWFLAKE_USER:-}"

# Auth method — most enterprise Snowflake accounts disable password auth.
# Keypair is recommended for headless / production (no browser needed).
echo
echo "Authentication method:"
echo "  [1] Keypair (RSA private key file) — headless, recommended"
echo "  [2] SSO (externalbrowser) — pops a browser for auth"
echo "  [3] Password (if your account still allows it)"
echo "  [4] PAT (Programmatic Access Token) — works when keypair registration is blocked"
read -r -p "Choice [1]: " AUTH_CHOICE
AUTH_CHOICE="${AUTH_CHOICE:-1}"
SNOW_AUTH_METHOD=""
SNOW_PASS=""
SNOW_KEY_FILE=""
SNOW_KEY_PWD=""
SNOW_PAT=""
case "$AUTH_CHOICE" in
  1|keypair)
    SNOW_AUTH_METHOD="keypair"
    prompt_default "Path to RSA private key file (PEM)" SNOW_KEY_FILE \
      "${SNOWFLAKE_PRIVATE_KEY_FILE:-$HOME/.ssh/snowflake_rsa_key.p8}"
    if [ ! -f "$SNOW_KEY_FILE" ]; then
      echo "  ⚠ Key file not found: $SNOW_KEY_FILE"
      exit 1
    fi
    if [ -n "${SNOWFLAKE_PRIVATE_KEY_FILE_PWD:-}" ]; then
      echo "Key passphrase: [using \$SNOWFLAKE_PRIVATE_KEY_FILE_PWD from env]"
      SNOW_KEY_PWD="$SNOWFLAKE_PRIVATE_KEY_FILE_PWD"
    else
      read -r -s -p "Key passphrase (hidden; blank if key is unencrypted): " SNOW_KEY_PWD
      echo
    fi
    ;;
  2|sso)
    SNOW_AUTH_METHOD="sso"
    echo "  (SSO uses externalbrowser — a browser tab will open for auth)"
    ;;
  3|password)
    SNOW_AUTH_METHOD="password"
    if [ -n "${SNOWFLAKE_PASSWORD:-}" ]; then
      echo "Password: [using \$SNOWFLAKE_PASSWORD from env]"
      SNOW_PASS="$SNOWFLAKE_PASSWORD"
    else
      read -r -s -p "Password (hidden): " SNOW_PASS
      echo
    fi
    ;;
  4|pat|PAT)
    SNOW_AUTH_METHOD="pat"
    if [ -n "${SNOWFLAKE_PAT:-}" ]; then
      echo "PAT: [using \$SNOWFLAKE_PAT from env]"
      SNOW_PAT="$SNOWFLAKE_PAT"
    else
      read -r -s -p "Programmatic Access Token (hidden): " SNOW_PAT
      echo
    fi
    if [ -z "$SNOW_PAT" ]; then
      echo "  ⚠ PAT is required when auth method is 'pat'."
      exit 1
    fi
    ;;
  *)
    echo "  ⚠ Invalid choice — pick 1, 2, 3, or 4."
    exit 1
    ;;
esac

prompt_default "Warehouse" SNOW_WAREHOUSE "${SNOWFLAKE_WAREHOUSE:-COMPUTE_WH}"
prompt_default "Role (leave blank for default; SYSADMIN is recommended)" SNOW_ROLE "${SNOWFLAKE_ROLE:-SYSADMIN}"

if [ -z "$SNOW_ACCOUNT" ] || [ -z "$SNOW_USER" ]; then
  echo "  ⚠ account / user are required."
  exit 1
fi
if [ "$SNOW_AUTH_METHOD" = "password" ] && [ -z "$SNOW_PASS" ]; then
  echo "  ⚠ password is required when auth method is 'password'."
  exit 1
fi

# Configurable target database — default DAGSTER_DEMO but lets users avoid
# a collision with anything already in the account. We sed-substitute this
# into the SQL text before execution so EVERY reference matches.
prompt_default "Target database name (will be created if absent)" SNOW_TARGET_DB "${SNOWFLAKE_TARGET_DATABASE:-DAGSTER_DEMO}"

# ── Pre-flight: role / warehouse / database / collision check ──────────
# Three safety passes before any DDL runs:
#   1. role + warehouse exist and the user can USE them
#   2. target database exists? If so, query every seed object name against
#      INFORMATION_SCHEMA to inventory exactly what would be overwritten
#   3. ask the user what to do based on what we found
echo
echo ">>> Pre-flight checks (role / warehouse / database / object collisions) ..."

PRECHECK_OUT="$(mktemp -t sf_preflight.XXXXXX).json"
SF_ACCOUNT="$SNOW_ACCOUNT" SF_USER="$SNOW_USER" SF_PASS="$SNOW_PASS" \
  SF_WAREHOUSE="$SNOW_WAREHOUSE" SF_ROLE="$SNOW_ROLE" \
  SF_TARGET_DB="$SNOW_TARGET_DB" \
  SF_AUTH_METHOD="$SNOW_AUTH_METHOD" \
  SF_KEY_FILE="$SNOW_KEY_FILE" SF_KEY_PWD="$SNOW_KEY_PWD" \
  SF_PAT="$SNOW_PAT" \
  PRECHECK_OUT="$PRECHECK_OUT" \
  uv run --quiet --with 'snowflake-connector-python' --no-project python - <<'PYEOF'
import json, os, sys
import snowflake.connector as sc

result = {"role_ok": False, "warehouse_ok": False, "db_exists": False, "collisions": {}, "errors": []}

# Build connect kwargs per auth method.
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
else:  # password
    ck['password'] = os.environ.get('SF_PASS', '')

try:
    conn = sc.connect(**ck)
except Exception as e:
    result["errors"].append(f"connect: {e}")
    json.dump(result, open(os.environ['PRECHECK_OUT'], 'w'))
    sys.exit(0)

cur = conn.cursor()

# 1. Role + warehouse — already enforced by `connect()` succeeding with
#    those values. SHOW confirms they're visible to the user.
try:
    cur.execute(f"SHOW ROLES LIKE '{os.environ['SF_ROLE']}'")
    result["role_ok"] = len(cur.fetchall()) > 0
except Exception as e:
    result["errors"].append(f"role check: {e}")

try:
    cur.execute(f"SHOW WAREHOUSES LIKE '{os.environ['SF_WAREHOUSE']}'")
    result["warehouse_ok"] = len(cur.fetchall()) > 0
except Exception as e:
    result["errors"].append(f"warehouse check: {e}")

# 2. Does the target database exist?
db = os.environ['SF_TARGET_DB']
try:
    cur.execute(f"SHOW DATABASES LIKE '{db}'")
    result["db_exists"] = len(cur.fetchall()) > 0
except Exception as e:
    result["errors"].append(f"database check: {e}")

# 3. If it exists, inventory collisions with the names this script will
#    CREATE OR REPLACE. We declare them explicitly here so we always
#    know exactly what's about to land.
SEED_OBJECTS = {
    "RAW.tables":           ["ORDERS", "CUSTOMERS", "PRODUCTS", "EVENTS"],
    "AI.tables":            ["CUSTOMER_FEEDBACK"],
    "STAGING.tables":       ["ORDERS_INGESTED"],
    "ANALYTICS.tables":     ["DAILY_REVENUE", "ALERT_LOG"],
    "STAGING.tasks":        ["DAILY_ORDERS_ROLLUP", "HOURLY_CUSTOMER_METRICS",
                              "WEEKLY_CHURN_SCORE", "MONTHLY_REVENUE_REPORT",
                              "PARENT_ETL_TASK", "CHILD_ETL_TASK"],
    "STAGING.dynamic_tables": ["PAID_ORDERS_DT", "CUSTOMER_360_DT",
                                "TOP_PRODUCTS_DT", "HOURLY_ACTIVITY_DT"],
    "STAGING.procedures":   ["SP_RECOMPUTE_TIERS", "SP_PURGE_OLD_EVENTS",
                              "SP_SNOWPARK_TOP_N"],
    "STAGING.streams":      ["ORDERS_STREAM", "CUSTOMERS_STREAM"],
    "STAGING.materialized_views": ["CUSTOMER_LIFETIME_VALUE_MV"],
    "STAGING.stages":       ["INTERNAL_STAGE", "LANDING_STAGE"],
    "STAGING.pipes":        ["ORDERS_PIPE"],
    "STAGING.alerts":       ["HIGH_REVENUE_DAY_ALERT"],
}

if result["db_exists"]:
    queries = {
        "tables":             "SELECT table_schema, table_name FROM {db}.INFORMATION_SCHEMA.TABLES WHERE table_schema IN ('RAW','STAGING','ANALYTICS','AI') AND table_type IN ('BASE TABLE')",
        "tasks":              "SELECT schema_name, name FROM {db}.INFORMATION_SCHEMA.TASKS WHERE schema_name = 'STAGING'",
        "dynamic_tables":     "SELECT schema_name, name FROM {db}.INFORMATION_SCHEMA.DYNAMIC_TABLES WHERE schema_name = 'STAGING'",
        "procedures":         "SELECT procedure_schema, procedure_name FROM {db}.INFORMATION_SCHEMA.PROCEDURES WHERE procedure_schema = 'STAGING'",
        "materialized_views": "SELECT table_schema, table_name FROM {db}.INFORMATION_SCHEMA.VIEWS WHERE table_schema = 'STAGING'",
    }
    existing = {}
    for kind, q in queries.items():
        try:
            cur.execute(q.format(db=db))
            existing[kind] = {f"{r[0]}.{r[1]}" for r in cur.fetchall()}
        except Exception as e:
            existing[kind] = set()  # missing schema or permission — treat as no existing
    # SHOW for kinds without an INFORMATION_SCHEMA view (streams/pipes/stages/alerts)
    for kind, sql_cmd in [("streams", "SHOW STREAMS"), ("pipes", "SHOW PIPES"),
                            ("stages", "SHOW STAGES"), ("alerts", "SHOW ALERTS")]:
        try:
            cur.execute(f"{sql_cmd} IN SCHEMA {db}.STAGING")
            # SHOW result column layout varies; name is usually col[1]
            existing[kind] = {f"STAGING.{r[1]}" for r in cur.fetchall()}
        except Exception:
            existing[kind] = set()

    # Compare against SEED_OBJECTS
    for slot, names in SEED_OBJECTS.items():
        schema, kind = slot.split(".", 1)
        live = existing.get(kind, set())
        hits = sorted([n for n in names if f"{schema}.{n}" in live])
        if hits:
            result["collisions"][slot] = hits

cur.close(); conn.close()
json.dump(result, open(os.environ['PRECHECK_OUT'], 'w'))
PYEOF
PRECHECK_RC=$?

if [ $PRECHECK_RC -ne 0 ] || [ ! -s "$PRECHECK_OUT" ]; then
  echo "  ⚠ Pre-flight check failed to run. Aborting."
  rm -f "$PRECHECK_OUT"
  exit 1
fi

# Parse pre-flight result and decide what to do.
PRECHECK_VERDICT=$(python3 - "$PRECHECK_OUT" <<'PYEOF'
import json, sys
r = json.load(open(sys.argv[1]))
if r["errors"]:
    print("ERROR")
    for e in r["errors"]: print(f"  {e}")
    sys.exit(0)
if not r["role_ok"]:
    print("ROLE_MISSING")
    sys.exit(0)
if not r["warehouse_ok"]:
    print("WAREHOUSE_MISSING")
    sys.exit(0)
if not r["db_exists"]:
    print("ALL_CLEAR")
    sys.exit(0)
# DB exists — count collisions
total = sum(len(v) for v in r["collisions"].values())
if total == 0:
    print("DB_EXISTS_NO_COLLISIONS")
    sys.exit(0)
print(f"COLLISIONS:{total}")
for slot, hits in r["collisions"].items():
    print(f"  {slot}: {', '.join(hits)}")
PYEOF
)
echo "$PRECHECK_VERDICT" | head -1
case "$(echo "$PRECHECK_VERDICT" | head -1)" in
  ERROR*)
    echo "$PRECHECK_VERDICT" | tail -n +2
    rm -f "$PRECHECK_OUT"; exit 1 ;;
  ROLE_MISSING)
    echo "  ⚠ Role '$SNOW_ROLE' is not visible to the user. Pick a role you have access to and re-run."
    rm -f "$PRECHECK_OUT"; exit 1 ;;
  WAREHOUSE_MISSING)
    echo "  ⚠ Warehouse '$SNOW_WAREHOUSE' is not visible to the user (or doesn't exist)."
    echo "    Common defaults: COMPUTE_WH, ANALYTICS_WH. Pick one you have access to."
    rm -f "$PRECHECK_OUT"; exit 1 ;;
  ALL_CLEAR)
    echo "  ✓ Role + warehouse OK. Target database '$SNOW_TARGET_DB' does NOT exist — will create fresh." ;;
  DB_EXISTS_NO_COLLISIONS)
    echo "  ✓ Role + warehouse OK. Target database '$SNOW_TARGET_DB' exists but has NO objects that share names with the seed. Safe to overlay." ;;
  COLLISIONS:*)
    N=$(echo "$PRECHECK_VERDICT" | head -1 | cut -d: -f2)
    echo "  ⚠ Target database '$SNOW_TARGET_DB' exists AND contains $N object(s) that share names with the seed:"
    echo "$PRECHECK_VERDICT" | tail -n +2
    echo
    echo "    These objects WILL BE OVERWRITTEN by CREATE OR REPLACE if you continue."
    echo
    while :; do
      read -r -p "    [r]euse and overwrite / [d]rop database and recreate / [c]hange database name / [q]uit: " CHOICE
      case "${CHOICE:-q}" in
        r|R|reuse)
          echo "  → Proceeding with overwrite. Existing objects with conflicting names will be replaced."
          break ;;
        d|D|drop)
          echo "  → Will DROP DATABASE $SNOW_TARGET_DB first, then recreate from scratch."
          # Inject a DROP DATABASE at the head of our SQL by prepending it.
          DROP_FIRST=true
          break ;;
        c|C|change)
          prompt_default "New target database name" SNOW_TARGET_DB "DAGSTER_DEMO_$(date +%s)"
          echo "  → Will use '$SNOW_TARGET_DB'. Re-running pre-flight ..."
          # Re-exec ourselves — easiest way to re-run the pre-flight cleanly.
          rm -f "$PRECHECK_OUT"
          SNOWFLAKE_TARGET_DATABASE="$SNOW_TARGET_DB" \
            SNOWFLAKE_ACCOUNT="$SNOW_ACCOUNT" SNOWFLAKE_USER="$SNOW_USER" \
            SNOWFLAKE_PASSWORD="$SNOW_PASS" SNOWFLAKE_WAREHOUSE="$SNOW_WAREHOUSE" \
            SNOWFLAKE_ROLE="$SNOW_ROLE" \
            exec "$0" "$@"
          ;;
        q|Q|quit) echo "  Aborted."; rm -f "$PRECHECK_OUT"; exit 0 ;;
        *) echo "    Pick r, d, c, or q." ;;
      esac
    done ;;
esac
rm -f "$PRECHECK_OUT"

# Read the SQL, substitute the database name, write to a temp file for execution.
SUBST_SQL="$(mktemp -t sf_seed_subst.XXXXXX).sql"
if [ "${DROP_FIRST:-false}" = "true" ]; then
  printf "USE ROLE %s;\nDROP DATABASE IF EXISTS %s;\n" "$SNOW_ROLE" "$SNOW_TARGET_DB" > "$SUBST_SQL"
fi
# Substitute every literal DAGSTER_DEMO occurrence with the chosen name.
# Use python for safe string replace (sed would need escaping for special chars).
python3 -c "
import sys
src = open(sys.argv[1]).read()
out = src.replace('DAGSTER_DEMO', sys.argv[2])
open(sys.argv[3], 'a').write(out)
" "$SQL_FILE" "$SNOW_TARGET_DB" "$SUBST_SQL"
SQL_FILE="$SUBST_SQL"

# ── Run the SQL ────────────────────────────────────────────────────────
echo
echo ">>> Executing setup against $SNOW_ACCOUNT (warehouse=$SNOW_WAREHOUSE role=$SNOW_ROLE) ..."
echo "    This takes 30–60 seconds. Progress will print as each block completes."
echo

SF_ACCOUNT="$SNOW_ACCOUNT" SF_USER="$SNOW_USER" SF_PASS="$SNOW_PASS" \
  SF_WAREHOUSE="$SNOW_WAREHOUSE" SF_ROLE="$SNOW_ROLE" \
  SF_AUTH_METHOD="$SNOW_AUTH_METHOD" \
  SF_KEY_FILE="$SNOW_KEY_FILE" SF_KEY_PWD="$SNOW_KEY_PWD" \
  SF_PAT="$SNOW_PAT" \
  SF_SQL_FILE="$SQL_FILE" \
  uv run --quiet --with 'snowflake-connector-python' --no-project python - <<'PYEOF'
import os, re, sys, time
import snowflake.connector as sc

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
else:
    ck['password'] = os.environ.get('SF_PASS', '')

try:
    conn = sc.connect(**ck)
except Exception as e:
    print(f"  ✗ Connection failed: {e}", file=sys.stderr)
    sys.exit(1)

cur = conn.cursor()
# Confirm connection
cur.execute("SELECT CURRENT_VERSION()")
print(f"  ✓ Connected. Snowflake version: {cur.fetchone()[0]}")

# Read the SQL file and split on `;` at end of line, but respect `$$` blocks
# (Snowpark Python procs use $$ ... $$ as the body delimiter and contain
# embedded semicolons).
with open(os.environ['SF_SQL_FILE']) as f:
    text = f.read()

# Tokenize on statement boundaries.
statements = []
buf = []
in_dollar = False
for line in text.splitlines():
    # Strip line-comments at the start of a line; don't strip in-line ones
    # because the SQL has trailing `-- ...` we want to preserve as-is.
    stripped = line.strip()
    if not in_dollar and (stripped.startswith('--') or not stripped):
        # Skip comment-only / blank lines outside dollar blocks.
        continue
    buf.append(line)
    # Toggle dollar-block flag on each `$$` occurrence.
    dollars = line.count('$$')
    if dollars % 2 == 1:
        in_dollar = not in_dollar
    # Statement ends at a `;` outside of any dollar block.
    if not in_dollar and stripped.endswith(';'):
        statements.append('\n'.join(buf))
        buf = []
if buf:
    statements.append('\n'.join(buf))

print(f"  > Running {len(statements)} statements ...")

failures = 0
for i, stmt in enumerate(statements, 1):
    s = stmt.strip()
    if not s or s == ';':
        continue
    # Extract a short label from the first non-comment line for progress.
    label_match = re.search(r'^\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|TASK|DYNAMIC\s+TABLE|STREAM|STAGE|PROCEDURE|MATERIALIZED\s+VIEW|PIPE|ALERT|DATABASE|SCHEMA)\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+))', s, re.IGNORECASE | re.MULTILINE)
    if label_match:
        label = label_match.group(0).strip().replace('\n', ' ')[:80]
    else:
        label = s.split('\n')[0][:80]
    try:
        t0 = time.time()
        cur.execute(s)
        elapsed = time.time() - t0
        # Consume any returned rows so cursor is ready for the next stmt.
        try:
            rows = cur.fetchall()
        except Exception:
            rows = []
        marker = "✓" if elapsed < 5 else "✓✓"
        print(f"    [{i:3}/{len(statements)}] {marker} {label}  ({elapsed:.1f}s)")
        if rows and isinstance(rows[0], tuple) and isinstance(rows[0][0], str) and rows[0][0].startswith('Setup complete'):
            print(f"          → {rows[0][0]}")
    except Exception as e:
        failures += 1
        msg = str(e).split('\n')[0][:120]
        print(f"    [{i:3}/{len(statements)}] ✗ {label}  → {msg}")

cur.close(); conn.close()
print()
if failures == 0:
    print("  ✓ All statements succeeded.")
else:
    print(f"  ⚠ {failures} statement(s) failed — see above. Most failures are due to")
    print("    missing privileges on the role you used. SYSADMIN is recommended.")
    sys.exit(1)
PYEOF
RC=$?

# ── Trailing guide ─────────────────────────────────────────────────────
cat <<MSG

═══════════════════════════════════════════════════════════════════════
  Done.
═══════════════════════════════════════════════════════════════════════
Created in $SNOW_ACCOUNT:
  • $SNOW_TARGET_DB.RAW       — ORDERS (10000), CUSTOMERS (1000), PRODUCTS (200), EVENTS (50000)
  • $SNOW_TARGET_DB.STAGING   — 6 TASKS, 4 DYNAMIC TABLES, 3 STORED PROCEDURES,
                             2 STREAMS, 1 MATERIALIZED VIEW, 2 STAGES,
                             1 SNOWPIPE, 1 ALERT
  • $SNOW_TARGET_DB.ANALYTICS — empty (your Dagster pipeline writes here)
  • $SNOW_TARGET_DB.AI        — CUSTOMER_FEEDBACK (for Cortex demos)

Next: run the workspace demo to discover everything and pull it into Dagster:

    curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_workspace_demo.sh -o setup_snowflake_workspace_demo.sh
    chmod +x setup_snowflake_workspace_demo.sh
    ./setup_snowflake_workspace_demo.sh

When the workspace demo asks for database/schema, use:
    database = $SNOW_TARGET_DB
    schema   = STAGING

The discovery output should look like:
    tasks                  6  daily_orders_rollup, hourly_customer_metrics, …
    dynamic_tables         4  paid_orders_dt, customer_360_dt, …
    stored_procedures      3  SP_RECOMPUTE_TIERS, SP_PURGE_OLD_EVENTS, SP_SNOWPARK_TOP_N
    streams                2  orders_stream, customers_stream
    snowpipes              1  orders_pipe
    stages                 2  internal_stage, landing_stage
    materialized_views     1  customer_lifetime_value_mv
    alerts                 1  high_revenue_day_alert
    openflow_flows         0  — this seed script CAN'T create them (see note below)

A note on OpenFlow:
    Dagster CAN orchestrate OpenFlow flows (set import_openflow_flows: true on
    the workspace component). This seed script CAN'T generate them — there's
    no CREATE FLOW DDL, no terraform resource for flow definitions, and the
    BYOC runtime itself is a non-trivial EKS deployment. If you want OpenFlow
    in a live demo, pre-build one flow in the OpenFlow UI of a demo account
    ahead of time and the workspace component picks it up automatically.

If you also picked the multi-step warehouse_pipeline add-on in the
workspace demo, point it at:
    Orders-like table:   RAW.ORDERS
    Customers-like table: RAW.CUSTOMERS
    Output schema:        ANALYTICS

If you picked Cortex, the AI.CUSTOMER_FEEDBACK table is a great input.

To clean everything up later:
    curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/teardown_snowflake_environment.sql -o teardown_snowflake_environment.sql
    # Then paste teardown_snowflake_environment.sql into a Snowflake worksheet, OR:
    # snowsql -a $SNOWFLAKE_ACCOUNT -u $SNOWFLAKE_USER -f teardown_snowflake_environment.sql
MSG

# Clean up the substituted SQL temp file (the original SQL_FILE was reassigned
# to point at it in the pre-flight block).
case "$SQL_FILE" in /tmp/*|/var/folders/*) rm -f "$SQL_FILE" ;; esac

exit $RC
