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
if [ -n "${SNOWFLAKE_PASSWORD:-}" ]; then
  echo "Password: [using \$SNOWFLAKE_PASSWORD from env]"
  SNOW_PASS="$SNOWFLAKE_PASSWORD"
else
  read -r -s -p "Password (hidden): " SNOW_PASS
  echo
fi
prompt_default "Warehouse" SNOW_WAREHOUSE "${SNOWFLAKE_WAREHOUSE:-COMPUTE_WH}"
prompt_default "Role (leave blank for default; SYSADMIN is recommended)" SNOW_ROLE "${SNOWFLAKE_ROLE:-SYSADMIN}"

if [ -z "$SNOW_ACCOUNT" ] || [ -z "$SNOW_USER" ] || [ -z "$SNOW_PASS" ]; then
  echo "  ⚠ account / user / password are required."
  exit 1
fi

# ── Run the SQL ────────────────────────────────────────────────────────
echo
echo ">>> Executing setup against $SNOW_ACCOUNT (warehouse=$SNOW_WAREHOUSE role=$SNOW_ROLE) ..."
echo "    This takes 30–60 seconds. Progress will print as each block completes."
echo

SF_ACCOUNT="$SNOW_ACCOUNT" SF_USER="$SNOW_USER" SF_PASS="$SNOW_PASS" \
  SF_WAREHOUSE="$SNOW_WAREHOUSE" SF_ROLE="$SNOW_ROLE" \
  SF_SQL_FILE="$SQL_FILE" \
  uv run --quiet --with 'snowflake-connector-python' --no-project python - <<'PYEOF'
import os, re, sys, time
import snowflake.connector as sc

try:
    conn = sc.connect(
        account=os.environ['SF_ACCOUNT'],
        user=os.environ['SF_USER'],
        password=os.environ['SF_PASS'],
        warehouse=os.environ['SF_WAREHOUSE'],
        role=os.environ.get('SF_ROLE') or None,
    )
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
  • DAGSTER_DEMO.RAW       — ORDERS (10000), CUSTOMERS (1000), PRODUCTS (200), EVENTS (50000)
  • DAGSTER_DEMO.STAGING   — 6 TASKS, 4 DYNAMIC TABLES, 3 STORED PROCEDURES,
                             2 STREAMS, 1 MATERIALIZED VIEW, 2 STAGES,
                             1 SNOWPIPE, 1 ALERT
  • DAGSTER_DEMO.ANALYTICS — empty (your Dagster pipeline writes here)
  • DAGSTER_DEMO.AI        — CUSTOMER_FEEDBACK (for Cortex demos)

Next: run the workspace demo to discover everything and pull it into Dagster:

    curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_workspace_demo.sh -o setup_snowflake_workspace_demo.sh
    chmod +x setup_snowflake_workspace_demo.sh
    ./setup_snowflake_workspace_demo.sh

When the workspace demo asks for database/schema, use:
    database = DAGSTER_DEMO
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

exit $RC
