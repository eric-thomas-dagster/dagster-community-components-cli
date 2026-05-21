#!/usr/bin/env bash
# Interactive setup: bring an existing Snowflake workspace's entities into a
# Dagster project — using the community `snowflake_workspace` component.
# Same prompt-driven flow as `setup_databricks_workspace_demo.sh` but for the
# Snowflake side of the catalog.
#
# What it does:
#   1. Prompts for project name (collision-checked)
#   2. Prompts for Snowflake creds (account/user/password/warehouse/database/role)
#      — supports literal values OR existing env vars. Verifies the connection
#      against `SELECT CURRENT_VERSION()` before going further.
#   3. Discovers what's importable in the chosen database+schema: counts of
#      TASKS, DYNAMIC TABLES, STORED PROCEDURES, STREAMS, PIPES, STAGES,
#      MATERIALIZED VIEWS, EXTERNAL TABLES, ALERTS.
#   4. User picks WHICH ENTITY TYPES to import (y/N per type — sensible
#      defaults: tasks + dynamic_tables on).
#   5. Optional: per-entity-name pattern filter ("HOURLY_*", "FINANCE_*", etc.)
#   6. Optional: declare cross-entity deps between imported entities (uses
#      `assets_by_name` — same shape as DatabricksWorkspaceComponent's
#      `assets_by_task_key`).
#   7. Optional: add a multi-step `warehouse_pipeline` demonstrating SQL
#      pushdown ON TOP of imported tables (joins + commission + multi-sink).
#   8. Optional: add a `snowflake_cortex_asset` (LLM call through Snowflake
#      Cortex — completion / summarize / sentiment).
#   9. Scaffolds project, writes all defs.yaml, prints next-step guide.
#
# AUTH: defaults to user/password (universal). For PAT / SSO / key-pair, edit
# the generated workspace defs.yaml or .env.demo — every connection field
# supports an `<field>_env_var` form too.
#
# COST: $0 script-side. Materialized tasks/dynamic-tables/pipeline runs cost
# whatever they cost in your Snowflake account.

set -eo pipefail

# Refuse to run via curl|bash — we prompt for several values.
if [ ! -t 0 ]; then
  cat <<'NONINTERACTIVE_GUARD'
════════════════════════════════════════════════════════════════════
  This is an INTERACTIVE script — it can't be run via `curl | bash`.

  Download first, then run from a terminal:

    curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_snowflake_workspace_demo.sh -o setup_snowflake_workspace_demo.sh
    chmod +x setup_snowflake_workspace_demo.sh
    ./setup_snowflake_workspace_demo.sh

  Tip: export SNOWFLAKE_ACCOUNT / SNOWFLAKE_USER / SNOWFLAKE_PASSWORD /
  SNOWFLAKE_WAREHOUSE / SNOWFLAKE_DATABASE / SNOWFLAKE_ROLE in your
  shell first and the script picks them up as defaults.
════════════════════════════════════════════════════════════════════
NONINTERACTIVE_GUARD
  cat >/dev/null 2>&1 || true
  exit 1
fi

PARTIAL_PROJECT_PATH=""
cleanup_on_interrupt() {
  if [ -n "$PARTIAL_PROJECT_PATH" ] && [ -d "$PARTIAL_PROJECT_PATH" ]; then
    echo
    echo ">>> Cleaning up half-built project at $PARTIAL_PROJECT_PATH ..."
    rm -rf "$PARTIAL_PROJECT_PATH" 2>/dev/null || true
  fi
  exit 130
}
trap cleanup_on_interrupt INT TERM

# ── 0. "Give me everything" mode ────────────────────────────────────────
# Set WANT_EVERYTHING=true to auto-y every optional add-on AND skip the
# cross-entity dep prompt (which is the easiest spot to mistype and stall).
# Individual WANT_* env vars still override — useful for "everything EXCEPT
# the dbt one" style runs.
if [ "${WANT_EVERYTHING:-}" = "true" ] || [ "${WANT_EVERYTHING:-}" = "1" ] || [ "${WANT_EVERYTHING:-}" = "y" ]; then
  echo "═════════════════════════════════════════════════════════════════════"
  echo "  WANT_EVERYTHING=true — auto-enabling every optional add-on"
  echo "═════════════════════════════════════════════════════════════════════"
  : "${WANT_DEPS:=n}"            # skip the cross-entity dep prompt
  : "${WANT_PIPELINE:=y}"        # multi-step warehouse_pipeline
  : "${WANT_AUTOCOND:=y}"        # AutomationCondition.eager() on the pipeline
  : "${WANT_CORTEX:=y}"          # snowflake_cortex_asset
  : "${WANT_OBSERVER:=y}"        # snowflake_table_observation_sensor
  : "${WANT_HET:=y}"             # partitioned Python -> Snowflake landing
  : "${WANT_FRESH:=y}"           # freshness_check on imported entity
  : "${WANT_SNOWPARK:=y}"        # snowpark_pipeline
  : "${WANT_EXTERNAL:=y}"        # external_snowflake_table
  : "${WANT_DBT:=y}"             # dbt project
  : "${WANT_DDL_SHOWCASE:=y}"    # 7 define-as-code DDL components
  export WANT_DEPS WANT_PIPELINE WANT_AUTOCOND WANT_CORTEX WANT_OBSERVER \
         WANT_HET WANT_FRESH WANT_SNOWPARK WANT_EXTERNAL WANT_DBT WANT_DDL_SHOWCASE
fi

# ── 1. uv guard (auto-install if missing) ──────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  echo "uv (Python package manager) is required and not installed."
  read -r -p "Install uv now? [Y/n] " ans
  case "${ans:-y}" in
    y|Y|yes) curl -fsSL https://astral.sh/uv/install.sh | sh
             # shellcheck disable=SC1090
             . "$HOME/.cargo/env" 2>/dev/null || true
             . "$HOME/.local/bin/env"  2>/dev/null || true
             export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" ;;
    *)       echo "Aborted. Install uv from https://docs.astral.sh/uv/"; exit 1 ;;
  esac
fi

# ── 2. Project name ────────────────────────────────────────────────────
DEFAULT_PROJECT="snowflake-dagster"
REUSE_EXISTING=false
while :; do
  read -r -p "Project name [$DEFAULT_PROJECT]: " PROJECT
  PROJECT="${PROJECT:-$DEFAULT_PROJECT}"
  if [ -e "$PROJECT" ]; then
    # Existing project — offer fast paths for stage iteration.
    echo "  '$PROJECT' already exists."
    echo "    [r]euse — keep venv + installed components, OVERWRITE defs.yaml / .env.demo / dbt/ from this run (fastest)"
    echo "    [d]elete — rm -rf and rebuild from scratch"
    echo "    [c]hange — pick a different name"
    read -r -p "  Choice [r/d/c]: " CHOICE
    case "${CHOICE:-r}" in
      r|R|reuse)
        echo "  ✓ Reusing existing project (defs/ will be overwritten)."
        REUSE_EXISTING=true
        break
        ;;
      d|D|delete)
        echo "  Deleting $PROJECT ..."
        rm -rf "$PROJECT"
        break
        ;;
      c|C|change)
        continue
        ;;
      *)
        echo "  Pick r, d, or c." ; continue
        ;;
    esac
  else
    break
  fi
done
[ "$REUSE_EXISTING" = "true" ] || PARTIAL_PROJECT_PATH="$PROJECT"

# ── 3. Snowflake credentials (prompt + verify) ──────────────────────────
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

# Auth method — keypair is the recommended default (headless, works with
# Dagster's daemon for sensors + schedules). Password preserved for accounts
# that still allow it. SSO is fine for laptop `dg dev` but the daemon can't
# do browser auth.
echo
echo "Authentication method:"
echo "  [1] Keypair (RSA private key file) — headless, recommended"
echo "  [2] SSO (externalbrowser) — pops a browser; OK for laptop dg dev only"
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
    prompt_default "Path to RSA private key file (PEM/P8)" SNOW_KEY_FILE \
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
    echo "  (SSO uses externalbrowser; a browser tab will open for auth)"
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
prompt_default "Database"  SNOW_DATABASE  "${SNOWFLAKE_DATABASE:-}"
prompt_default "Schema"    SNOW_SCHEMA    "${SNOWFLAKE_SCHEMA:-PUBLIC}"
prompt_default "Role (leave blank for default)" SNOW_ROLE "${SNOWFLAKE_ROLE:-}"

if [ -z "$SNOW_ACCOUNT" ] || [ -z "$SNOW_USER" ] || [ -z "$SNOW_DATABASE" ]; then
  echo "  ⚠ account / user / database are required."
  exit 1
fi
if [ "$SNOW_AUTH_METHOD" = "password" ] && [ -z "$SNOW_PASS" ]; then
  echo "  ⚠ password is required when auth method is 'password'."
  exit 1
fi

echo
echo ">>> Verifying connection to Snowflake ..."
# Use uv's ephemeral environment to run snowflake-connector-python once for
# verification + entity discovery, without polluting the not-yet-scaffolded
# project. Results land in /tmp/_sf_inv.json for the discovery step below.
INV_OUT="$(mktemp -t sf_inv.XXXXXX).json"
SF_PY_PRELUDE=$(cat <<PYHEAD
import json, os, sys
import snowflake.connector as sc
ck = dict(
    account=os.environ['SF_ACCOUNT'],
    user=os.environ['SF_USER'],
    warehouse=os.environ['SF_WAREHOUSE'],
    database=os.environ['SF_DATABASE'],
    schema=os.environ['SF_SCHEMA'],
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
else:
    ck['password'] = os.environ.get('SF_PASS', '')
try:
    conn = sc.connect(**ck)
except Exception as e:
    print(f"ERR: {e}", file=sys.stderr)
    sys.exit(1)
cur = conn.cursor()
PYHEAD
)

# Verification: SELECT CURRENT_VERSION() → must return a row.
SF_ACCOUNT="$SNOW_ACCOUNT" SF_USER="$SNOW_USER" SF_PASS="$SNOW_PASS" SF_AUTH_METHOD="$SNOW_AUTH_METHOD" SF_KEY_FILE="$SNOW_KEY_FILE" SF_KEY_PWD="$SNOW_KEY_PWD" SF_PAT="$SNOW_PAT" \
  SF_WAREHOUSE="$SNOW_WAREHOUSE" SF_DATABASE="$SNOW_DATABASE" SF_SCHEMA="$SNOW_SCHEMA" \
  SF_ROLE="$SNOW_ROLE" \
  uv run --quiet --with 'snowflake-connector-python[secure-local-storage]' --no-project python - <<EOF
$SF_PY_PRELUDE
cur.execute("SELECT CURRENT_VERSION()")
row = cur.fetchone()
print(f"  ✓ Connected. Snowflake version: {row[0]}")
cur.close(); conn.close()
EOF

if [ $? -ne 0 ]; then
  echo "  ⚠ Could not verify the connection."
  read -r -p "Continue anyway and write the project without verification? [y/N] " ans
  case "${ans:-n}" in y|Y|yes) ;; *) exit 1 ;; esac
fi

# ── 4. Entity discovery (counts per type in the chosen database.schema) ─
echo
echo "─────────────────────────────────────────────────────────────────────"
echo "  Discovering importable entities in $SNOW_DATABASE.$SNOW_SCHEMA ..."
echo "─────────────────────────────────────────────────────────────────────"

SF_ACCOUNT="$SNOW_ACCOUNT" SF_USER="$SNOW_USER" SF_PASS="$SNOW_PASS" SF_AUTH_METHOD="$SNOW_AUTH_METHOD" SF_KEY_FILE="$SNOW_KEY_FILE" SF_KEY_PWD="$SNOW_KEY_PWD" SF_PAT="$SNOW_PAT" \
  SF_WAREHOUSE="$SNOW_WAREHOUSE" SF_DATABASE="$SNOW_DATABASE" SF_SCHEMA="$SNOW_SCHEMA" \
  SF_ROLE="$SNOW_ROLE" INV_OUT="$INV_OUT" \
  uv run --quiet --with 'snowflake-connector-python[secure-local-storage]' --no-project python - <<EOF
$SF_PY_PRELUDE
inv = {"types": {}}
# Per-type discovery queries. Wrap each in try/except so a permission error
# on one type doesn't blow up the whole discovery.
queries = [
    ("tasks",                f"SELECT name FROM {os.environ['SF_DATABASE']}.INFORMATION_SCHEMA.TASKS WHERE schema_name = '{os.environ['SF_SCHEMA']}' ORDER BY name"),
    ("dynamic_tables",       f"SELECT name FROM {os.environ['SF_DATABASE']}.INFORMATION_SCHEMA.DYNAMIC_TABLES WHERE schema_name = '{os.environ['SF_SCHEMA']}' ORDER BY name"),
    ("stored_procedures",    f"SELECT procedure_name AS name FROM {os.environ['SF_DATABASE']}.INFORMATION_SCHEMA.PROCEDURES WHERE procedure_schema = '{os.environ['SF_SCHEMA']}' ORDER BY name"),
    ("streams",              f"SHOW STREAMS IN SCHEMA {os.environ['SF_DATABASE']}.{os.environ['SF_SCHEMA']}"),
    ("snowpipes",            f"SHOW PIPES IN SCHEMA {os.environ['SF_DATABASE']}.{os.environ['SF_SCHEMA']}"),
    ("stages",               f"SHOW STAGES IN SCHEMA {os.environ['SF_DATABASE']}.{os.environ['SF_SCHEMA']}"),
    ("materialized_views",   f"SELECT table_name AS name FROM {os.environ['SF_DATABASE']}.INFORMATION_SCHEMA.VIEWS WHERE table_schema = '{os.environ['SF_SCHEMA']}' AND is_secure = 'NO'"),
    ("external_tables",      f"SHOW EXTERNAL TABLES IN SCHEMA {os.environ['SF_DATABASE']}.{os.environ['SF_SCHEMA']}"),
    ("alerts",               f"SHOW ALERTS IN SCHEMA {os.environ['SF_DATABASE']}.{os.environ['SF_SCHEMA']}"),
]
for label, q in queries:
    try:
        cur.execute(q)
        rows = cur.fetchall()
        # First column is the entity name across both SHOW and SELECT shapes.
        names = [r[1] if (label in ("streams","snowpipes","stages","external_tables","alerts") and len(r) > 1) else r[0] for r in rows]
        inv["types"][label] = names
    except Exception as e:
        inv["types"][label] = {"error": str(e)}
# Also enumerate base tables — useful to pick source tables for warehouse_pipeline.
try:
    cur.execute(f"SELECT table_name FROM {os.environ['SF_DATABASE']}.INFORMATION_SCHEMA.TABLES WHERE table_schema = '{os.environ['SF_SCHEMA']}' AND table_type = 'BASE TABLE' ORDER BY table_name")
    inv["base_tables"] = [r[0] for r in cur.fetchall()]
except Exception as e:
    inv["base_tables"] = {"error": str(e)}
with open(os.environ['INV_OUT'], 'w') as f:
    json.dump(inv, f)
cur.close(); conn.close()
EOF
DISC_RC=$?

if [ $DISC_RC -ne 0 ] || [ ! -s "$INV_OUT" ]; then
  echo "  ⚠ Discovery query failed. Continuing with empty inventory (you can"
  echo "    still hand-edit the workspace defs.yaml after setup)."
  echo '{"types":{},"base_tables":[]}' > "$INV_OUT"
fi

echo "Found entities in $SNOW_DATABASE.$SNOW_SCHEMA:"
python3 - "$INV_OUT" <<'PYEOF'
import json, sys
inv = json.load(open(sys.argv[1]))
order = ["tasks","dynamic_tables","stored_procedures","streams","snowpipes",
         "stages","materialized_views","external_tables","alerts"]
for k in order:
    v = inv["types"].get(k)
    if isinstance(v, dict) and "error" in v:
        print(f"  {k:22}  (skipped: {v['error'][:60]})")
    elif v is None:
        print(f"  {k:22}  —")
    else:
        sample = ", ".join(v[:3])
        more = f" + {len(v)-3} more" if len(v) > 3 else ""
        print(f"  {k:22}  {len(v):4}  {sample}{more}")
bt = inv.get("base_tables", [])
if isinstance(bt, list):
    print(f"  base_tables (info)     {len(bt):4}  {', '.join(bt[:3])}{' + …' if len(bt)>3 else ''}")
PYEOF

# ── 5. Pick entity TYPES to import ─────────────────────────────────────
echo
echo "─────────────────────────────────────────────────────────────────────"
echo "  Pick entity types to import (y/n per type)"
echo "─────────────────────────────────────────────────────────────────────"

yn() {
  local prompt="$1" def="$2" label
  # bash 3.2 (macOS default) doesn't support `${var^^}` — uppercase via tr.
  label=$(printf "%s" "$def" | tr '[:lower:]' '[:upper:]')
  read -r -p "  $prompt [$label/n]: " ans
  ans="${ans:-$def}"
  case "$ans" in y|Y|yes) echo "true" ;; *) echo "false" ;; esac
}

IMPORT_TASKS=$(yn          "Tasks (scheduled SQL routines)?               " "y")
IMPORT_DYNAMIC_TABLES=$(yn "Dynamic Tables (materialized auto-refresh)?  " "y")
IMPORT_STORED_PROCS=$(yn   "Stored Procedures?                             " "n")
IMPORT_STREAMS=$(yn        "Streams (change data capture)?                 " "n")
IMPORT_SNOWPIPES=$(yn      "Snowpipes (continuous ingestion)?              " "n")
IMPORT_STAGES=$(yn         "Stages?                                        " "n")
IMPORT_MAT_VIEWS=$(yn      "Materialized Views?                            " "n")
IMPORT_EXT_TABLES=$(yn     "External Tables?                               " "n")
IMPORT_ALERTS=$(yn         "Alerts?                                        " "n")

echo
echo "Optional: include/exclude regex filters (applied to entity names)"
read -r -p "  filter_by_name_pattern (blank for all): "    FILTER_NAME
read -r -p "  exclude_name_pattern   (blank for none): "   EXCLUDE_NAME

# ── 6. Cross-entity dep wiring (optional) ──────────────────────────────
# Mirrors the databricks_workspace dep-asking flow. Asks per imported task
# (and dynamic table) which OTHER imported entities it depends on.
declare -a DEP_ENTITIES=()
declare -a DEP_TYPES=()
declare -a DEP_UPSTREAMS=()  # parallel array: "|"-separated upstream names

echo
[ -n "${WANT_DEPS:-}" ] || read -r -p "Declare cross-entity dependencies now? [y/N] " WANT_DEPS
if [ "${WANT_DEPS:-n}" = "y" ] || [ "${WANT_DEPS:-n}" = "Y" ]; then
  # Build the candidate list (names of imported entities, with type prefix
  # so the user can see what they're picking from). Only includes types
  # that were turned on above + have any names.
  CAND=$(python3 - "$INV_OUT" \
    "$IMPORT_TASKS" "$IMPORT_DYNAMIC_TABLES" "$IMPORT_STORED_PROCS" \
    "$IMPORT_STREAMS" "$IMPORT_SNOWPIPES" "$IMPORT_STAGES" \
    "$IMPORT_MAT_VIEWS" "$IMPORT_EXT_TABLES" "$IMPORT_ALERTS" <<'PYEOF'
import json, sys
inv = json.load(open(sys.argv[1]))
flags = sys.argv[2:11]
type_keys = ["tasks","dynamic_tables","stored_procedures","streams","snowpipes",
             "stages","materialized_views","external_tables","alerts"]
out = []
for flag, key in zip(flags, type_keys):
    if flag == "true":
        v = inv["types"].get(key)
        if isinstance(v, list):
            for name in v:
                out.append(f"{key}\t{name}")
for i, row in enumerate(out, 1):
    t, n = row.split("\t")
    print(f"  [{i:3}] {t:22} {n}")
print("---DELIM---")
for row in out:
    print(row)
PYEOF
)
  echo "$CAND" | sed -n '/---DELIM---/q;p'
  ENTITY_TABLE=$(echo "$CAND" | sed -n '/---DELIM---/,$p' | tail -n +2)
  if [ -z "$ENTITY_TABLE" ]; then
    echo "  (no entities discovered for the selected types — skipping dep wiring)"
  else
    N_CAND=$(echo "$ENTITY_TABLE" | wc -l | tr -d ' ')
    echo
    echo "For each entity, list which OTHER entities it depends on (by"
    echo "number, comma-separated). Press Enter for none."
    echo
    idx=0
    while IFS=$'\t' read -r etype ename; do
      idx=$((idx+1))
      [ -z "$ename" ] && continue
      while :; do
        read -r -p "  [$idx] $ename depends on: " RAW_DEPS
        DEP_NAMES=""
        BAD=false
        if [ -n "$RAW_DEPS" ]; then
          OLDIFS="$IFS"; IFS=','
          for didx in $RAW_DEPS; do
            IFS="$OLDIFS"
            didx="${didx// /}"
            [ -z "$didx" ] && continue
            if ! echo "$didx" | grep -qE '^[0-9]+$'; then
              echo "    Non-numeric: '$didx' — re-enter."; BAD=true; break
            fi
            if [ "$didx" -lt 1 ] || [ "$didx" -gt "$N_CAND" ]; then
              echo "    Out of range: $didx (have 1..$N_CAND) — re-enter."; BAD=true; break
            fi
            if [ "$didx" = "$idx" ]; then
              echo "    (skipped self-reference: $didx)"; continue
            fi
            DEP_LINE=$(echo "$ENTITY_TABLE" | sed -n "${didx}p")
            DEP_TYPE=$(echo "$DEP_LINE" | cut -f1)
            DEP_NAME=$(echo "$DEP_LINE" | cut -f2)
            DEP_NAMES="${DEP_NAMES}${DEP_TYPE}/${DEP_NAME}|"
          done
          IFS="$OLDIFS"
          [ "$BAD" = "true" ] && continue
          DEP_NAMES="${DEP_NAMES%|}"
        fi
        DEP_ENTITIES+=("$ename")
        DEP_TYPES+=("$etype")
        DEP_UPSTREAMS+=("$DEP_NAMES")
        break
      done
    done <<< "$ENTITY_TABLE"
  fi
fi

# ── 7. Optional: multi-step warehouse_pipeline on top ───────────────────
echo
echo "─────────────────────────────────────────────────────────────────────"
echo "  Optional add-ons"
echo "─────────────────────────────────────────────────────────────────────"
[ -n "${WANT_PIPELINE:-}" ] || read -r -p "Add a multi-step warehouse_pipeline demo (joins + op:sql commission + multi-sink)? [Y/n] " WANT_PIPELINE
WANT_PIPELINE="${WANT_PIPELINE:-y}"
if [ "$WANT_PIPELINE" = "y" ] || [ "$WANT_PIPELINE" = "Y" ]; then
  echo "  Pick two base tables from the discovered list, or paste your own."
  prompt_default "Orders-like table (FULLY QUALIFIED, e.g. RAW.ORDERS)" PIPE_ORDERS ""
  prompt_default "Customers-like table (FULLY QUALIFIED, e.g. RAW.CUSTOMERS)" PIPE_CUSTOMERS ""
  prompt_default "Output schema for the pipeline's two sink tables" PIPE_OUT_SCHEMA "ANALYTICS"
  if [ -z "$PIPE_ORDERS" ] || [ -z "$PIPE_CUSTOMERS" ]; then
    echo "  (both table names required — skipping pipeline add-on)"
    WANT_PIPELINE="n"
  fi
fi

[ -n "${WANT_CORTEX:-}" ] || read -r -p "Add a snowflake_cortex_asset (LLM completion / summarize / sentiment)? [Y/n] " WANT_CORTEX
WANT_CORTEX="${WANT_CORTEX:-y}"
CORTEX_MODE=""
CORTEX_INPUT=""
if [ "$WANT_CORTEX" = "y" ] || [ "$WANT_CORTEX" = "Y" ]; then
  echo "  Cortex mode: 'complete' (free-form), 'summarize', or 'sentiment'."
  prompt_default "Cortex mode" CORTEX_MODE "summarize"
  prompt_default "Input text (or {{ jinja }} referencing other assets)" CORTEX_INPUT \
    "Snowflake is a cloud data platform. It separates storage and compute, supports semi-structured data natively, and offers time-travel features."
fi

# Reactive trigger: snowflake_table_observation_sensor watches a table's
# row count + ingests changes. Demonstrates "react to table mutation"
# beyond what Snowpipe's cloud-storage trigger can express.
[ -n "${WANT_OBSERVER:-}" ] || read -r -p "Add a snowflake_table_observation_sensor watching a table for changes? [Y/n] " WANT_OBSERVER
WANT_OBSERVER="${WANT_OBSERVER:-y}"
OBSERVER_DATABASE=""
OBSERVER_SCHEMA=""
OBSERVER_TABLE=""
if [ "$WANT_OBSERVER" = "y" ] || [ "$WANT_OBSERVER" = "Y" ]; then
  prompt_default "Database to watch"          OBSERVER_DATABASE "$SNOW_DATABASE"
  prompt_default "Schema to watch"            OBSERVER_SCHEMA   "RAW"
  prompt_default "Table to watch"             OBSERVER_TABLE    "ORDERS"
fi

# Reactive chaining: wire AutomationCondition.eager() on the pipeline
# asset so it fires the moment any of its imported upstreams change.
# Only meaningful if the user selected the pipeline add-on.
WANT_AUTOCOND="${WANT_AUTOCOND:-}"
if [ "$WANT_PIPELINE" = "y" ] || [ "$WANT_PIPELINE" = "Y" ]; then
  [ -n "$WANT_AUTOCOND" ] || read -r -p "Wire AutomationCondition.eager() on the pipeline so it auto-reacts to upstream changes? [Y/n] " WANT_AUTOCOND
  WANT_AUTOCOND="${WANT_AUTOCOND:-y}"
fi

# Heterogeneous + partitioned: synthetic_data_generator (Python, daily
# partitioned) -> dataframe_to_snowflake (also daily partitioned). One
# scaffold proves TWO claims: cross-engine lineage (Python on the left,
# Snowflake on the right) AND first-class partition replay (backfill 30
# days, concurrency-capped, from the dg dev UI).
[ -n "${WANT_HET:-}" ] || read -r -p "Add a partitioned Python -> Snowflake landing chain (heterogeneous + backfillable)? [Y/n] " WANT_HET
WANT_HET="${WANT_HET:-y}"
HET_DATABASE=""
HET_SCHEMA=""
HET_TABLE=""
HET_PARTITION_START=""
if [ "$WANT_HET" = "y" ] || [ "$WANT_HET" = "Y" ]; then
  prompt_default "Destination database for the daily landing"      HET_DATABASE "$SNOW_DATABASE"
  prompt_default "Destination schema for the daily landing"        HET_SCHEMA   "RAW"
  prompt_default "Destination table for the daily landing"         HET_TABLE    "PYTHON_DAILY_EVENTS"
  prompt_default "Partition start date (YYYY-MM-DD; today by default)" HET_PARTITION_START \
    "$(date -u -v-30d +%Y-%m-%d 2>/dev/null || date -u --date='30 days ago' +%Y-%m-%d)"
fi

# Data quality: freshness_check on a chosen imported asset.
[ -n "${WANT_FRESH:-}" ] || read -r -p "Add a freshness_check asset check on one of the imported entities? [Y/n] " WANT_FRESH
WANT_FRESH="${WANT_FRESH:-y}"
FRESH_ASSET_KEY=""
FRESH_FAIL_HOURS=""
if [ "$WANT_FRESH" = "y" ] || [ "$WANT_FRESH" = "Y" ]; then
  echo "  Asset key format mirrors workspace asset keys: <type>/<lowercased_name>."
  echo "  Examples: tasks/daily_orders_rollup, dynamic_tables/paid_orders_dt"
  prompt_default "Asset key to attach the freshness check to" FRESH_ASSET_KEY "tasks/daily_orders_rollup"
  prompt_default "Fail if no update within N hours"           FRESH_FAIL_HOURS "26"
fi

# snowpark_pipeline — the Snowpark DataFrame parallel to warehouse_pipeline.
# Same multi-step shape (steps + ref + op:sql + multi-sink), but uses
# Snowpark's lazy DataFrame API instead of compiling to CTAS+CTEs. Shows
# customers that Dagster works equally well with either Snowflake compute
# paradigm — pure SQL OR DataFrame.
[ -n "${WANT_SNOWPARK:-}" ] || read -r -p "Add a snowpark_pipeline (DataFrame-API parallel to warehouse_pipeline)? [Y/n] " WANT_SNOWPARK
WANT_SNOWPARK="${WANT_SNOWPARK:-y}"
SP_ORDERS=""
SP_CUSTOMERS=""
SP_OUT_SCHEMA=""
if [ "$WANT_SNOWPARK" = "y" ] || [ "$WANT_SNOWPARK" = "Y" ]; then
  prompt_default "Orders-like table for snowpark_pipeline"        SP_ORDERS    "${PIPE_ORDERS:-RAW.ORDERS}"
  prompt_default "Customers-like table for snowpark_pipeline"     SP_CUSTOMERS "${PIPE_CUSTOMERS:-RAW.CUSTOMERS}"
  prompt_default "Output schema for snowpark_pipeline sink tables" SP_OUT_SCHEMA "${PIPE_OUT_SCHEMA:-ANALYTICS}"
fi

# external_snowflake_table — declare-only lineage. Useful for "this table
# is owned by another team / managed outside Dagster, but we want to
# depend on it." Common enterprise pattern.
[ -n "${WANT_EXTERNAL:-}" ] || read -r -p "Declare an external_snowflake_table (lineage to a table you don't manage)? [Y/n] " WANT_EXTERNAL
WANT_EXTERNAL="${WANT_EXTERNAL:-y}"
EXT_DATABASE=""
EXT_SCHEMA=""
EXT_TABLE=""
EXT_KEY=""
if [ "$WANT_EXTERNAL" = "y" ] || [ "$WANT_EXTERNAL" = "Y" ]; then
  prompt_default "Database of the externally-managed table"       EXT_DATABASE "$SNOW_DATABASE"
  prompt_default "Schema of the externally-managed table"         EXT_SCHEMA   "RAW"
  prompt_default "Table name"                                     EXT_TABLE    "PRODUCTS"
  EXT_KEY_DEFAULT=$(echo "external/$EXT_DATABASE/$EXT_SCHEMA/$EXT_TABLE" | tr '[:upper:]' '[:lower:]')
  prompt_default "Dagster asset key for this external reference"  EXT_KEY      "$EXT_KEY_DEFAULT"
fi

# dbt — Dagster's official `dagster-dbt` integration. Scaffolds a tiny
# dbt project (dbt_project.yml + profiles.yml + 2 models) inside the
# Dagster project. The DbtProjectComponent imports every model in the
# project as a Dagster asset, with lineage from the source tables in
# RAW.* through the dbt models to the final mart.
[ -n "${WANT_DBT:-}" ] || read -r -p "Add a dbt project (Dagster's official dagster-dbt integration)? [Y/n] " WANT_DBT
WANT_DBT="${WANT_DBT:-y}"
DBT_SOURCE_DB=""
DBT_SOURCE_SCHEMA=""
DBT_TARGET_SCHEMA=""
if [ "$WANT_DBT" = "y" ] || [ "$WANT_DBT" = "Y" ]; then
  prompt_default "Source database for dbt models"       DBT_SOURCE_DB    "$SNOW_DATABASE"
  prompt_default "Source schema (where RAW.* tables live)" DBT_SOURCE_SCHEMA "RAW"
  prompt_default "Target schema (where dbt models materialize)" DBT_TARGET_SCHEMA "DBT_ANALYTICS"
fi

# 7 'define-as-code' DDL components (snowflake_task, snowflake_dynamic_table,
# snowflake_stream, snowflake_stored_procedure, snowflake_snowpipe,
# snowflake_alert, snowflake_materialized_view). Single y/N to scaffold ONE
# minimal example of each into the project, demonstrating "Dagster as Snowflake's
# control plane" alongside the workspace component's "import existing" pattern.
# Entities are name-prefixed DG_ to avoid colliding with the seed's objects and
# to let the workspace component exclude them via exclude_name_pattern.
[ -n "${WANT_DDL_SHOWCASE:-}" ] || read -r -p "Add a showcase of all 7 'define-Snowflake-as-code' DDL components? [Y/n] " WANT_DDL_SHOWCASE
WANT_DDL_SHOWCASE="${WANT_DDL_SHOWCASE:-y}"
DDL_TARGET_SCHEMA=""
if [ "$WANT_DDL_SHOWCASE" = "y" ] || [ "$WANT_DDL_SHOWCASE" = "Y" ]; then
  prompt_default "Schema where the 7 Dagster-defined entities go" DDL_TARGET_SCHEMA "$SNOW_SCHEMA"
fi

# ── 8. Scaffold the project (or reuse existing) ────────────────────────
echo
if [ "$REUSE_EXISTING" = "true" ]; then
  echo ">>> Reusing existing project at $PROJECT ..."
  cd "$PROJECT"
  # Safety guard: confirm this directory IS a create-dagster project before
  # we start mutating it.
  if [ ! -d src ] || [ -z "$(ls src 2>/dev/null)" ] || [ ! -f pyproject.toml ]; then
    echo "  ⚠ $PROJECT doesn't look like a Dagster project (missing src/<pkg>/ or"
    echo "    pyproject.toml). Refusing to mutate. Pick [d]elete next time, or"
    echo "    use a different project name."
    exit 1
  fi
  PKG="$(ls src/ | head -1)"
  # Wipe every defs.yaml dir we MIGHT recreate this run so stale ones from
  # a prior run with different selections don't linger.
  for d in snowflake_workspace regional_top_paid_pipeline cortex_demo \
           row_count_observer python_daily_events python_daily_events_to_snowflake \
           freshness_check_demo dbt_project \
           snowpark_pipeline_demo external_table_demo \
           dg_task dg_dynamic_table dg_stream dg_stored_procedure \
           dg_snowpipe dg_alert dg_materialized_view; do
    rm -rf "src/$PKG/defs/$d"
  done
  rm -rf dbt
else
  echo ">>> Scaffolding Dagster project at $PROJECT ..."
  uvx create-dagster@latest project "$PROJECT" --no-uv-sync >/dev/null
  cd "$PROJECT"
  PKG="$(ls src/ | head -1)"
  uv add -q 'yarl<1.24'
  uv add --dev -q dagster-dg-cli dagster-webserver
  uv add -q 'snowflake-connector-python>=3.7.0'
fi

CLI="uvx --from dagster-community-components-cli dagster-component"

# Install components — idempotent: `dagster-component add --auto-install`
# is safe to call when the component dir already exists (re-fetches +
# overwrites the source). The auto-installed sample defs.yaml is removed
# below; we'll write our own.
echo ">>> Installing snowflake_workspace component ..."
$CLI add snowflake_workspace --auto-install
rm -rf "src/$PKG/defs/snowflake_workspace"

if [ "$WANT_PIPELINE" = "y" ] || [ "$WANT_PIPELINE" = "Y" ]; then
  echo ">>> Installing warehouse_pipeline component ..."
  $CLI add warehouse_pipeline --auto-install
  rm -rf "src/$PKG/defs/warehouse_pipeline"
fi
if [ "$WANT_CORTEX" = "y" ] || [ "$WANT_CORTEX" = "Y" ]; then
  echo ">>> Installing snowflake_cortex_asset component ..."
  $CLI add snowflake_cortex_asset --auto-install
  rm -rf "src/$PKG/defs/snowflake_cortex_asset"
fi
if [ "$WANT_OBSERVER" = "y" ] || [ "$WANT_OBSERVER" = "Y" ]; then
  echo ">>> Installing snowflake_table_observation_sensor component ..."
  $CLI add snowflake_table_observation_sensor --auto-install
  rm -rf "src/$PKG/defs/snowflake_table_observation_sensor"
fi
if [ "$WANT_HET" = "y" ] || [ "$WANT_HET" = "Y" ]; then
  echo ">>> Installing synthetic_data_generator + dataframe_to_snowflake components ..."
  $CLI add synthetic_data_generator --auto-install
  rm -rf "src/$PKG/defs/synthetic_data_generator"
  $CLI add dataframe_to_snowflake --auto-install
  rm -rf "src/$PKG/defs/dataframe_to_snowflake"
fi
if [ "$WANT_FRESH" = "y" ] || [ "$WANT_FRESH" = "Y" ]; then
  echo ">>> Installing freshness_check component ..."
  $CLI add freshness_check --auto-install
  rm -rf "src/$PKG/defs/freshness_check"
fi
if [ "$WANT_SNOWPARK" = "y" ] || [ "$WANT_SNOWPARK" = "Y" ]; then
  echo ">>> Installing snowpark_pipeline component ..."
  $CLI add snowpark_pipeline --auto-install
  rm -rf "src/$PKG/defs/snowpark_pipeline"
  uv add -q 'snowflake-snowpark-python>=1.10.0'
fi
if [ "$WANT_EXTERNAL" = "y" ] || [ "$WANT_EXTERNAL" = "Y" ]; then
  echo ">>> Installing external_snowflake_table component ..."
  $CLI add external_snowflake_table --auto-install
  rm -rf "src/$PKG/defs/external_snowflake_table"
fi
if [ "$WANT_DBT" = "y" ] || [ "$WANT_DBT" = "Y" ]; then
  echo ">>> Installing dagster-dbt + dbt-snowflake ..."
  uv add -q dagster-dbt 'dbt-core>=1.7' dbt-snowflake
fi
if [ "$WANT_DDL_SHOWCASE" = "y" ] || [ "$WANT_DDL_SHOWCASE" = "Y" ]; then
  echo ">>> Installing 7 snowflake_<entity> DDL components ..."
  for c in snowflake_task snowflake_dynamic_table snowflake_stream \
           snowflake_stored_procedure snowflake_snowpipe \
           snowflake_alert snowflake_materialized_view; do
    $CLI add "$c" --auto-install
    rm -rf "src/$PKG/defs/$c"
  done
fi

write_yaml() {
  local name="$1"; shift
  local body="$1"; shift
  mkdir -p "src/$PKG/defs/$name"
  printf "%s\n" "$body" > "src/$PKG/defs/$name/defs.yaml"
}

# ── Auth helpers (emit the right YAML fields per auth method) ──────────
# Two flavors needed since different components use different field names:
#   _direct  emits direct fields:    password / authenticator / private_key_file
#   _envvar  emits env-var fields:   password_env_var / private_key_file_env_var / etc.
# Both prefix output with 2 spaces for indentation under `attributes:`.

snow_auth_fields_direct() {
  # Args: $1 = leading indent (e.g. "  " or "    ")
  local I="${1:-  }"
  case "$SNOW_AUTH_METHOD" in
    keypair)
      printf '%sauthenticator: SNOWFLAKE_JWT\n' "$I"
      printf '%sprivate_key_file: "{{ env('"'"'SNOWFLAKE_PRIVATE_KEY_FILE'"'"') }}"\n' "$I"
      [ -n "$SNOW_KEY_PWD" ] && \
        printf '%sprivate_key_file_pwd: "{{ env('"'"'SNOWFLAKE_PRIVATE_KEY_FILE_PWD'"'"') }}"\n' "$I"
      ;;
    sso)
      printf '%sauthenticator: externalbrowser\n' "$I"
      ;;
    pat)
      # PAT uses authenticator=PROGRAMMATIC_ACCESS_TOKEN with the token in
      # the dedicated `token` field. Components patched in templates commit
      # bfadf23d already accept `token` as an Optional[str] field.
      printf '%sauthenticator: PROGRAMMATIC_ACCESS_TOKEN\n' "$I"
      printf '%stoken: "{{ env('"'"'SNOWFLAKE_PAT'"'"') }}"\n' "$I"
      ;;
    password|*)
      printf '%spassword: "{{ env('"'"'SNOWFLAKE_PASSWORD'"'"') }}"\n' "$I"
      ;;
  esac
}

snow_auth_fields_envvar() {
  # Same shape but emits the *_env_var field-name convention.
  local I="${1:-  }"
  case "$SNOW_AUTH_METHOD" in
    keypair)
      printf '%sauthenticator: SNOWFLAKE_JWT\n' "$I"
      printf '%sprivate_key_file_env_var: SNOWFLAKE_PRIVATE_KEY_FILE\n' "$I"
      [ -n "$SNOW_KEY_PWD" ] && \
        printf '%sprivate_key_file_pwd_env_var: SNOWFLAKE_PRIVATE_KEY_FILE_PWD\n' "$I"
      ;;
    sso)
      printf '%sauthenticator: externalbrowser\n' "$I"
      ;;
    pat)
      printf '%sauthenticator: PROGRAMMATIC_ACCESS_TOKEN\n' "$I"
      printf '%stoken_env_var: SNOWFLAKE_PAT\n' "$I"
      ;;
    password|*)
      printf '%spassword_env_var: SNOWFLAKE_PASSWORD\n' "$I"
      ;;
  esac
}

# SQLAlchemy URL for warehouse_pipeline. snowflake-sqlalchemy supports
# both password-in-URL (deprecated for production) and keypair/SSO via
# URL params + connect_args. For the demo, we use the URL-param form
# since warehouse_pipeline only takes a string.
build_snowflake_url() {
  case "$SNOW_AUTH_METHOD" in
    keypair)
      local pwdarg=""
      [ -n "$SNOW_KEY_PWD" ] && pwdarg="&private_key_file_pwd=$SNOW_KEY_PWD"
      printf 'snowflake://%s@%s/%s/%s?warehouse=%s&authenticator=SNOWFLAKE_JWT&private_key_file=%s%s%s' \
        "$SNOW_USER" "$SNOW_ACCOUNT" "$SNOW_DATABASE" "$SNOW_SCHEMA" "$SNOW_WAREHOUSE" \
        "$SNOW_KEY_FILE" "$pwdarg" \
        "${SNOW_ROLE:+&role=$SNOW_ROLE}"
      ;;
    sso)
      printf 'snowflake://%s@%s/%s/%s?warehouse=%s&authenticator=externalbrowser%s' \
        "$SNOW_USER" "$SNOW_ACCOUNT" "$SNOW_DATABASE" "$SNOW_SCHEMA" "$SNOW_WAREHOUSE" \
        "${SNOW_ROLE:+&role=$SNOW_ROLE}"
      ;;
    pat)
      # PAT goes as a `token` query param (snowflake-sqlalchemy >=1.5 supports
      # this) with authenticator=PROGRAMMATIC_ACCESS_TOKEN. No user:pwd in
      # the URL path — auth is via the token param.
      printf 'snowflake://%s@%s/%s/%s?warehouse=%s&authenticator=PROGRAMMATIC_ACCESS_TOKEN&token=%s%s' \
        "$SNOW_USER" "$SNOW_ACCOUNT" "$SNOW_DATABASE" "$SNOW_SCHEMA" "$SNOW_WAREHOUSE" \
        "$SNOW_PAT" \
        "${SNOW_ROLE:+&role=$SNOW_ROLE}"
      ;;
    password|*)
      printf 'snowflake://%s:%s@%s/%s/%s?warehouse=%s%s' \
        "$SNOW_USER" "$SNOW_PASS" "$SNOW_ACCOUNT" "$SNOW_DATABASE" "$SNOW_SCHEMA" "$SNOW_WAREHOUSE" \
        "${SNOW_ROLE:+&role=$SNOW_ROLE}"
      ;;
  esac
}

# ── 9. workspace defs.yaml ─────────────────────────────────────────────
ROLE_LINE=""
[ -n "$SNOW_ROLE" ] && ROLE_LINE="  role: \"$SNOW_ROLE\""

FILTER_LINE=""
[ -n "$FILTER_NAME" ]  && FILTER_LINE="$FILTER_LINE
  filter_by_name_pattern: \"$FILTER_NAME\""
[ -n "$EXCLUDE_NAME" ] && FILTER_LINE="$FILTER_LINE
  exclude_name_pattern: \"$EXCLUDE_NAME\""

AUTH_FIELDS_DIRECT="$(snow_auth_fields_direct '  ')"
WORKSPACE_YAML="type: $PKG.components.snowflake_workspace.component.SnowflakeWorkspaceComponent
attributes:
  account: \"{{ env('SNOWFLAKE_ACCOUNT') }}\"
  user:    \"{{ env('SNOWFLAKE_USER') }}\"
$AUTH_FIELDS_DIRECT
  warehouse: \"$SNOW_WAREHOUSE\"
  database:  \"$SNOW_DATABASE\"
  schema:    \"$SNOW_SCHEMA\"
$ROLE_LINE
  import_tasks: $IMPORT_TASKS
  import_dynamic_tables: $IMPORT_DYNAMIC_TABLES
  import_stored_procedures: $IMPORT_STORED_PROCS
  import_streams: $IMPORT_STREAMS
  import_snowpipes: $IMPORT_SNOWPIPES
  import_stages: $IMPORT_STAGES
  import_materialized_views: $IMPORT_MAT_VIEWS
  import_external_tables: $IMPORT_EXT_TABLES
  import_alerts: $IMPORT_ALERTS$FILTER_LINE"

# Cross-entity deps via assets_by_name (same shape as databricks_workspace's
# assets_by_task_key — `key`, `deps`, `group_name`, etc.)
if [ "${#DEP_ENTITIES[@]}" -gt 0 ]; then
  ABN="
  assets_by_name:"
  for i in "${!DEP_ENTITIES[@]}"; do
    NAME="${DEP_ENTITIES[$i]}"
    UPS="${DEP_UPSTREAMS[$i]}"
    [ -z "$UPS" ] && continue
    ABN="$ABN
    $NAME:
      deps:"
    OLDIFS="$IFS"; IFS='|'
    for u in $UPS; do
      IFS="$OLDIFS"
      [ -z "$u" ] && continue
      ABN="$ABN
        - \"$u\""
    done
    IFS="$OLDIFS"
  done
  WORKSPACE_YAML="$WORKSPACE_YAML$ABN"
fi

write_yaml "snowflake_workspace" "$WORKSPACE_YAML"

# ── 10. Optional warehouse_pipeline (multi-step) ───────────────────────
if [ "$WANT_PIPELINE" = "y" ] || [ "$WANT_PIPELINE" = "Y" ]; then
  write_yaml "regional_top_paid_pipeline" "type: $PKG.components.warehouse_pipeline.component.WarehousePipelineComponent
attributes:
  asset_name: regional_top_paid_pipeline
  dialect: snowflake
  database_url_env_var: SNOWFLAKE_URL
  steps:
    - id: delivered_orders
      source: {kind: table, table: $PIPE_ORDERS}
      operations:
        - {op: filter, predicate: \"STATUS = 'delivered'\"}

    - id: vip_customers
      source: {kind: table, table: $PIPE_CUSTOMERS}
      operations:
        - {op: filter, predicate: \"LIFETIME_VALUE > 3000\"}

    - id: enriched
      source: {kind: ref, ref: delivered_orders}
      operations:
        - op: join
          right: {ref: vip_customers}
          on_columns: [CUSTOMER_ID]
          how: inner
        - op: sql
          sql: |
            SELECT *, TOTAL * 0.15 AS COMMISSION
            FROM <<self>>
        - op: group_by
          group_by: [STATE]
          aggregations:
            REVENUE:          {col: TOTAL,      agg: sum}
            TOTAL_COMMISSION: {col: COMMISSION, agg: sum}
            ORDER_COUNT:      {col: ORDER_ID,   agg: count}

    - id: top_states
      source: {kind: ref, ref: enriched}
      operations:
        - {op: top_n, sort_by: REVENUE, n: 3, ascending: false}

  sinks:
    - {from: enriched,   table: $PIPE_OUT_SCHEMA.STATE_ENRICHED, mode: replace}
    - {from: top_states, table: $PIPE_OUT_SCHEMA.TOP_3_STATES,   mode: replace}

  group_name: snowflake_transforms
  include_preview_metadata: true

  # Demonstrate retries — opt into Dagster's RetryPolicy with 3 retries +
  # exponential backoff, useful for transient warehouse / network issues.
  retry_policy_max_retries: 3
  retry_policy_delay_seconds: 5
  retry_policy_backoff: exponential$(if [ "$WANT_AUTOCOND" = "y" ] || [ "$WANT_AUTOCOND" = "Y" ]; then
  printf "\n  automation_condition: \"{{ dg.AutomationCondition.eager() }}\""
  fi)"
fi

# ── 11. Optional Cortex asset ──────────────────────────────────────────
if [ "$WANT_CORTEX" = "y" ] || [ "$WANT_CORTEX" = "Y" ]; then
  # Cortex component uses snowflake_*_env_var field convention (4-space indent).
  CORTEX_AUTH_FIELDS=$(snow_auth_fields_envvar '  ' | sed 's/^  authenticator/  snowflake_authenticator/; s/^  private_key_file_env_var/  snowflake_private_key_file_env_var/; s/^  private_key_file_pwd_env_var/  snowflake_private_key_file_pwd_env_var/; s/^  password_env_var/  snowflake_password_env_var/; s/^/  /')
  write_yaml "cortex_demo" "type: $PKG.components.snowflake_cortex_asset.component.SnowflakeCortexAssetComponent
attributes:
  asset_name: cortex_demo
  snowflake_account_env_var: SNOWFLAKE_ACCOUNT
  snowflake_user_env_var:    SNOWFLAKE_USER
$CORTEX_AUTH_FIELDS
  snowflake_database: \"$SNOW_DATABASE\"
  snowflake_schema:   \"$SNOW_SCHEMA\"
  snowflake_warehouse: \"$SNOW_WAREHOUSE\"
  cortex_function: $CORTEX_MODE
  source_table: AI.CUSTOMER_FEEDBACK
  target_table: AI.CUSTOMER_FEEDBACK_${CORTEX_MODE}D
  text_column: COMMENT
  group_name: snowflake_ai"
fi

# ── 12. Optional observation sensor ────────────────────────────────────
# Reactive trigger: emits a runtime metric every check_interval_seconds
# AND triggers downstream when row count changes — no Snowpipe needed.
if [ "$WANT_OBSERVER" = "y" ] || [ "$WANT_OBSERVER" = "Y" ]; then
  # bash 3.2 (macOS default) lacks `${var,,}` lowercase — use tr.
  OBSERVER_TABLE_LC=$(echo "$OBSERVER_TABLE" | tr '[:upper:]' '[:lower:]')
  OBSERVER_DATABASE_LC=$(echo "$OBSERVER_DATABASE" | tr '[:upper:]' '[:lower:]')
  OBSERVER_SCHEMA_LC=$(echo "$OBSERVER_SCHEMA" | tr '[:upper:]' '[:lower:]')
  OBS_AUTH_FIELDS="$(snow_auth_fields_envvar '  ')"
  write_yaml "row_count_observer" "type: $PKG.components.snowflake_table_observation_sensor.component.SnowflakeTableObservationSensorComponent
attributes:
  sensor_name: ${OBSERVER_TABLE_LC}_row_count_observer
  asset_key: external/$OBSERVER_DATABASE_LC/$OBSERVER_SCHEMA_LC/$OBSERVER_TABLE_LC
  account: \"{{ env('SNOWFLAKE_ACCOUNT') }}\"
  database: \"$OBSERVER_DATABASE\"
  schema_name: \"$OBSERVER_SCHEMA\"
  table_name: \"$OBSERVER_TABLE\"
  username_env_var: SNOWFLAKE_USER
$OBS_AUTH_FIELDS
  warehouse: \"$SNOW_WAREHOUSE\"
  check_interval_seconds: 60
  include_preview_metadata: true"
fi

# ── 13. Optional partitioned heterogeneous chain ───────────────────────
# Python synthetic_data_generator (daily partitioned) -> dataframe_to_snowflake
# (also daily partitioned). Proves cross-engine lineage (Python ⇄ Snowflake)
# AND lets you backfill arbitrary date ranges from the dg dev UI.
if [ "$WANT_HET" = "y" ] || [ "$WANT_HET" = "Y" ]; then
  write_yaml "python_daily_events" "type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: python_daily_events
  schema_type: events
  row_count: 200
  random_state: 42
  partition_type: daily
  partition_start: \"$HET_PARTITION_START\"
  group_name: heterogeneous_ingest"

  HET_AUTH_FIELDS="$(snow_auth_fields_envvar '  ')"
  write_yaml "python_daily_events_to_snowflake" "type: $PKG.components.dataframe_to_snowflake.component.DataframeToSnowflakeComponent
attributes:
  asset_name: python_daily_events_to_snowflake
  upstream_asset_key: python_daily_events
  account_env_var: SNOWFLAKE_ACCOUNT
  user_env_var: SNOWFLAKE_USER
$HET_AUTH_FIELDS
  warehouse: \"$SNOW_WAREHOUSE\"
  database: \"$HET_DATABASE\"
  schema: \"$HET_SCHEMA\"
  table: \"$HET_TABLE\"
  if_exists: append
  partition_type: daily
  partition_start: \"$HET_PARTITION_START\"
  partition_date_column: event_ts
  group_name: heterogeneous_ingest"
fi

# ── 14. Optional freshness check ───────────────────────────────────────
if [ "$WANT_FRESH" = "y" ] || [ "$WANT_FRESH" = "Y" ]; then
  write_yaml "freshness_check_demo" "type: $PKG.components.freshness_check.component.FreshnessPolicyComponent
attributes:
  asset_key: \"$FRESH_ASSET_KEY\"
  policy_type: time_window
  fail_window_hours: $FRESH_FAIL_HOURS
  warn_window_hours: $(echo "$FRESH_FAIL_HOURS / 2" | bc 2>/dev/null || echo "12")"
fi

# ── 15a. Optional snowpark_pipeline ────────────────────────────────────
# DataFrame-API parallel to warehouse_pipeline. Same multi-step shape
# (steps/ref/op:sql/multi-sink) but uses Snowpark's lazy DataFrame
# API and compiles to ONE Snowflake SQL statement per sink server-side.
if [ "$WANT_SNOWPARK" = "y" ] || [ "$WANT_SNOWPARK" = "Y" ]; then
  # snowpark_pipeline accepts an arbitrary `connection` dict that goes
  # straight to Session.builder.configs(). Snowpark resolves env vars
  # via the `<key>_env_var` convention. Plus authenticator + private key.
  SP_AUTH_FIELDS="$(snow_auth_fields_envvar '    ')"
  write_yaml "snowpark_pipeline_demo" "type: $PKG.components.snowpark_pipeline.component.SnowparkPipelineComponent
attributes:
  asset_name: snowpark_pipeline_demo
  connection:
    account_env_var:  SNOWFLAKE_ACCOUNT
    user_env_var:     SNOWFLAKE_USER
$SP_AUTH_FIELDS
    warehouse: \"$SNOW_WAREHOUSE\"
    database:  \"$SNOW_DATABASE\"
    schema:    \"$SNOW_SCHEMA\"
  steps:
    - id: delivered_orders
      source: {kind: table, table: $SP_ORDERS}
      operations:
        - {op: filter, predicate: \"STATUS = 'delivered'\"}

    - id: vip_customers
      source: {kind: table, table: $SP_CUSTOMERS}
      operations:
        - {op: filter, predicate: \"LIFETIME_VALUE > 3000\"}

    - id: enriched
      source: {kind: ref, ref: delivered_orders}
      operations:
        - op: join
          right: {ref: vip_customers}
          on_columns: [CUSTOMER_ID]
          how: inner
        - op: sql
          sql: |
            SELECT *, TOTAL * 0.20 AS PREMIUM_COMMISSION
            FROM self
        - op: group_by
          group_by: [STATE]
          aggregations:
            REVENUE:               {col: TOTAL,              agg: sum}
            TOTAL_PREMIUM_COMM:    {col: PREMIUM_COMMISSION, agg: sum}
            ORDER_COUNT:           {col: ORDER_ID,           agg: count}

  sinks:
    - {from: enriched, kind: table, table: $SP_OUT_SCHEMA.SNOWPARK_PREMIUM_REVENUE, mode: overwrite}
  group_name: snowflake_transforms"
fi

# ── 15b. Optional external_snowflake_table ─────────────────────────────
# Declare-only asset — Dagster sees the table as an upstream / sibling
# but doesn't manage it. Common enterprise pattern: another team owns
# this table; we want it on our lineage graph without taking ownership.
if [ "$WANT_EXTERNAL" = "y" ] || [ "$WANT_EXTERNAL" = "Y" ]; then
  write_yaml "external_table_demo" "type: $PKG.components.external_snowflake_table.component.ExternalSnowflakeTableAsset
attributes:
  asset_key: \"$EXT_KEY\"
  account: \"{{ env('SNOWFLAKE_ACCOUNT') }}\"
  database: \"$EXT_DATABASE\"
  schema_name: \"$EXT_SCHEMA\"
  table_name: \"$EXT_TABLE\"
  group_name: external_tables
  description: \"Externally-managed table — lineage only, Dagster does not write or refresh.\""
fi

# ── 16. Optional dbt project ───────────────────────────────────────────
# Scaffolds a tiny dbt project inside the Dagster project and wires it
# in via Dagster's OFFICIAL dagster-dbt integration. Models build on top
# of RAW.* tables and materialize into the chosen target schema.
if [ "$WANT_DBT" = "y" ] || [ "$WANT_DBT" = "Y" ]; then
  echo ">>> Scaffolding dbt project at dbt/ ..."
  mkdir -p dbt/models/staging dbt/models/marts

  cat > dbt/dbt_project.yml <<DBTPRJ
name: 'snowflake_dagster_dbt'
version: '1.0.0'
config-version: 2
profile: 'snowflake_dagster_dbt'
model-paths: ["models"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]
models:
  snowflake_dagster_dbt:
    staging:
      +materialized: view
      +schema: dbt_staging
    marts:
      +materialized: table
      +schema: $DBT_TARGET_SCHEMA
DBTPRJ

  # dbt-snowflake supports password, keypair (private_key_path), and SSO
  # (authenticator: externalbrowser). Pick the right block based on the
  # auth method the user selected at the top of this script.
  case "$SNOW_AUTH_METHOD" in
    keypair)
      DBT_AUTH_BLOCK='      authenticator:    "jwt"
      private_key_path: "{{ env_var('"'"'SNOWFLAKE_PRIVATE_KEY_FILE'"'"') }}"'
      [ -n "$SNOW_KEY_PWD" ] && DBT_AUTH_BLOCK="$DBT_AUTH_BLOCK
      private_key_passphrase: \"{{ env_var('SNOWFLAKE_PRIVATE_KEY_FILE_PWD') }}\""
      ;;
    sso)
      DBT_AUTH_BLOCK='      authenticator:    "externalbrowser"' ;;
    *)
      DBT_AUTH_BLOCK='      password:  "{{ env_var('"'"'SNOWFLAKE_PASSWORD'"'"') }}"' ;;
  esac
  cat > dbt/profiles.yml <<DBTPROF
snowflake_dagster_dbt:
  target: dev
  outputs:
    dev:
      type: snowflake
      account:   "{{ env_var('SNOWFLAKE_ACCOUNT') }}"
      user:      "{{ env_var('SNOWFLAKE_USER') }}"
$DBT_AUTH_BLOCK
      role:      "{{ env_var('SNOWFLAKE_ROLE', 'SYSADMIN') }}"
      database:  "$DBT_SOURCE_DB"
      warehouse: "$SNOW_WAREHOUSE"
      schema:    "$DBT_TARGET_SCHEMA"
      threads: 4
DBTPROF

  cat > dbt/models/staging/stg_orders.sql <<'DBTSQL'
SELECT
    order_id,
    customer_id,
    order_date,
    category,
    region,
    total,
    status
FROM {{ source('raw', 'orders') }}
WHERE status IN ('paid', 'delivered')
DBTSQL

  cat > dbt/models/staging/stg_customers.sql <<'DBTSQL'
SELECT
    customer_id,
    first_name,
    last_name,
    tier,
    lifetime_value
FROM {{ source('raw', 'customers') }}
WHERE is_active = true
DBTSQL

  cat > dbt/models/marts/customer_revenue.sql <<'DBTSQL'
SELECT
    c.customer_id,
    c.tier,
    c.lifetime_value,
    COUNT(o.order_id)  AS order_count,
    COALESCE(SUM(o.total), 0) AS recent_revenue
FROM {{ ref('stg_customers') }} c
LEFT JOIN {{ ref('stg_orders') }} o ON c.customer_id = o.customer_id
GROUP BY 1, 2, 3
DBTSQL

  cat > dbt/models/staging/sources.yml <<DBTYAML
version: 2
sources:
  - name: raw
    database: $DBT_SOURCE_DB
    schema:   $DBT_SOURCE_SCHEMA
    tables:
      - name: orders
      - name: customers
DBTYAML

  # Parse the dbt project so DbtProjectComponent can load the manifest.
  ( cd dbt && DBT_PROFILES_DIR=. uv run dbt parse --quiet 2>&1 | tail -5 || true )

  # Dagster defs for the dbt project — uses the OFFICIAL dagster-dbt
  # integration. Each model becomes a Dagster asset; sources become
  # external observable assets; the lineage shows RAW → staging → marts.
  write_yaml "dbt_project" "type: dagster_dbt.DbtProjectComponent
attributes:
  project:
    dbt_project_dir: ../../../dbt
    profiles_dir: ../../../dbt
  group_name: dbt_models"
fi

# ── 17. Optional 'define-as-code' DDL showcase ─────────────────────────
# 7 minimal defs.yaml files, one per snowflake_<entity> component.
# Each defines a Dagster-managed sibling to the seed's objects (with DG_
# prefix to avoid collisions). The workspace component's
# exclude_name_pattern is set below so they don't get double-imported.
if [ "$WANT_DDL_SHOWCASE" = "y" ] || [ "$WANT_DDL_SHOWCASE" = "Y" ]; then
  # Shared connection block reused by all 7 — emitted via the auth helper.
  DDL_AUTH_FIELDS="$(snow_auth_fields_direct '  ')"
  DDL_CONN_HEADER="\
  account: \"{{ env('SNOWFLAKE_ACCOUNT') }}\"
  user:    \"{{ env('SNOWFLAKE_USER') }}\"
$DDL_AUTH_FIELDS
  warehouse: \"$SNOW_WAREHOUSE\"
  database: \"$SNOW_DATABASE\"
  schema_name: \"$DDL_TARGET_SCHEMA\""
  [ -n "$SNOW_ROLE" ] && DDL_CONN_HEADER="$DDL_CONN_HEADER
  role: \"$SNOW_ROLE\""

  write_yaml "dg_task" "type: $PKG.components.snowflake_task.component.SnowflakeTaskComponent
attributes:
  asset_name: dg_task_daily_rollup
  task_name: DG_DAILY_ORDERS_ROLLUP
$DDL_CONN_HEADER
  schedule: \"USING CRON 0 4 * * * UTC\"
  sql: |
    SELECT 1
  on_materialize: create_only
  group_name: snowflake_ddl_showcase"

  write_yaml "dg_dynamic_table" "type: $PKG.components.snowflake_dynamic_table.component.SnowflakeDynamicTableComponent
attributes:
  asset_name: dg_dt_paid_orders
  dt_name: DG_PAID_ORDERS_DT
$DDL_CONN_HEADER
  target_lag: \"15 minutes\"
  initialize: ON_SCHEDULE
  refresh_mode: AUTO
  sql: |
    SELECT * FROM $SNOW_DATABASE.$SNOW_SCHEMA.ORDERS WHERE STATUS IN ('paid', 'delivered')
  group_name: snowflake_ddl_showcase"

  write_yaml "dg_stream" "type: $PKG.components.snowflake_stream.component.SnowflakeStreamComponent
attributes:
  asset_name: dg_stream_orders
  stream_name: DG_ORDERS_STREAM
  on_table: $SNOW_DATABASE.$SNOW_SCHEMA.ORDERS
$DDL_CONN_HEADER
  append_only: false
  group_name: snowflake_ddl_showcase"

  write_yaml "dg_stored_procedure" "type: $PKG.components.snowflake_stored_procedure.component.SnowflakeStoredProcedureComponent
attributes:
  asset_name: dg_sp_count_orders
  procedure_name: DG_SP_COUNT_ORDERS
$DDL_CONN_HEADER
  language: SQL
  returns: NUMBER
  body: |
    \$\$
    BEGIN
      RETURN (SELECT COUNT(*) FROM $SNOW_DATABASE.$SNOW_SCHEMA.ORDERS);
    END;
    \$\$
  on_materialize: call
  group_name: snowflake_ddl_showcase"

  write_yaml "dg_snowpipe" "type: $PKG.components.snowflake_snowpipe.component.SnowflakeSnowpipeComponent
attributes:
  asset_name: dg_pipe_orders
  pipe_name: DG_ORDERS_PIPE
$DDL_CONN_HEADER
  auto_ingest: false
  copy_statement: |
    COPY INTO $SNOW_DATABASE.$SNOW_SCHEMA.ORDERS_INGESTED
    FROM @$SNOW_DATABASE.$SNOW_SCHEMA.INTERNAL_STAGE
    FILE_FORMAT = (TYPE = CSV SKIP_HEADER = 1)
    ON_ERROR = 'CONTINUE'
  on_materialize: create_only
  group_name: snowflake_ddl_showcase"

  write_yaml "dg_alert" "type: $PKG.components.snowflake_alert.component.SnowflakeAlertComponent
attributes:
  asset_name: dg_alert_high_revenue
  alert_name: DG_HIGH_REVENUE_DAY
$DDL_CONN_HEADER
  schedule: \"60 minute\"
  condition_sql: |
    SELECT 1 FROM $SNOW_DATABASE.$SNOW_SCHEMA.ORDERS WHERE TOTAL > 1000 LIMIT 1
  action_sql: |
    SELECT SYSTEM\$SET_RETURN_VALUE('high_revenue_detected')
  on_materialize: create_only
  group_name: snowflake_ddl_showcase"

  write_yaml "dg_materialized_view" "type: $PKG.components.snowflake_materialized_view.component.SnowflakeMaterializedViewComponent
attributes:
  asset_name: dg_mv_customer_ltv
  mv_name: DG_CUSTOMER_LTV_MV
$DDL_CONN_HEADER
  sql: |
    SELECT CUSTOMER_ID, SUM(TOTAL) AS LIFETIME_REVENUE
    FROM $SNOW_DATABASE.$SNOW_SCHEMA.ORDERS
    WHERE STATUS IN ('paid', 'delivered')
    GROUP BY CUSTOMER_ID
  group_name: snowflake_ddl_showcase"
fi

# ── 12. .env.demo ──────────────────────────────────────────────────────
# Emit only the env vars relevant to the chosen auth method, plus the
# always-needed account/user/warehouse/database/schema.
SNOWFLAKE_URL_VAL="$(build_snowflake_url)"
{
  echo "# Snowflake credentials — gitignored. Source before running:"
  echo "#   source .env.demo"
  echo "export SNOWFLAKE_ACCOUNT=\"$SNOW_ACCOUNT\""
  echo "export SNOWFLAKE_USER=\"$SNOW_USER\""
  echo "export SNOWFLAKE_WAREHOUSE=\"$SNOW_WAREHOUSE\""
  echo "export SNOWFLAKE_DATABASE=\"$SNOW_DATABASE\""
  echo "export SNOWFLAKE_SCHEMA=\"$SNOW_SCHEMA\""
  [ -n "$SNOW_ROLE" ] && echo "export SNOWFLAKE_ROLE=\"$SNOW_ROLE\""
  case "$SNOW_AUTH_METHOD" in
    keypair)
      echo "# Keypair auth (headless — works for daemon + dg dev + Dagster+)"
      echo "export SNOWFLAKE_PRIVATE_KEY_FILE=\"$SNOW_KEY_FILE\""
      [ -n "$SNOW_KEY_PWD" ] && echo "export SNOWFLAKE_PRIVATE_KEY_FILE_PWD=\"$SNOW_KEY_PWD\""
      ;;
    sso)
      echo "# SSO auth (externalbrowser — laptop dg dev only; daemon won't work)"
      echo "# No password / key env vars needed."
      ;;
    pat)
      echo "# Programmatic Access Token auth (headless; works for daemon + dg dev)"
      echo "export SNOWFLAKE_PAT=\"$SNOW_PAT\""
      ;;
    password)
      echo "export SNOWFLAKE_PASSWORD=\"$SNOW_PASS\""
      ;;
  esac
  echo "# warehouse_pipeline reads this single URL form via database_url_env_var:"
  echo "export SNOWFLAKE_URL=\"$SNOWFLAKE_URL_VAL\""
} > .env.demo
chmod 600 .env.demo
grep -q '^\.env\.demo' .gitignore 2>/dev/null || echo ".env.demo" >> .gitignore

PARTIAL_PROJECT_PATH=""

# ── 13. Trailing guide ─────────────────────────────────────────────────
cat <<MSG

>>> Setup complete.

Generated:
  src/$PKG/defs/snowflake_workspace/defs.yaml
$([ "$WANT_PIPELINE" = "y" ] || [ "$WANT_PIPELINE" = "Y" ] && echo "  src/$PKG/defs/regional_top_paid_pipeline/defs.yaml")
$([ "$WANT_CORTEX"   = "y" ] || [ "$WANT_CORTEX"   = "Y" ] && echo "  src/$PKG/defs/cortex_demo/defs.yaml")
  .env.demo  (mode 600 — contains your password; gitignored)

Run it:
    cd $PROJECT
    source .env.demo
    uv run dg check defs        # validate
    uv run dg dev               # opens UI at http://localhost:3000

What you'll see in dg dev:
  • Every imported Snowflake entity surfaces as a Dagster asset
    (tasks, dynamic tables, stored procs, streams, … per your picks)
  • Clicking 'Materialize' triggers the actual Snowflake TASK / refreshes
    the DYNAMIC TABLE / calls the stored proc / etc.
$([ "$WANT_PIPELINE" = "y" ] || [ "$WANT_PIPELINE" = "Y" ] && cat <<P
  • regional_top_paid_pipeline runs multi-step transforms ON TOP of
    your existing tables — join + op:sql commission + group_by — all
    compiled to ONE Snowflake SQL plan per sink (STATE_ENRICHED +
    TOP_3_STATES under $PIPE_OUT_SCHEMA).
P
)
$([ "$WANT_CORTEX" = "y" ] || [ "$WANT_CORTEX" = "Y" ] && echo "  • cortex_demo calls Snowflake Cortex ($CORTEX_MODE mode) — LLM as a first-class asset")
$([ "$WANT_OBSERVER" = "y" ] || [ "$WANT_OBSERVER" = "Y" ] && echo "  • row_count_observer watches $OBSERVER_DATABASE.$OBSERVER_SCHEMA.$OBSERVER_TABLE for changes (every 60s)")
$([ "$WANT_AUTOCOND" = "y" ] || [ "$WANT_AUTOCOND" = "Y" ] && echo "  • regional_top_paid_pipeline runs with AutomationCondition.eager() — fires the moment any upstream changes")
$(if [ "$WANT_HET" = "y" ] || [ "$WANT_HET" = "Y" ]; then cat <<P
  • python_daily_events → python_daily_events_to_snowflake (daily-partitioned).
    Heterogeneous lineage: Python on the left, Snowflake on the right.
    Backfill 30 days with:
      uv run dg launch --assets python_daily_events_to_snowflake --partition-range $HET_PARTITION_START...\$(date -u +%Y-%m-%d) --max-concurrent 5
P
fi)
$([ "$WANT_FRESH" = "y" ] || [ "$WANT_FRESH" = "Y" ] && echo "  • freshness_check_demo fails if $FRESH_ASSET_KEY hasn't been updated in $FRESH_FAIL_HOURS hours")
$(if [ "$WANT_SNOWPARK" = "y" ] || [ "$WANT_SNOWPARK" = "Y" ]; then cat <<P
  • snowpark_pipeline_demo (DataFrame API parallel to warehouse_pipeline) —
    same multi-step shape, but uses Snowpark's lazy DataFrame API.
    Writes $SP_OUT_SCHEMA.SNOWPARK_PREMIUM_REVENUE. Side-by-side with
    regional_top_paid_pipeline this shows Dagster works equally well
    with either Snowflake compute paradigm (SQL CTAS vs DataFrame).
P
fi)
$([ "$WANT_EXTERNAL" = "y" ] || [ "$WANT_EXTERNAL" = "Y" ] && echo "  • external_table_demo declares $EXT_KEY — lineage only, Dagster doesn't manage the table")
$(if [ "$WANT_DBT" = "y" ] || [ "$WANT_DBT" = "Y" ]; then cat <<P
  • dbt project at ./dbt — 2 staging models + 1 mart (customer_revenue).
    Imported via Dagster's official dagster-dbt integration; each model
    is a Dagster asset with lineage from RAW.* → staging → marts.
P
fi)

Cross-entity dep wiring:
$(if [ "${#DEP_ENTITIES[@]}" -gt 0 ]; then
  echo "  You declared ${#DEP_ENTITIES[@]} cross-entity dep(s) via assets_by_name."
  echo "  Edit src/$PKG/defs/snowflake_workspace/defs.yaml to add more later."
else
  echo "  None declared — edit src/$PKG/defs/snowflake_workspace/defs.yaml to add deps:"
  echo "      assets_by_name:"
  echo "        MY_TASK_NAME:"
  echo "          deps: [raw/orders, raw/customers]"
fi)

Switching auth (password → PAT / key-pair / SSO):
  The workspace component supports any field's '<field>_env_var' alternate
  AND password / authenticator / private_key / token. Edit the workspace
  defs.yaml — the official Snowflake connector docs cover each combo.

Other Snowflake components in the registry you can layer in:
  • dataframe_to_snowflake / dataframe_to_snowflake_bulk — write data IN
  • external_snowflake_table                            — declare-only refs
  • snowflake_io_manager / _polars_io_manager / _pyspark_io_manager
  • snowflake_table_observation_sensor                  — watch + react
  • snowflake_access_history_ingestion                  — audit trail asset
  • snowpark_pipeline                                   — Snowpark DataFrame multi-step

Add any of them with:
    uvx --from dagster-community-components-cli dagster-component add <name>
MSG

rm -f "$INV_OUT"
