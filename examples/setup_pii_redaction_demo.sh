#!/usr/bin/env bash
# setup_pii_redaction_demo.sh
#
# The GDPR / HIPAA compliance ask every enterprise has: "we need to detect
# and redact PII across our data before it hits the warehouse / logs / LLM."
#
# Two-pass pipeline:
#   1. Rule-based detection + redaction via pii_detector + pii_redactor
#      (Presidio under the hood — regex + NER for PERSON / EMAIL / PHONE / etc.)
#   2. LLM double-check for edge cases the rules miss (name-in-context,
#      ambiguous strings, custom sensitive fields)
#
# The two-pass shape catches ~99% of PII: rules for the fast path, LLM for
# the long tail. This is how compliance teams actually build these.
#
# What it demonstrates
#   • synthetic_data_generator (support tickets, contain seeded PII)
#   • pii_detector — finds PII entities + confidence scores
#   • pii_redactor — replaces PII with placeholders (or masks / hashes)
#   • langchain_chain_asset — LLM double-check for edge cases
#
# Cost: ~$0.005 per run (rules are free; LLM double-check is a small call).
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • OPENAI_API_KEY (for the LLM double-check step)
#
# Usage
#   export OPENAI_API_KEY=sk-...
#   ./setup_pii_redaction_demo.sh                    # → pii_demo/
#   ./setup_pii_redaction_demo.sh my_project         # custom name

set -eo pipefail

PROJECT_NAME="${1:-pii_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

[ -z "${OPENAI_API_KEY:-}" ] && fail "OPENAI_API_KEY not set (for the LLM double-check step)."
command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Target project: $PROJECT_DIR"

info "Scaffolding Dagster project…"
uvx create-dagster project "$PROJECT_DIR" --uv-sync 2>&1 | tail -3 || fail "create-dagster failed"
cd "$PROJECT_DIR"

info "Installing deps (dagster-community-components + Presidio + langchain-openai + spaCy model)…"
if [ -n "${DCC_SRC:-}" ] && [ -d "$DCC_SRC" ]; then
  info "Using local DCC source: $DCC_SRC"
  uv add --quiet "dagster-community-components @ ${DCC_SRC}" \
    'presidio-analyzer>=2.2.0' 'presidio-anonymizer>=2.2.0' 'spacy>=3.5.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' 'pandas>=1.5.0' 'tabulate>=0.9.0' \
    || fail "uv add failed"
else
  uv add --quiet 'dagster-community-components @ git+https://github.com/eric-thomas-dagster/dagster-component-templates.git' \
    'presidio-analyzer>=2.2.0' 'presidio-anonymizer>=2.2.0' 'spacy>=3.5.0' \
    'langchain-core>=0.3.0' 'langchain-openai>=0.2.0' 'pandas>=1.5.0' 'tabulate>=0.9.0' \
    || fail "uv add failed"
fi
info "Installing spaCy en_core_web_sm (needed for Presidio NER)…"
uv add --quiet 'https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl' 2>&1 | tail -1
# Presidio's spacy-model auto-downloader shells out to `python -m pip install`,
# but uv-created venvs don't ship pip. Install pip in the venv so Presidio's
# fallback path works if the model wheel above ever misses.
uv pip install --python "$PROJECT_DIR/.venv/bin/python" pip 2>&1 | tail -1
ok "Deps installed"

# Persistent IO manager so the four assets can share state across
# sequential materialize invocations.
mkdir -p "$PROJECT_DIR/.dagster_storage"
cat > "src/${PROJECT_NAME}/definitions.py" <<'PY'
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

mkdir -p "src/${PROJECT_NAME}/defs/support_tickets" \
         "src/${PROJECT_NAME}/defs/pii_detected"   \
         "src/${PROJECT_NAME}/defs/redacted_tickets" \
         "src/${PROJECT_NAME}/defs/llm_double_check"

# 1. Synthetic support tickets — seed contains messages with PERSON, EMAIL, PHONE, SSN patterns
cat > "src/${PROJECT_NAME}/defs/support_tickets/defs.yaml" <<'YAML'
type: dagster_community_components.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets
  schema_type: support_tickets
  row_count: 30
  random_state: 42
  group_name: pii
YAML

# 2. Presidio-based PII detection over the ticket body
cat > "src/${PROJECT_NAME}/defs/pii_detected/defs.yaml" <<'YAML'
type: dagster_community_components.PiiDetectorComponent
attributes:
  asset_name: pii_detected
  upstream_asset_key: support_tickets
  input_column: ticket_text
  output_column: pii_entities
  entity_types:
    - PERSON
    - EMAIL_ADDRESS
    - PHONE_NUMBER
    - US_SSN
    - CREDIT_CARD
    - IP_ADDRESS
  score_threshold: 0.5
  language: en
  add_count_column: true
  group_name: pii
YAML

# 3. Redact with placeholders
cat > "src/${PROJECT_NAME}/defs/redacted_tickets/defs.yaml" <<'YAML'
type: dagster_community_components.PiiRedactorComponent
attributes:
  asset_name: redacted_tickets
  upstream_asset_key: pii_detected
  input_column: ticket_text
  output_column: ticket_text_redacted
  entity_types:
    - PERSON
    - EMAIL_ADDRESS
    - PHONE_NUMBER
    - US_SSN
    - CREDIT_CARD
    - IP_ADDRESS
  replacement_style: placeholder
  score_threshold: 0.5
  language: en
  group_name: pii
YAML

# 4. LLM double-check — catch anything Presidio missed. The prompt asks the
#    LLM to flag any remaining sensitive info that regex/NER wouldn't
#    catch (nicknames, internal IDs, org-specific PII, etc).
cat > "src/${PROJECT_NAME}/defs/llm_double_check/defs.yaml" <<'YAML'
type: dagster_community_components.LangChainChainAssetComponent
attributes:
  asset_name: llm_double_check
  upstream_asset_key: redacted_tickets
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  temperature: 0.0
  max_tokens: 200

  system_message: |
    You are a data-privacy reviewer. Given a text where PII has ALREADY been
    redacted with placeholders like <PERSON>, <EMAIL_ADDRESS>, <PHONE_NUMBER>,
    check for anything remaining that a privacy audit would flag: internal
    account IDs, nicknames, proper-noun place names, dates of birth, medical
    conditions, salary info, or any other quasi-identifying info. Reply
    with JSON: {"clean": true/false, "flags": ["...", ...]}. If truly clean,
    reply {"clean": true, "flags": []}.

  prompt_template: |
    Redacted text: {ticket_text_redacted}

  parse_json: true
  group_name: pii
YAML

ok "Wrote 4 defs.yaml (synth → detect → redact → LLM check)"

# Materialize in order — persistent IO manager handles hand-off
DM="${PROJECT_NAME}.definitions"
info "Materializing support_tickets…"
uv run dagster asset materialize --select support_tickets -m "$DM" 2>&1 | tail -3 || fail "synth failed"
info "Materializing pii_detected (Presidio scan)…"
uv run dagster asset materialize --select pii_detected -m "$DM" 2>&1 | tail -3 || fail "pii_detected failed"
info "Materializing redacted_tickets (Presidio redact)…"
uv run dagster asset materialize --select redacted_tickets -m "$DM" 2>&1 | tail -3 || fail "redact failed"
info "Materializing llm_double_check (LLM edge-case sweep, 30 calls ~5s)…"
uv run dagster asset materialize --select llm_double_check -m "$DM" 2>&1 | tail -3 || fail "llm check failed"

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran:
  1. Generated 30 synthetic support tickets (contain PERSON / EMAIL / PHONE / SSN)
  2. Presidio scanned each ticket's description column → pii_entities column
  3. Presidio redacted the PII with placeholders → description_redacted column
  4. gpt-4o-mini reviewed each redacted text for anything Presidio missed
     → clean (bool) + flags (list) columns

Inspect the results:
  cd $PROJECT_NAME
  uv run dg dev
    → asset graph: support_tickets → pii_detected → redacted_tickets → llm_double_check
    → click llm_double_check → filter by clean == false to see LLM-flagged edge cases

Production variants:
  • Replace synthetic_data_generator with your real ticket ingestion
    (zendesk_ingestion, intercom_resource, salesforce_ingestion, etc.)
  • Add a downstream sink (dataframe_to_snowflake / dataframe_to_bigquery)
    that writes ONLY the redacted columns to your warehouse
  • Chain a dagster_asset_check that fails the run if llm_double_check
    finds any row with clean == false — GDPR audit hook
EOF
