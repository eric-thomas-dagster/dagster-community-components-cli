#!/usr/bin/env bash
# bootstrap.sh
#
# Scaffold a Dagster project that orchestrates the demo environment created
# by seed.sh. Reads .env, creates a fresh project via `uvx create-dagster`,
# installs the community components needed for the demo, and writes
# defs.yaml files with dependencies wired so the lineage forms ONE
# connected end-to-end DAG.
#
# Default is COMPREHENSIVE — every Snowflake capability the demo can show
# is on. Customers see the full breadth: Python ingest → workspace-imported
# Snowflake-native entities (tasks / dynamic tables / streams / stored
# procs / MVs / stages / snowpipes / alerts) → Dagster-managed warehouse
# transforms → Cortex AI → Snowpark Python in Snowflake → Iceberg sink →
# freshness + observability.
#
# Result is ~30+ assets in a connected DAG. Use --lean to strip back to
# the minimum spine (workspace + Python ingest + Iceberg + freshness) when
# you want a quick check.
#
# Default-on capabilities (toggle off with the matching --no-* flag):
#   workspace          (always — imports all 21 STAGING objects)
#   python ingest      (always — synthetic_data_generator + dataframe_to_snowflake)
#   iceberg            (auto-detected from .env)
#   freshness          (always)
#   --no-cortex              snowflake_cortex_asset off AI.CUSTOMER_FEEDBACK
#   --no-snowpark            snowpark_pipeline (Python in Snowflake)
#   --no-warehouse-pipeline  Dagster-managed multi-step SQL
#   --no-observer            snowflake_table_observation_sensor
#
# Off-by-default (needs more setup):
#   --with-dbt               dbt_project_component (requires dbt-snowflake config beyond .env)
#
# Usage:
#   ./bootstrap.sh                          # comprehensive demo, reads ./.env
#   ./bootstrap.sh --lean                   # minimal connected spine only
#   ./bootstrap.sh --no-cortex --no-snowpark   # comprehensive minus AI bits
#   ./bootstrap.sh --name my-snow-demo      # name the project (default: prompt)
#   ./bootstrap.sh --env-file ./.env.demo   # use a specific env file
#   ./bootstrap.sh --help                   # this message

set -eo pipefail

# Short-circuit --help before the TTY guard.
for arg in "$@"; do
  case "$arg" in
    -h|--help) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

if [ ! -t 0 ]; then
  cat <<'NIG'
════════════════════════════════════════════════════════════════════
  bootstrap.sh is interactive — run from a terminal, not curl|bash.

    curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/bootstrap.sh -o bootstrap.sh
    chmod +x bootstrap.sh
    ./bootstrap.sh
════════════════════════════════════════════════════════════════════
NIG
  exit 1
fi

# ─── Flag parsing ─────────────────────────────────────────────────────────
# DEFAULT IS COMPREHENSIVE: every capability the demo can show is on, so
# customers see the full breadth of "Dagster orchestrating Snowflake."
# Use --lean to strip back to just workspace + Python ingest + Iceberg
# (the connected backbone), or --no-<capability> to turn off individual
# components.
PROJECT_NAME=""
ENV_FILE="${BOOTSTRAP_ENV_FILE:-./.env}"
WITH_CORTEX=true              # snowflake_cortex_asset on AI.CUSTOMER_FEEDBACK
WITH_SNOWPARK=true            # snowpark_pipeline (Python in Snowflake)
WITH_WAREHOUSE_PIPELINE=true  # warehouse_pipeline (Dagster-managed multi-step SQL)
WITH_OBSERVER=true            # snowflake_table_observation_sensor
WITH_DBT=false                # dbt: off by default (requires dbt-snowflake config beyond .env)
LEAN=false

while [ $# -gt 0 ]; do
  case "$1" in
    --name)                     PROJECT_NAME="$2"; shift 2 ;;
    --name=*)                   PROJECT_NAME="${1#*=}"; shift ;;
    --env-file)                 ENV_FILE="$2"; shift 2 ;;
    --env-file=*)               ENV_FILE="${1#*=}"; shift ;;
    --with-cortex)              WITH_CORTEX=true; shift ;;
    --no-cortex)                WITH_CORTEX=false; shift ;;
    --with-snowpark)            WITH_SNOWPARK=true; shift ;;
    --no-snowpark)              WITH_SNOWPARK=false; shift ;;
    --with-dbt)                 WITH_DBT=true; shift ;;
    --no-dbt)                   WITH_DBT=false; shift ;;
    --with-warehouse-pipeline)  WITH_WAREHOUSE_PIPELINE=true; shift ;;
    --no-warehouse-pipeline)    WITH_WAREHOUSE_PIPELINE=false; shift ;;
    --with-observer)            WITH_OBSERVER=true; shift ;;
    --no-observer)              WITH_OBSERVER=false; shift ;;
    --lean)                     LEAN=true; shift ;;
    --full)                     WITH_CORTEX=true; WITH_SNOWPARK=true; WITH_DBT=true; \
                                WITH_WAREHOUSE_PIPELINE=true; WITH_OBSERVER=true; shift ;;
    -h|--help) sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

# --lean trumps everything: drop every opt-in down to the bare backbone.
if [ "$LEAN" = "true" ]; then
  WITH_CORTEX=false; WITH_SNOWPARK=false; WITH_DBT=false
  WITH_WAREHOUSE_PIPELINE=false; WITH_OBSERVER=false
fi

# ─── uv guard ─────────────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  echo "uv (Python package manager) is required and not installed."
  read -r -p "Install uv now? [Y/n] " ans
  case "${ans:-y}" in
    y|Y|yes) curl -fsSL https://astral.sh/uv/install.sh | sh
             . "$HOME/.local/bin/env" 2>/dev/null || true
             export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" ;;
    *)       echo "Aborted."; exit 1 ;;
  esac
fi

# ─── Read .env ────────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════════════"
echo "  Dagster Snowflake demo — project bootstrap"
echo "═══════════════════════════════════════════════════════════════════════"

if [ ! -f "$ENV_FILE" ]; then
  echo "  ✗ $ENV_FILE not found."
  echo "    Run ./seed.sh first to provision Snowflake + write .env."
  echo "    Or specify a different file with --env-file PATH."
  exit 1
fi

echo "  Loading $ENV_FILE ..."
# shellcheck disable=SC1090
set -a
. "$ENV_FILE"
set +a

REQUIRED_VARS="SNOWFLAKE_ACCOUNT SNOWFLAKE_USER SNOWFLAKE_WAREHOUSE SNOWFLAKE_ROLE SNOWFLAKE_DATABASE SNOWFLAKE_SCHEMA SNOWFLAKE_AUTH_METHOD"
MISSING=""
for v in $REQUIRED_VARS; do
  eval "val=\${$v:-}"
  [ -z "$val" ] && MISSING="$MISSING $v"
done
if [ -n "$MISSING" ]; then
  echo "  ✗ Required env vars missing from $ENV_FILE:$MISSING"
  echo "    Re-run ./seed.sh to regenerate."
  exit 1
fi

# Auth-method-specific extras. Any secret we prompt for here is also saved
# to .env.secrets (mode 600, sibling of .env) so `dg dev` can pick it up
# in a fresh shell. .env.secrets is added to .gitignore below.
SECRETS_TO_PERSIST=""

case "$SNOWFLAKE_AUTH_METHOD" in
  keypair)
    [ -z "${SNOWFLAKE_PRIVATE_KEY_FILE:-}" ] && {
      echo "  ✗ SNOWFLAKE_AUTH_METHOD=keypair but SNOWFLAKE_PRIVATE_KEY_FILE not set in $ENV_FILE."; exit 1; }
    [ -f "$SNOWFLAKE_PRIVATE_KEY_FILE" ] || {
      echo "  ✗ Key file not found: $SNOWFLAKE_PRIVATE_KEY_FILE"; exit 1; }
    # Encrypted key? Prompt for passphrase if not in env.
    if [ -z "${SNOWFLAKE_PRIVATE_KEY_FILE_PWD:-}" ] && head -1 "$SNOWFLAKE_PRIVATE_KEY_FILE" | grep -qi 'ENCRYPTED'; then
      read -r -s -p "  Private key passphrase (hidden): " SNOWFLAKE_PRIVATE_KEY_FILE_PWD; echo
      export SNOWFLAKE_PRIVATE_KEY_FILE_PWD
    fi
    # Always persist if we have one, prompted-here or pre-existing-in-env.
    [ -n "${SNOWFLAKE_PRIVATE_KEY_FILE_PWD:-}" ] && SECRETS_TO_PERSIST="$SECRETS_TO_PERSIST SNOWFLAKE_PRIVATE_KEY_FILE_PWD" ;;
  password)
    if [ -z "${SNOWFLAKE_PASSWORD:-}" ]; then
      echo "  ⚠ SNOWFLAKE_AUTH_METHOD=password but SNOWFLAKE_PASSWORD not in env."
      read -r -s -p "  Enter Snowflake password (hidden): " SNOWFLAKE_PASSWORD; echo
      export SNOWFLAKE_PASSWORD
    fi
    SECRETS_TO_PERSIST="$SECRETS_TO_PERSIST SNOWFLAKE_PASSWORD" ;;
  pat)
    if [ -z "${SNOWFLAKE_PAT:-}" ]; then
      echo "  ⚠ SNOWFLAKE_AUTH_METHOD=pat but SNOWFLAKE_PAT not in env."
      read -r -s -p "  Enter PAT (hidden): " SNOWFLAKE_PAT; echo
      export SNOWFLAKE_PAT
    fi
    SECRETS_TO_PERSIST="$SECRETS_TO_PERSIST SNOWFLAKE_PAT" ;;
  password_mfa)
    if [ -z "${SNOWFLAKE_PASSWORD:-}" ]; then
      read -r -s -p "  Enter Snowflake password (hidden): " SNOWFLAKE_PASSWORD; echo
      export SNOWFLAKE_PASSWORD
    fi
    SECRETS_TO_PERSIST="$SECRETS_TO_PERSIST SNOWFLAKE_PASSWORD" ;;
  sso) : ;;   # nothing to load
  *) echo "  ✗ Unknown auth method: $SNOWFLAKE_AUTH_METHOD"; exit 1 ;;
esac

echo "  ✓ Loaded config:"
echo "      account=$SNOWFLAKE_ACCOUNT  user=$SNOWFLAKE_USER  role=$SNOWFLAKE_ROLE"
echo "      database=$SNOWFLAKE_DATABASE  schema=$SNOWFLAKE_SCHEMA  warehouse=$SNOWFLAKE_WAREHOUSE"
echo "      auth=$SNOWFLAKE_AUTH_METHOD"
[ -n "${ICEBERG_EXTERNAL_VOLUME:-}" ] && echo "      iceberg volume=$ICEBERG_EXTERNAL_VOLUME"

# ─── Connection sanity check ──────────────────────────────────────────────
echo
echo ">>> Verifying Snowflake connection ..."
SF_AUTH_METHOD="$SNOWFLAKE_AUTH_METHOD" \
SF_ACCOUNT="$SNOWFLAKE_ACCOUNT" SF_USER="$SNOWFLAKE_USER" \
SF_WAREHOUSE="$SNOWFLAKE_WAREHOUSE" SF_ROLE="$SNOWFLAKE_ROLE" \
SF_PASS="${SNOWFLAKE_PASSWORD:-}" \
SF_KEY_FILE="${SNOWFLAKE_PRIVATE_KEY_FILE:-}" \
SF_KEY_PWD="${SNOWFLAKE_PRIVATE_KEY_FILE_PWD:-}" \
SF_PAT="${SNOWFLAKE_PAT:-}" \
SF_DB="$SNOWFLAKE_DATABASE" SF_SCHEMA="$SNOWFLAKE_SCHEMA" \
uv run --quiet --with 'snowflake-connector-python[secure-local-storage]' --no-project python - <<'PYEOF'
import os, sys
import snowflake.connector as sc

ck = dict(account=os.environ['SF_ACCOUNT'], user=os.environ['SF_USER'],
          warehouse=os.environ['SF_WAREHOUSE'], role=os.environ.get('SF_ROLE') or None)
auth = os.environ['SF_AUTH_METHOD']
if   auth == 'keypair':      ck.update(authenticator='SNOWFLAKE_JWT', private_key_file=os.environ['SF_KEY_FILE'])
elif auth == 'sso':          ck.update(authenticator='externalbrowser')
elif auth == 'pat':          ck.update(authenticator='PROGRAMMATIC_ACCESS_TOKEN', token=os.environ['SF_PAT'])
elif auth == 'password_mfa': ck.update(authenticator='username_password_mfa', password=os.environ.get('SF_PASS',''), client_request_mfa_token=True)
else:                        ck['password'] = os.environ.get('SF_PASS','')
if auth == 'keypair' and os.environ.get('SF_KEY_PWD'):
    ck['private_key_file_pwd'] = os.environ['SF_KEY_PWD']

try:
    conn = sc.connect(**ck); cur = conn.cursor()
    cur.execute("SELECT CURRENT_VERSION()")
    print(f"  ✓ Connected. Snowflake version: {cur.fetchone()[0]}")
    cur.execute(f"USE DATABASE {os.environ['SF_DB']}")
    cur.execute(f"USE SCHEMA {os.environ['SF_SCHEMA']}")
    cur.execute("SHOW DYNAMIC TABLES")
    dts = [r[1] for r in cur.fetchall()]
    cur.execute("SHOW TASKS")
    tasks = [r[1] for r in cur.fetchall()]
    print(f"  ✓ Found in {os.environ['SF_DB']}.{os.environ['SF_SCHEMA']}: dynamic tables={dts}, tasks={tasks}")
    if "EVENTS_CLEANED_DT" not in dts or "DAILY_ORDERS_ROLLUP" not in tasks:
        print("  ⚠ Expected seed objects (EVENTS_CLEANED_DT, DAILY_ORDERS_ROLLUP) NOT found.")
        print("    Run ./seed.sh first, then re-run this script.")
        sys.exit(2)
    cur.close(); conn.close()
except Exception as e:
    print(f"  ✗ Connection or schema check failed: {e}")
    sys.exit(1)
PYEOF
[ $? -ne 0 ] && exit 1

# ─── Project name ─────────────────────────────────────────────────────────
if [ -z "$PROJECT_NAME" ]; then
  echo
  read -r -p "Project name [snowflake-demo]: " PROJECT_NAME
  PROJECT_NAME="${PROJECT_NAME:-snowflake-demo}"
fi

# Validate: lowercase, alphanumeric + hyphens. Convert if needed.
PROJECT_NAME_RAW="$PROJECT_NAME"
PROJECT_NAME=$(echo "$PROJECT_NAME" | tr '[:upper:]' '[:lower:]' | tr ' _' '--' | sed 's/[^a-z0-9-]//g')
if [ "$PROJECT_NAME" != "$PROJECT_NAME_RAW" ]; then
  echo "  Normalized project name: $PROJECT_NAME_RAW → $PROJECT_NAME"
fi
if [ -e "$PROJECT_NAME" ]; then
  echo "  ✗ Directory '$PROJECT_NAME' already exists. Pick a different --name or delete it."
  exit 1
fi

# Python package name = project with hyphens → underscores
PKG=$(echo "$PROJECT_NAME" | tr '-' '_')

echo
echo "─────────────────────────────────────────────────────────────────────"
echo "  Project:  $PROJECT_NAME    (Python package: $PKG)"
echo "  Lean default:  python_daily_events → events_to_snowflake → workspace imports"
[ -n "${ICEBERG_EXTERNAL_VOLUME:-}" ] && echo "                + iceberg_daily_revenue (volume: $ICEBERG_EXTERNAL_VOLUME)"
echo "  Opt-ins on:"
[ "$WITH_CORTEX" = "true" ]             && echo "    • snowflake_cortex_asset (AI.CUSTOMER_FEEDBACK)"
[ "$WITH_SNOWPARK" = "true" ]           && echo "    • snowpark_pipeline"
[ "$WITH_DBT" = "true" ]                && echo "    • dbt_project_component"
[ "$WITH_WAREHOUSE_PIPELINE" = "true" ] && echo "    • warehouse_pipeline (multi-step SQL)"
[ "$WITH_OBSERVER" = "true" ]           && echo "    • snowflake_table_observation_sensor"
echo "─────────────────────────────────────────────────────────────────────"
read -r -p "Continue? [Y/n] " GO
case "${GO:-y}" in y|Y|yes) ;; *) echo "Aborted."; exit 0 ;; esac

# ─── Create project ───────────────────────────────────────────────────────
echo
echo ">>> Creating Dagster project '$PROJECT_NAME' ..."
uvx create-dagster project "$PROJECT_NAME" --uv-sync >/dev/null 2>&1 || {
  echo "  ⚠ uvx create-dagster failed — falling back to verbose output for diagnosis:"
  uvx create-dagster project "$PROJECT_NAME" --uv-sync
  exit 1
}
echo "  ✓ Project scaffolded at ./$PROJECT_NAME/"

cd "$PROJECT_NAME"

# Symlink the .env so dg dev picks it up automatically.
ln -sf "../$(basename "$ENV_FILE")" .env
echo "  ✓ Symlinked .env → ../$(basename "$ENV_FILE")"

# Write .env.secrets for any secrets we prompted for. dg dev auto-loads
# .env AND .env.secrets if both exist. .env.secrets is gitignored — keep
# it that way; never commit it.
if [ -n "$SECRETS_TO_PERSIST" ]; then
  {
    echo "# Generated by bootstrap.sh on $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    echo "# This file holds Snowflake secrets that .env deliberately omits."
    echo "# DO NOT COMMIT — it's in .gitignore."
    for var in $SECRETS_TO_PERSIST; do
      eval "val=\${$var}"
      printf '%s=%s\n' "$var" "$val"
    done
  } > .env.secrets
  chmod 600 .env.secrets
  # Ensure .env.secrets stays out of git, regardless of what create-dagster scaffolded.
  if ! grep -qE '^\.env\.secrets$' .gitignore 2>/dev/null; then
    echo ".env.secrets" >> .gitignore
  fi
  echo "  ✓ Saved secret(s) to .env.secrets (mode 600, gitignored):$(echo "$SECRETS_TO_PERSIST" | tr ' ' '\n' | sed 's/^/        - /')"
fi

# ─── Overwrite definitions.py to wire in the automation-condition applicator ─
# The default scaffold just calls load_from_defs_folder. The applicator
# component's docstring says: "Without this wiring, the component is a no-op
# — Dagster components can't post-process other components' output via
# build_defs alone." So we scan the defs/ tree for any applicator yamls and
# pipe their rules through apply_rules() against the built Definitions.
echo
echo ">>> Wiring automation-condition applicator into definitions.py ..."
cat > "src/$PKG/definitions.py" <<'PYEOF'
import logging
import os
from pathlib import Path

import yaml
from dagster import definitions, load_from_defs_folder

# Import path is set at write time by bootstrap.sh — see PKG_IMPORT below.
from PKG_IMPORT.components.automation_condition_applicator.component import apply_rules

_logger = logging.getLogger(__name__)


# Secret env vars referenced via `{{ env('NAME') }}` in component YAMLs.
# When unset, Dagster's jinja resolver throws at load time and the entire
# code location fails to start — booth-hostile UX. Set safe empty-string
# defaults BEFORE load_from_defs_folder so YAML resolves and the project
# loads with a fully-rendered UI; runtime materializations will then fail
# with proper Snowflake connection errors instead of a startup crash.
#
# How to actually set these for real runs:
#   - Quickest (current shell):   `export SNOWFLAKE_PAT=<token>`
#   - Persistent (gitignored):    add to .env.secrets in the repo root,
#                                 which dg dev auto-loads alongside .env.
_SECRET_ENV_VARS = ("SNOWFLAKE_PAT", "SNOWFLAKE_PASSWORD")
_missing = []
for _var in _SECRET_ENV_VARS:
    if not os.environ.get(_var):
        os.environ.setdefault(_var, "")
        _missing.append(_var)

if _missing:
    _logger.warning(
        "⚠ Snowflake secret(s) not set: %s. The project will LOAD (so the "
        "UI renders), but materializations against Snowflake will fail. "
        "Set them via `export %s=<value>` or add to .env.secrets.",
        ", ".join(_missing),
        _missing[0],
    )

# warehouse_pipeline wants a single SQLAlchemy connection string
# (SNOWFLAKE_URL) — seed.sh only writes the constituent parts. Build the
# URL from those parts when SNOWFLAKE_URL isn't already set, so we don't
# duplicate config in .env.secrets.
if not os.environ.get("SNOWFLAKE_URL"):
    _pat = os.environ.get("SNOWFLAKE_PAT")
    _account = os.environ.get("SNOWFLAKE_ACCOUNT")
    _user = os.environ.get("SNOWFLAKE_USER")
    _db = os.environ.get("SNOWFLAKE_DATABASE")
    _wh = os.environ.get("SNOWFLAKE_WAREHOUSE")
    _role = os.environ.get("SNOWFLAKE_ROLE")
    if _pat and _account and _user and _db:
        from urllib.parse import quote_plus
        # snowflake-sqlalchemy's authenticator=oauth expects a true OAuth
        # bearer token (external IdP), NOT a Snowflake PAT. PATs are
        # authenticated by passing them as the password — Snowflake
        # recognizes the shape and switches to PAT auth automatically.
        _qs_parts = []
        if _wh:
            _qs_parts.append(f"warehouse={quote_plus(_wh)}")
        if _role:
            _qs_parts.append(f"role={quote_plus(_role)}")
        _qs = "&".join(_qs_parts)
        os.environ["SNOWFLAKE_URL"] = (
            f"snowflake://{quote_plus(_user)}:{quote_plus(_pat)}"
            f"@{quote_plus(_account)}/{quote_plus(_db)}/"
            + (f"?{_qs}" if _qs else "")
        )
    else:
        _logger.warning(
            "⚠ SNOWFLAKE_URL is not set and not all parts available to "
            "build it (need SNOWFLAKE_PAT + ACCOUNT + USER + DATABASE). "
            "warehouse_pipeline assets will fail at materialize time."
        )


@definitions
def defs():
    here = Path(__file__).parent
    base = load_from_defs_folder(path_within_project=here)

    # The AutomationConditionApplicatorComponent's defs.yaml is a config
    # input — the component itself can't post-process other components'
    # output from inside its own build_defs. Scan the defs/ tree for any
    # applicator yamls and feed their rules through apply_rules() against
    # the already-built Definitions.
    for defs_yaml in here.glob("defs/**/defs.yaml"):
        try:
            cfg = yaml.safe_load(defs_yaml.read_text()) or {}
        except Exception:
            continue
        if not cfg.get("type", "").endswith("AutomationConditionApplicatorComponent"):
            continue
        attrs = cfg.get("attributes", {}) or {}
        base = apply_rules(
            base,
            attrs.get("rules", []) or [],
            preserve_existing=attrs.get("preserve_existing", True),
            precedence=attrs.get("precedence", "first_match"),
        )
    return base
PYEOF
# Substitute the actual package name in the import line.
sed -i.bak "s/PKG_IMPORT/$PKG/" "src/$PKG/definitions.py" && rm -f "src/$PKG/definitions.py.bak"
echo "  ✓ definitions.py wired up"

# ─── Install community components ─────────────────────────────────────────
CLI="uvx --from dagster-community-components-cli dagster-component"

echo
echo ">>> Installing community components ..."

# Always install workspace + Python ingest stack
COMPONENTS_TO_INSTALL="snowflake_workspace synthetic_data_generator dataframe_to_snowflake freshness_check"

# Iceberg if available
[ -n "${ICEBERG_EXTERNAL_VOLUME:-}" ] && COMPONENTS_TO_INSTALL="$COMPONENTS_TO_INSTALL snowflake_iceberg_table"

# Opt-ins
[ "$WITH_CORTEX" = "true" ]             && COMPONENTS_TO_INSTALL="$COMPONENTS_TO_INSTALL snowflake_cortex_asset"
[ "$WITH_SNOWPARK" = "true" ]           && COMPONENTS_TO_INSTALL="$COMPONENTS_TO_INSTALL snowpark_pipeline"
[ "$WITH_WAREHOUSE_PIPELINE" = "true" ] && COMPONENTS_TO_INSTALL="$COMPONENTS_TO_INSTALL warehouse_pipeline"
[ "$WITH_OBSERVER" = "true" ]           && COMPONENTS_TO_INSTALL="$COMPONENTS_TO_INSTALL snowflake_table_observation_sensor"

# S3 sink for the Snowpipe demo — only useful when seed.sh wired up the S3
# staging bucket + external stage + auto-ingest pipe. Without the bucket,
# this component is installed-but-unused.
[ -n "${S3_STAGING_BUCKET:-}" ]         && COMPONENTS_TO_INSTALL="$COMPONENTS_TO_INSTALL dataframe_to_s3"

# Cron schedules — drive the Python generators automatically (daily for
# orders/events, hourly for the Snowpipe path). Always installed; schedules
# without time-based partitions still work via the legacy ScheduleDefinition
# branch.
COMPONENTS_TO_INSTALL="$COMPONENTS_TO_INSTALL cron_schedule"

# Automation conditions — applies AutomationCondition.eager() to the
# Dagster-managed downstream + Snowflake tasks so sensor-emitted
# materializations propagate through the DAG without manual clicks.
COMPONENTS_TO_INSTALL="$COMPONENTS_TO_INSTALL automation_condition_applicator"

for c in $COMPONENTS_TO_INSTALL; do
  echo "  · $c"
  $CLI add "$c" --auto-install >/dev/null 2>&1 || {
    echo "    ⚠ add $c failed — re-running with verbose output:"
    $CLI add "$c" --auto-install
    exit 1
  }
  # `dg add` scaffolds an empty defs.yaml; we'll overwrite below.
  rm -rf "src/$PKG/defs/$c"
done

# dbt component is from dagster_dbt (built-in), not the community CLI.
if [ "$WITH_DBT" = "true" ]; then
  echo "  · dbt_project_component (built-in)"
  uv add dagster-dbt dbt-snowflake >/dev/null 2>&1 || {
    echo "    ⚠ uv add dagster-dbt dbt-snowflake failed:"
    uv add dagster-dbt dbt-snowflake
    exit 1
  }
fi
echo "  ✓ Components installed."

# ─── Add the components' runtime Python dependencies ──────────────────────
# `dg add <component>` copies the component code into the project but
# doesn't add the third-party libraries the component imports. Without
# this, `uv run dg dev` crashes the moment it imports pandas /
# snowflake-connector-python / etc.
echo
echo ">>> Adding runtime Python dependencies to pyproject.toml ..."

# Core deps — needed by synthetic_data_generator + dataframe_to_snowflake +
# snowflake_workspace + snowflake_iceberg_table + freshness_check.
# IMPORTANT: snowflake-connector-python needs the [pandas] extra for
# write_pandas() to work. The bare install doesn't activate pandas integration
# even when pandas is also installed. Bracket extras are safe unquoted because
# nothing matches the glob '[pandas]' in this directory.
RUNTIME_DEPS="pandas snowflake-connector-python[pandas] snowflake-sqlalchemy"

[ "$WITH_SNOWPARK" = "true" ]           && RUNTIME_DEPS="$RUNTIME_DEPS snowflake-snowpark-python"
[ "$WITH_WAREHOUSE_PIPELINE" = "true" ] && RUNTIME_DEPS="$RUNTIME_DEPS sqlglot"
# dataframe_to_s3 needs boto3 + s3fs for the S3 writes; pyarrow for parquet
# (we use CSV here, but s3fs imports it transitively in some paths).
[ -n "${S3_STAGING_BUCKET:-}" ]         && RUNTIME_DEPS="$RUNTIME_DEPS boto3 s3fs pyarrow"
# Cortex uses snowflake-connector-python (already in core). No extra dep.
# Observer also uses snowflake-connector-python (already in core).

echo "    deps: $RUNTIME_DEPS"
if ! uv add $RUNTIME_DEPS >/tmp/bootstrap_uv_add.log 2>&1; then
  echo "    ⚠ uv add failed:"
  cat /tmp/bootstrap_uv_add.log
  rm -f /tmp/bootstrap_uv_add.log
  exit 1
fi
rm -f /tmp/bootstrap_uv_add.log
echo "    ✓ Runtime deps installed."

# ─── Auth field generators ────────────────────────────────────────────────
# Emit the auth-related YAML fields for components that take direct values
# ({{ env('SNOWFLAKE_*') }}) vs ones that use the *_env_var convention.
SNOW_HAS_KEY_PWD=$([ -n "${SNOWFLAKE_PRIVATE_KEY_FILE_PWD:-}" ] && echo yes || echo no)

emit_auth_direct() {
  local I="${1:-  }"
  case "$SNOWFLAKE_AUTH_METHOD" in
    keypair)
      printf '%sauthenticator: SNOWFLAKE_JWT\n' "$I"
      printf '%sprivate_key_file: "{{ env('"'"'SNOWFLAKE_PRIVATE_KEY_FILE'"'"') }}"\n' "$I"
      [ "$SNOW_HAS_KEY_PWD" = "yes" ] && \
        printf '%sprivate_key_file_pwd: "{{ env('"'"'SNOWFLAKE_PRIVATE_KEY_FILE_PWD'"'"') }}"\n' "$I" ;;
    sso) printf '%sauthenticator: externalbrowser\n' "$I" ;;
    pat)
      printf '%sauthenticator: PROGRAMMATIC_ACCESS_TOKEN\n' "$I"
      printf '%stoken: "{{ env('"'"'SNOWFLAKE_PAT'"'"') }}"\n' "$I" ;;
    password|password_mfa|*)
      printf '%spassword: "{{ env('"'"'SNOWFLAKE_PASSWORD'"'"') }}"\n' "$I" ;;
  esac
}

emit_auth_envvar() {
  local I="${1:-  }"
  case "$SNOWFLAKE_AUTH_METHOD" in
    keypair)
      printf '%sauthenticator: SNOWFLAKE_JWT\n' "$I"
      printf '%sprivate_key_file_env_var: SNOWFLAKE_PRIVATE_KEY_FILE\n' "$I"
      [ "$SNOW_HAS_KEY_PWD" = "yes" ] && \
        printf '%sprivate_key_file_pwd_env_var: SNOWFLAKE_PRIVATE_KEY_FILE_PWD\n' "$I" ;;
    sso) printf '%sauthenticator: externalbrowser\n' "$I" ;;
    pat)
      printf '%sauthenticator: PROGRAMMATIC_ACCESS_TOKEN\n' "$I"
      printf '%stoken_env_var: SNOWFLAKE_PAT\n' "$I" ;;
    password|password_mfa|*)
      printf '%spassword_env_var: SNOWFLAKE_PASSWORD\n' "$I" ;;
  esac
}

# Helper: write a defs.yaml under src/<pkg>/defs/<name>/
write_defs() {
  local NAME="$1" YAML="$2"
  mkdir -p "src/$PKG/defs/$NAME"
  printf '%s\n' "$YAML" > "src/$PKG/defs/$NAME/defs.yaml"
}

# ─── 1. python_daily_events (synthetic_data_generator) ────────────────────
# This is the source of the DAG. Daily-partitioned synthetic events that
# events_to_snowflake will load into RAW.EVENTS.

# Default start: 7 days ago, formatted as YYYY-MM-DD.
HET_PARTITION_START=$(date -u -v-7d +%Y-%m-%d 2>/dev/null || date -u -d '7 days ago' +%Y-%m-%d)

# ─── 0. raw_sources — observable source assets for the RAW.* + AI.* tables ─
# Plain Python file (not YAML) — uses @observable_source_asset so each
# table emits row_count + last_altered metadata on each observation. The
# data_version is derived from those two fields, so eager downstream only
# re-fires when the underlying table genuinely changes; quiet ticks don't
# cascade. Materialized by warehouse_observation_schedule alongside the
# stream / stage / alert source assets.
mkdir -p "src/$PKG/defs/raw_sources"
cat > "src/$PKG/defs/raw_sources/defs.py" <<'RAW_SOURCES_EOF'
"""Observable source assets for the seeded RAW.* and AI.* tables.

These tables are created by seed.sql (not by Dagster). Modeling them as
@observable_source_asset (instead of plain AssetSpec) lets the warehouse
observation schedule periodically read their row count + last_altered
metadata. Each observation emits a stable `data_version` derived from
(row_count, last_altered) — downstream eager only re-fires when that
tuple actually changes, so growing data triggers cascades but quiet
observation ticks don't.
"""
import os
from typing import Optional

import snowflake.connector
from dagster import (
    AssetExecutionContext,
    DataVersion,
    Definitions,
    ObserveResult,
    observable_source_asset,
)

_KINDS = {"snowflake", "table"}
_GROUP = "raw_sources"


def _connect():
    kwargs = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"],
        "database": os.environ["SNOWFLAKE_DATABASE"],
    }
    if os.environ.get("SNOWFLAKE_ROLE"):
        kwargs["role"] = os.environ["SNOWFLAKE_ROLE"]
    if os.environ.get("SNOWFLAKE_PAT"):
        kwargs["authenticator"] = "PROGRAMMATIC_ACCESS_TOKEN"
        kwargs["token"] = os.environ["SNOWFLAKE_PAT"]
    elif os.environ.get("SNOWFLAKE_PASSWORD"):
        kwargs["password"] = os.environ["SNOWFLAKE_PASSWORD"]
    return snowflake.connector.connect(**kwargs)


def _observe(context: AssetExecutionContext, db: str, schema: str, table: str) -> ObserveResult:
    row_count: Optional[int] = None
    bytes_: Optional[int] = None
    last_altered: Optional[str] = None
    try:
        conn = _connect()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(f"SHOW TABLES LIKE '{table}' IN SCHEMA {db}.{schema}")
                row = cursor.fetchone()
                if row:
                    cols = [c[0].lower() for c in cursor.description]
                    info = dict(zip(cols, row))
                    if info.get("rows") is not None:
                        try:
                            row_count = int(info["rows"])
                        except (TypeError, ValueError):
                            pass
                    if info.get("bytes") is not None:
                        try:
                            bytes_ = int(info["bytes"])
                        except (TypeError, ValueError):
                            pass
                cursor.execute(
                    f"SELECT TO_VARCHAR(last_altered, 'YYYY-MM-DD HH24:MI:SS') "
                    f"FROM {db}.INFORMATION_SCHEMA.TABLES "
                    f"WHERE TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{table}'"
                )
                row = cursor.fetchone()
                if row and row[0]:
                    last_altered = row[0]
            finally:
                cursor.close()
        finally:
            conn.close()
    except Exception as exc:
        context.log.warning(f"Could not observe {db}.{schema}.{table}: {exc}")

    return ObserveResult(
        data_version=DataVersion(f"{row_count}:{last_altered}"),
        metadata={
            "table": f"{db}.{schema}.{table}",
            "row_count": row_count if row_count is not None else 0,
            "bytes": bytes_ if bytes_ is not None else 0,
            "last_altered": last_altered or "unknown",
        },
    )


def _make(key: str, db: str, schema: str, table: str, description: str):
    @observable_source_asset(
        key=key,
        group_name=_GROUP,
        kinds=_KINDS,
        description=description,
    )
    def _asset(context: AssetExecutionContext) -> ObserveResult:
        return _observe(context, db, schema, table)
    return _asset


defs = Definitions(
    assets=[
        _make(
            "raw_orders", "DAGSTER_DEMO", "RAW", "ORDERS",
            "Snowflake RAW.ORDERS — order destination for Dagster's Python orders ingest "
            "(python_daily_orders + orders_to_snowflake). Seeded empty; rows arrive daily "
            "from the partitioned generator. Read by paid_orders_dt, top_products_dt, "
            "customer_360_dt, daily_orders_rollup, orders_cdc_stream, "
            "customer_lifetime_value_mv, snowpark_order_features.",
        ),
        _make(
            "raw_customers", "DAGSTER_DEMO", "RAW", "CUSTOMERS",
            "Snowflake RAW.CUSTOMERS — seeded customer table. TIER column is mutated "
            "nightly by SP_RECOMPUTE_TIERS. Read by customer_360_dt, weekly_churn_score, "
            "customers_cdc_stream.",
        ),
        _make(
            "raw_products", "DAGSTER_DEMO", "RAW", "PRODUCTS",
            "Snowflake RAW.PRODUCTS — seeded product catalog. Read by top_products_dt.",
        ),
        _make(
            "raw_events", "DAGSTER_DEMO", "RAW", "EVENTS",
            "Snowflake RAW.EVENTS — clickstream destination for Dagster's Python events "
            "ingest (python_daily_events + events_to_snowflake). Seeded empty; rows arrive "
            "daily from the partitioned generator. Read by events_cleaned_dt, "
            "hourly_activity_dt, sp_purge_old_events.",
        ),
        _make(
            "ai_customer_feedback", "DAGSTER_DEMO", "AI", "CUSTOMER_FEEDBACK",
            "Snowflake AI.CUSTOMER_FEEDBACK — text source for the Cortex SUMMARIZE demo.",
        ),
    ]
)
RAW_SOURCES_EOF
echo "  · raw_sources/defs.py (observable source assets for RAW.* + AI.* tables)"

write_defs python_daily_events "type: ${PKG}.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: python_daily_events
  schema_type: events
  row_count: 200
  random_state: 42
  partition_type: daily
  partition_start: \"$HET_PARTITION_START\"
  group_name: dagster_python"

# ─── 2. events_to_snowflake (dataframe_to_snowflake) ──────────────────────
# Reads python_daily_events and writes to RAW.EVENTS. This is the bridge
# from Dagster Python into Snowflake — the seed pre-creates RAW.EVENTS
# with a compatible schema.
write_defs events_to_snowflake "type: ${PKG}.components.dataframe_to_snowflake.component.DataframeToSnowflakeComponent
attributes:
  asset_name: events_to_snowflake
  upstream_asset_key: python_daily_events
  account_env_var: SNOWFLAKE_ACCOUNT
  user_env_var: SNOWFLAKE_USER
$(emit_auth_envvar '  ')
  warehouse: \"{{ env('SNOWFLAKE_WAREHOUSE') }}\"
  database: \"{{ env('SNOWFLAKE_DATABASE') }}\"
  schema: RAW
  table: EVENTS
  # Seed pre-creates RAW.EVENTS empty with the generator's column schema.
  # Switch to 'replace' if you want Python to wipe and re-create per run.
  if_exists: append
  # Partition spec MUST match python_daily_events.
  partition_type: daily
  partition_start: \"$HET_PARTITION_START\"
  group_name: dagster_python"

# ─── 2b. python_daily_orders + orders_to_snowflake ────────────────────────
# Sibling pair to the events ingest: daily synthetic orders generated by the
# component's new 'orders' schema_type, then loaded into RAW.ORDERS via
# dataframe_to_snowflake. Makes orders Dagster-managed (no more static seed
# data) so the whole pipeline runs on actual freshly-generated rows.
write_defs python_daily_orders "type: ${PKG}.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: python_daily_orders
  schema_type: orders
  row_count: 100
  random_state: 7
  partition_type: daily
  partition_start: \"$HET_PARTITION_START\"
  group_name: dagster_python
  description: Dagster Python asset — generates 100 synthetic orders per daily partition (order_id, customer_id, order_date, category, num_items, subtotal, shipping, tax, total, status, region). Output loaded to RAW.ORDERS by orders_to_snowflake."

write_defs orders_to_snowflake "type: ${PKG}.components.dataframe_to_snowflake.component.DataframeToSnowflakeComponent
attributes:
  asset_name: orders_to_snowflake
  upstream_asset_key: python_daily_orders
  account_env_var: SNOWFLAKE_ACCOUNT
  user_env_var: SNOWFLAKE_USER
$(emit_auth_envvar '  ')
  warehouse: \"{{ env('SNOWFLAKE_WAREHOUSE') }}\"
  database: \"{{ env('SNOWFLAKE_DATABASE') }}\"
  schema: RAW
  table: ORDERS
  if_exists: append
  partition_type: daily
  partition_start: \"$HET_PARTITION_START\"
  group_name: dagster_python
  description: Dagster Python → Snowflake bridge for orders. Appends synthetic order rows from python_daily_orders to RAW.ORDERS via write_pandas. Daily-partitioned to match upstream."

# ─── 2c. python_hourly_orders_for_pipe + orders_to_s3 (Snowpipe demo) ─────
# Drops a CSV per hourly partition into s3://$S3_STAGING_BUCKET/orders/
# (the EXTERNAL stage seed.sh wires up). S3 PUT notifications fire the
# Snowflake-managed SQS queue for ORDERS_AUTO_INGEST_PIPE, which COPYs the
# file into STAGING.ORDERS_INGESTED. This is what makes the pipe asset
# actually fire — without this, the pipe is decorative.
if [ -n "${S3_STAGING_BUCKET:-}" ]; then
  # HourlyPartitionsDefinition expects fmt %Y-%m-%d-%H:%M (Dagster's default).
  HET_HOURLY_START=$(date -u -v-1d -v0H -v0M -v0S +%Y-%m-%d-%H:%M 2>/dev/null || date -u -d '1 day ago 00:00:00' +%Y-%m-%d-%H:%M)

  write_defs python_hourly_orders_for_pipe "type: ${PKG}.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: python_hourly_orders_for_pipe
  schema_type: orders
  row_count: 50
  random_state: 13
  partition_type: hourly
  partition_start: \"$HET_HOURLY_START\"
  group_name: dagster_python
  description: \"Dagster Python asset — generates 50 synthetic orders per hourly partition. Output sinks to S3 (orders_to_s3) → Snowpipe → STAGING.ORDERS_INGESTED. Demonstrates live file-driven ingest, distinct from the daily write_pandas path.\""

  write_defs orders_to_s3 "type: ${PKG}.components.dataframe_to_s3.component.DataframeToS3Component
attributes:
  asset_name: orders_to_s3
  upstream_asset_key: python_hourly_orders_for_pipe
  bucket_env_var: S3_STAGING_BUCKET
  # {run_id} suffix ensures every materialization writes a unique S3
  # filename. Snowpipe dedupes by filename (~14-day window); without the
  # run_id, re-running the same partition would be a no-op at the pipe
  # layer. With it, backfills and re-runs each produce a fresh COPY event
  # and a new sensor materialization on snowpipe_orders_auto_ingest_pipe.
  key: \"orders/{partition_key}-{run_id}.csv\"
  format: csv
  partition_type: hourly
  partition_start: \"$HET_HOURLY_START\"
  group_name: dagster_python
  kinds: [s3, python]
  description: \"Writes hourly orders CSV to s3://\$S3_STAGING_BUCKET/orders/{partition_key}-{run_id}.csv. The PUT fires the Snowflake-managed SQS queue behind ORDERS_AUTO_INGEST_PIPE, which COPYs the file into STAGING.ORDERS_INGESTED. Each materialization (including re-runs of the same partition) writes a fresh filename so Snowpipe doesn't dedupe.\""

  write_defs hourly_orders_schedule "type: ${PKG}.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: hourly_orders_schedule
  asset_keys:
    - python_hourly_orders_for_pipe
    - orders_to_s3
  partition_type: hourly
  partition_start: \"$HET_HOURLY_START\"
  default_status: RUNNING
  tags:
    cadence: hourly
    pipeline: snowpipe"
fi

# ─── 2d. Daily schedules for the orders + events write_pandas paths ───────
write_defs daily_orders_schedule "type: ${PKG}.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: daily_orders_schedule
  asset_keys:
    - python_daily_orders
    - orders_to_snowflake
  partition_type: daily
  partition_start: \"$HET_PARTITION_START\"
  default_status: RUNNING
  tags:
    cadence: daily
    pipeline: write_pandas"

write_defs daily_events_schedule "type: ${PKG}.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: daily_events_schedule
  asset_keys:
    - python_daily_events
    - events_to_snowflake
  partition_type: daily
  partition_start: \"$HET_PARTITION_START\"
  default_status: RUNNING
  tags:
    cadence: daily
    pipeline: write_pandas"

# ─── 2e. warehouse_observation_schedule ───────────────────────────────────
# The applicator skips SourceAssets when attaching AutomationConditions
# (split/rejoin), so we can't put on_cron on the @observable_source_asset
# entities (streams, stages, alerts) via the applicator. Schedule them
# directly — each \"materialization\" of an @observable_source_asset invokes
# its observe_fn and emits an ObserveResult, which counts as a parent
# update for eager downstream (e.g. task_process_order_changes_task
# depends on stream_orders_cdc_stream).
write_defs warehouse_observation_schedule "type: ${PKG}.components.cron_schedule.component.CronScheduleComponent
attributes:
  schedule_name: warehouse_observation_schedule
  asset_keys:
    - stream_orders_cdc_stream
    - stream_customers_cdc_stream
    - stage_internal_stage
    - stage_landing_stage
    - alert_high_revenue_day_alert
    # Raw source tables — observe row_count + last_altered every tick;
    # data_version dedupes \"no change\" observations so eager downstream
    # only fires when the underlying table genuinely changed.
    - raw_orders
    - raw_customers
    - raw_products
    - raw_events
    - ai_customer_feedback
  cron_expression: \"*/5 * * * *\"
  default_status: RUNNING
  tags:
    cadence: 5min
    pipeline: observation"

# ─── 3. snowflake_workspace (the centerpiece) ─────────────────────────────
# Imports STAGING.EVENTS_CLEANED_DT (dynamic table) and STAGING.DAILY_ORDERS_ROLLUP
# (task). The `assets_by_name` field declares cross-component dependencies so
# the imported dynamic table shows as downstream of the Dagster Python ingest.
write_defs snowflake_workspace "type: ${PKG}.components.snowflake_workspace.component.SnowflakeWorkspaceComponent
attributes:
  account: \"{{ env('SNOWFLAKE_ACCOUNT') }}\"
  user:    \"{{ env('SNOWFLAKE_USER') }}\"
$(emit_auth_direct '  ')
  warehouse: \"{{ env('SNOWFLAKE_WAREHOUSE') }}\"
  database:  \"{{ env('SNOWFLAKE_DATABASE') }}\"
  schema:    \"{{ env('SNOWFLAKE_SCHEMA') }}\"
  role:      \"{{ env('SNOWFLAKE_ROLE') }}\"
  import_tasks: true
  import_dynamic_tables: true
  import_stored_procedures: true
  import_streams: true
  import_snowpipes: true
  import_stages: true
  import_materialized_views: true
  import_external_tables: false   # seed doesn't create any
  import_alerts: true
  # Snowflake's INFORMATION_SCHEMA.PROCEDURES surfaces SYSTEM\$* built-ins
  # (SYSTEM\$SEND_EMAIL, SYSTEM\$REFERENCE, etc.) — they collide on the
  # generated asset key because the component dedupes on name not signature.
  # Filter them out here.
  exclude_name_pattern: '^(SYSTEM\\\$|EXECUTE_AI_|COMPUTE_AI_)'
  assets_by_name:
    # NOTE: assets_by_name keys are the Snowflake object SHORT name —
    # the workspace transforms them into asset keys like task_<lowercase>
    # and dynamic_table_<lowercase>. The deps list takes ACTUAL asset keys,
    # so cross-references below must use the transformed names.
    # The kinds field shows up as the colored chips next to each asset in the UI.
    EVENTS_CLEANED_DT:
      deps:
        - raw_events
        - events_to_snowflake     # Dagster's events ingest populates RAW.EVENTS
      kinds: [snowflake, dynamic_table]
    HOURLY_ACTIVITY_DT:
      deps:
        - raw_events
        - events_to_snowflake
      kinds: [snowflake, dynamic_table]
    CUSTOMER_360_DT:
      deps:
        - raw_customers
        - raw_orders
        - orders_to_snowflake     # Dagster's orders ingest populates RAW.ORDERS
        - proc_sp_recompute_tiers
      kinds: [snowflake, dynamic_table]
    PAID_ORDERS_DT:
      deps:
        - raw_orders
        - orders_to_snowflake
      kinds: [snowflake, dynamic_table]
    TOP_PRODUCTS_DT:
      deps:
        - raw_orders
        - orders_to_snowflake
        - raw_products
      kinds: [snowflake, dynamic_table]
    DAILY_ORDERS_ROLLUP:
      deps:
        - dynamic_table_events_cleaned_dt
        - dynamic_table_paid_orders_dt
      kinds: [snowflake, task]
    MONTHLY_REVENUE_REPORT:
      deps:
        - task_daily_orders_rollup
      kinds: [snowflake, task]
    HOURLY_CUSTOMER_METRICS:
      deps:
        - dynamic_table_customer_360_dt
      kinds: [snowflake, task]
    WEEKLY_CHURN_SCORE:
      deps:
        - dynamic_table_customer_360_dt
      kinds: [snowflake, task]
    # Nightly maintenance task DAG: tier-update root → events-purge child.
    # Each task CALLs a proc; the proc is therefore DOWNSTREAM of its task.
    NIGHTLY_TIER_UPDATE_TASK:
      deps: []   # root of the maintenance chain
      kinds: [snowflake, task]
    # NIGHTLY_EVENTS_PURGE_TASK — single asset with a Dagster Config form
    # in the launchpad. Visitors see ONE asset, click materialize, see a
    # typed \`days_old\` field defaulted to 90, can override per run. Same
    # EXECUTE TASK USING CONFIG plumbing under the hood; SYSTEM\$GET_TASK_GRAPH_CONFIG
    # in the task body reads whatever the user typed.
    NIGHTLY_EVENTS_PURGE_TASK:
      config_schema:
        days_old:
          type: int
          default: 90
          description: \"Days of event history to retain. Older events are purged.\"
      deps: [task_nightly_tier_update_task]
      kinds: [snowflake, task]
    # Stream-consumer task — drains ORDERS_CDC_STREAM into ANALYTICS.ORDERS_CHANGELOG
    PROCESS_ORDER_CHANGES_TASK:
      deps:
        - stream_orders_cdc_stream
      kinds: [snowflake, task]
    # Stored procedures — downstream of the tasks that CALL them.
    # Multi-instance pattern for SP_SNOWPARK_TOP_N demonstrates one proc → N
    # Dagster assets, each invoking the same proc with different args.
    SP_RECOMPUTE_TIERS:
      deps:
        - task_nightly_tier_update_task
      kinds: [snowflake, stored_procedure]
    SP_PURGE_OLD_EVENTS:
      args: [90]
      deps:
        - task_nightly_events_purge_task
      kinds: [snowflake, stored_procedure]
    SP_SNOWPARK_TOP_N:
      instances:
        - asset_name: snowpark_top3_revenue_days
          args: [3]
          description: \"Top 3 revenue days — for executive dashboards.\"
          deps: [task_daily_orders_rollup]
          kinds: [snowflake, stored_procedure, snowpark]
          group_name: snowflake_workspace
        - asset_name: snowpark_top10_revenue_days
          args: [10]
          description: \"Top 10 revenue days — operational reporting.\"
          deps: [task_daily_orders_rollup]
          kinds: [snowflake, stored_procedure, snowpark]
          group_name: snowflake_workspace
        - asset_name: snowpark_top100_revenue_days
          args: [100]
          description: \"Top 100 revenue days — historical analysis.\"
          deps: [task_daily_orders_rollup]
          kinds: [snowflake, stored_procedure, snowpark]
          group_name: snowflake_workspace
    # Streams — Snowflake CDC, modeled as @observable_source_asset.
    # Passive observers (no deps, no execution sequence). Grouped separately
    # so the main snowflake_workspace group only contains materializable assets.
    ORDERS_CDC_STREAM:
      group_name: warehouse_observation
      kinds: [snowflake, stream]
    CUSTOMERS_CDC_STREAM:
      group_name: warehouse_observation
      kinds: [snowflake, stream]
    # Materialized view
    CUSTOMER_LIFETIME_VALUE_MV:
      deps:
        - dynamic_table_paid_orders_dt   # both summarize RAW.ORDERS
      kinds: [snowflake, materialized_view]
    # Stages — observable source assets, emit file_count + total_bytes metrics.
    # Live queue monitoring, not part of an execution sequence.
    INTERNAL_STAGE:
      group_name: warehouse_observation
      kinds: [snowflake, stage]
    LANDING_STAGE:
      group_name: warehouse_observation
      kinds: [snowflake, stage]
    # Snowpipes — materializable (ALTER PIPE … REFRESH). Stay in snowflake_workspace.
    ORDERS_MANUAL_INGEST_PIPE:
      deps:
        - stage_internal_stage
      kinds: [snowflake, snowpipe]
    ORDERS_AUTO_INGEST_PIPE:
      deps:
        - stage_landing_stage
$([ -n "${S3_STAGING_BUCKET:-}" ] && echo '        - orders_to_s3   # Dagster drops hourly CSVs into the bucket prefix the pipe watches')
      kinds: [snowflake, snowpipe]
    # Alert — observable source asset, monitors a condition and logs.
    HIGH_REVENUE_DAY_ALERT:
      group_name: warehouse_observation
      kinds: [snowflake, alert]
  group_name: snowflake_workspace"

# ─── 4. snowflake_iceberg_table (if Iceberg volume available) ─────────────
# Materializes ANALYTICS.DAILY_REVENUE as an Iceberg table backed by the
# external volume that seed.sh provisioned.
if [ -n "${ICEBERG_EXTERNAL_VOLUME:-}" ]; then
  write_defs iceberg_daily_revenue "type: ${PKG}.components.snowflake_iceberg_table.component.SnowflakeIcebergTableComponent
attributes:
  asset_name: iceberg_daily_revenue
  account: \"{{ env('SNOWFLAKE_ACCOUNT') }}\"
  user:    \"{{ env('SNOWFLAKE_USER') }}\"
$(emit_auth_direct '  ')
  warehouse:   \"{{ env('SNOWFLAKE_WAREHOUSE') }}\"
  database:    \"{{ env('SNOWFLAKE_DATABASE') }}\"
  schema_name: ANALYTICS
  role:        \"{{ env('SNOWFLAKE_ROLE') }}\"
  table_name: DAILY_REVENUE_ICEBERG
  external_volume: \"{{ env('ICEBERG_EXTERNAL_VOLUME') }}\"
  base_location: 'iceberg/daily_revenue/'
  sql: |
    SELECT *
    FROM {{ env('SNOWFLAKE_DATABASE') }}.ANALYTICS.DAILY_REVENUE
  deps:
    - task_daily_orders_rollup
  group_name: iceberg_lake"
fi

# ─── 5. freshness_check on ANALYTICS.DAILY_REVENUE ────────────────────────
# Always on. Demonstrates Dagster's data-quality story without needing to
# add another engine to the mix.
write_defs freshness_daily_revenue "type: ${PKG}.components.freshness_check.component.FreshnessPolicyComponent
attributes:
  asset_key: task_daily_orders_rollup
  fail_window_hours: 24.0
  warn_window_hours: 12.0"

# ─── 6. Opt-ins ───────────────────────────────────────────────────────────
if [ "$WITH_CORTEX" = "true" ]; then
  # Cortex uses snowflake_*_env_var field convention (different from others).
  # Use one -e per substitution so this works on BSD sed (macOS) AND GNU sed.
  CORTEX_AUTH="$(emit_auth_envvar '  ' | sed \
    -e 's/^  authenticator/  snowflake_authenticator/' \
    -e 's/^  private_key_file_env_var/  snowflake_private_key_file_env_var/' \
    -e 's/^  private_key_file_pwd_env_var/  snowflake_private_key_file_pwd_env_var/' \
    -e 's/^  password_env_var/  snowflake_password_env_var/' \
    -e 's/^  token_env_var/  snowflake_token_env_var/')"
  write_defs cortex_feedback_summary "type: ${PKG}.components.snowflake_cortex_asset.component.SnowflakeCortexAssetComponent
attributes:
  asset_name: cortex_feedback_summary
  snowflake_account_env_var: SNOWFLAKE_ACCOUNT
  snowflake_user_env_var:    SNOWFLAKE_USER
$CORTEX_AUTH
  snowflake_database:  \"{{ env('SNOWFLAKE_DATABASE') }}\"
  snowflake_schema:    AI
  snowflake_warehouse: \"{{ env('SNOWFLAKE_WAREHOUSE') }}\"
  cortex_function: summarize
  source_table: AI.CUSTOMER_FEEDBACK
  target_table: AI.CUSTOMER_FEEDBACK_SUMMARIZED
  text_column: comment
  deps:
    - ai_customer_feedback         # real source of the SUMMARIZE input
    - task_weekly_churn_score      # ordering hint
  group_name: ai_enrichment"
fi

if [ "$WITH_SNOWPARK" = "true" ]; then
  # snowpark_pipeline now wants connection params as a nested `connection:` dict.
  # Indent the auth fields 4 spaces so they nest correctly under it.
  SP_AUTH="$(emit_auth_envvar '    ')"
  write_defs snowpark_order_features "type: ${PKG}.components.snowpark_pipeline.component.SnowparkPipelineComponent
attributes:
  asset_name: snowpark_order_features
  connection:
    account_env_var: SNOWFLAKE_ACCOUNT
    user_env_var: SNOWFLAKE_USER
$SP_AUTH
    warehouse: \"{{ env('SNOWFLAKE_WAREHOUSE') }}\"
    database:  \"{{ env('SNOWFLAKE_DATABASE') }}\"
    role:      \"{{ env('SNOWFLAKE_ROLE') }}\"
  source:
    kind: table
    table: RAW.ORDERS
  operations:
    - op: filter
      predicate: \"STATUS IN ('paid', 'delivered')\"
  sink:
    kind: table
    table: ANALYTICS.ORDER_FEATURES
    mode: overwrite        # snowpark SaveMode: append|overwrite|errorifexists|ignore|truncate
  group_name: snowpark_features
  deps:
    - dynamic_table_paid_orders_dt"
fi

if [ "$WITH_WAREHOUSE_PIPELINE" = "true" ]; then
  write_defs revenue_top_states "type: ${PKG}.components.warehouse_pipeline.component.WarehousePipelineComponent
attributes:
  asset_name: revenue_top_states
  dialect: snowflake
  database_url_env_var: SNOWFLAKE_URL
  steps:
    - id: enriched_revenue
      source: {kind: table, table: ANALYTICS.DAILY_REVENUE}
      operations:
        - op: sql
          sql: |
            SELECT *, REVENUE / NULLIF(ORDER_COUNT, 0) AS AVG_ORDER_VALUE
            FROM <<self>>
    - id: top_revenue_days
      source: {kind: ref, ref: enriched_revenue}
      operations:
        - {op: top_n, sort_by: REVENUE, n: 10, ascending: false}
  sinks:
    - {from: top_revenue_days, table: ANALYTICS.TOP_REVENUE_DAYS, mode: replace}
  group_name: warehouse_pipeline
  deps:
    - task_daily_orders_rollup"
fi

if [ "$WITH_OBSERVER" = "true" ]; then
  OBS_AUTH="$(emit_auth_envvar '  ')"
  write_defs daily_revenue_observer "type: ${PKG}.components.snowflake_table_observation_sensor.component.SnowflakeTableObservationSensorComponent
attributes:
  sensor_name: daily_revenue_observer
  asset_key: task_daily_orders_rollup
  account: \"{{ env('SNOWFLAKE_ACCOUNT') }}\"
  database: \"{{ env('SNOWFLAKE_DATABASE') }}\"
  schema_name: ANALYTICS
  table_name: DAILY_REVENUE
  username_env_var: SNOWFLAKE_USER
$OBS_AUTH
  warehouse: \"{{ env('SNOWFLAKE_WAREHOUSE') }}\"
  check_interval_seconds: 60"
fi

# ─── 6.5 Automation conditions ────────────────────────────────────────────
# Applied AFTER all other components have registered their assets. The
# applicator runs at definitions-load time, walks the asset graph, and
# attaches AutomationCondition.eager() to the selected assets so that
# sensor-emitted materializations (DT refreshes, pipe loads) propagate
# downstream without manual clicks or extra schedules.
write_defs automation_conditions "type: ${PKG}.components.automation_condition_applicator.component.AutomationConditionApplicatorComponent
attributes:
  # If any component ever sets its own automation_condition, that wins —
  # we don't silently override component-level intent.
  preserve_existing: true
  rules:
    # Narrow on_cron rules first (first_match wins) — these assets are
    # otherwise orphans for eager: cortex_feedback_summary's only parent
    # is a static AssetSpec that never materializes, and
    # task_nightly_tier_update_task is a root task with deps:[]. Both
    # need their own cron to fire visibly during the demo.
    - name: cortex_summary_oncron
      selection: \"key:cortex_feedback_summary\"
      cron: \"*/15 * * * *\"

    - name: nightly_root_task_oncron
      selection: \"key:task_nightly_tier_update_task\"
      cron: \"0 * * * *\"

    # Snowflake-side compute: tasks, stored procedures (incl. Snowpark
    # SP instances), materialized views, AND snowpipes (so the manual-
    # refresh pipe fires when stage_internal_stage gets observed, and
    # the auto-ingest pipe gets a periodic safety-net REFRESH on top of
    # its SQS-driven loads). DTs excluded — Snowflake auto-refreshes
    # them via TARGET_LAG.
    - name: workspace_compute_eager
      selection: \"kind:task or kind:stored_procedure or kind:materialized_view or kind:snowpipe\"
      preset: eager
    # Dagster-managed downstream (Iceberg sink, warehouse pipeline,
    # Snowpark features, Cortex enrichment). Each materializes off
    # whichever upstream — task, DT, raw table — most recently changed.
    - name: dagster_downstream_eager
      selection: \"group:iceberg_lake or group:warehouse_pipeline or group:snowpark_features or group:ai_enrichment\"
      preset: eager"

# ─── 7. Validate with dg check defs ───────────────────────────────────────
echo
echo ">>> Validating defs.yaml configuration with dg check defs ..."
if uv run dg check defs 2>&1 | tee /tmp/bootstrap_check.log | tail -20; then
  if grep -qiE 'error|invalid|fail' /tmp/bootstrap_check.log; then
    echo "  ⚠ dg check defs reported issues — inspect the output above."
    echo "    Common fixes:"
    echo "      • Field name mismatch between this script's YAML and the installed component schema"
    echo "        (the community components evolve; verify with: uv run dg list components)"
    echo "      • Auth field convention (direct vs _env_var) — check the component's docs"
    echo "    The project still scaffolded — edit src/$PKG/defs/<name>/defs.yaml as needed."
  else
    echo "  ✓ dg check defs passed clean."
  fi
else
  echo "  ⚠ dg check defs failed. Project scaffolded but defs need attention — edit the files under src/$PKG/defs/"
fi
rm -f /tmp/bootstrap_check.log

# ─── 8. Trailing summary ──────────────────────────────────────────────────
cat <<DONE

═══════════════════════════════════════════════════════════════════════
  Bootstrap complete.
═══════════════════════════════════════════════════════════════════════
  Project:      ./$PROJECT_NAME/
  .env:         symlinked → ../$(basename "$ENV_FILE")

  Components scaffolded:
    src/$PKG/defs/python_daily_events/defs.yaml          (Python: synthetic events)
    src/$PKG/defs/events_to_snowflake/defs.yaml          (Python → RAW.EVENTS)
    src/$PKG/defs/snowflake_workspace/defs.yaml          (imports STAGING dynamic table + task)
DONE
[ -n "${ICEBERG_EXTERNAL_VOLUME:-}" ] && \
  echo "    src/$PKG/defs/iceberg_daily_revenue/defs.yaml         (Iceberg sink off ANALYTICS.DAILY_REVENUE)"
echo "    src/$PKG/defs/freshness_daily_revenue/defs.yaml      (quality check)"
[ "$WITH_CORTEX" = "true" ]             && echo "    src/$PKG/defs/cortex_feedback_summary/defs.yaml      (Cortex: summarize AI.CUSTOMER_FEEDBACK)"
[ "$WITH_SNOWPARK" = "true" ]           && echo "    src/$PKG/defs/snowpark_order_features/defs.yaml      (Snowpark feature engineering)"
[ "$WITH_WAREHOUSE_PIPELINE" = "true" ] && echo "    src/$PKG/defs/revenue_top_states/defs.yaml           (warehouse_pipeline multi-step SQL)"
[ "$WITH_OBSERVER" = "true" ]           && echo "    src/$PKG/defs/daily_revenue_observer/defs.yaml       (observation sensor)"

cat <<DONE

  Expected lineage when you open the UI (one connected spine):

    python_daily_events
            ↓
    events_to_snowflake (→ RAW.EVENTS)
            ↓
    EVENTS_CLEANED_DT (workspace import)
            ↓
    DAILY_ORDERS_ROLLUP (workspace import; reads ORDERS + cleaned events)
            ↓
    ANALYTICS.DAILY_REVENUE
            ↓
    iceberg_daily_revenue (if Iceberg available)
            ↓
    freshness_daily_revenue (quality check overlay)

  Next:
    cd $PROJECT_NAME
    uv run dg dev      # dg auto-loads .env and .env.secrets from the current dir

  Then open http://localhost:3000, click "Assets → Global Asset Lineage",
  and you should see one cohesive graph spanning Python + Snowflake-native + Iceberg.

  To add capabilities later: re-run with --with-cortex / --with-snowpark / --with-dbt / --with-warehouse-pipeline / --with-observer.
DONE
