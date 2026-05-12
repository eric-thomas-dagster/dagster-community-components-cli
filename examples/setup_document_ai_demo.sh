#!/usr/bin/env bash
# Document AI OCR demo — generate 2 PDFs, extract text via Cloud Document AI.
#
# WHAT THIS DEMONSTRATES (validated live against servicepulse-490502)
#   100% components — no custom Python in defs/.
#     synthetic_pdf_generator   → 2 PDFs on disk (invoice + letter)
#     document_ai_extractor     → OCR each → DataFrame with doc_text column
#
# Asset graph:
#   sample_documents          ← synthetic_pdf_generator (default samples)
#         │
#         └── documents_extracted  ← document_ai_extractor (OCR_PROCESSOR)
#                                    adds doc_text + doc_page_count
#
# REQUIRED ENV VARS
#   GOOGLE_APPLICATION_CREDENTIALS  service-account JSON path
#   GCP_PROJECT_ID                  project id
#   DOCUMENTAI_PROCESSOR_ID         processor UUID (auto-created below if not set)
#
# REQUIRED API
#   Document AI  https://console.cloud.google.com/apis/library/documentai.googleapis.com
#
# REQUIRED IAM
#   roles/documentai.apiUser
#
# COST: ~$0.001 (2 pages well under 1000-pages/month free tier).

set -euo pipefail
PROJECT_DIR="${1:-document-ai-demo}"

if [ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  echo "ERROR: set GOOGLE_APPLICATION_CREDENTIALS"; exit 1
fi
if [ -z "${GCP_PROJECT_ID:-}" ]; then
  echo "ERROR: set GCP_PROJECT_ID"; exit 1
fi
LOCATION="${DOCUMENTAI_LOCATION:-us}"

# Auto-create OCR processor if not supplied
if [ -z "${DOCUMENTAI_PROCESSOR_ID:-}" ]; then
  echo ">>> Auto-creating OCR processor"
  DOCUMENTAI_PROCESSOR_ID="$(python3 <<PY
from google.cloud import documentai_v1 as documentai
from google.oauth2 import service_account
from google.api_core.client_options import ClientOptions
creds = service_account.Credentials.from_service_account_file('$GOOGLE_APPLICATION_CREDENTIALS')
opts = ClientOptions(api_endpoint='$LOCATION-documentai.googleapis.com')
c = documentai.DocumentProcessorServiceClient(credentials=creds, client_options=opts)
parent = f'projects/$GCP_PROJECT_ID/locations/$LOCATION'
existing = [p for p in c.list_processors(parent=parent) if p.type_ == 'OCR_PROCESSOR']
if existing:
    print(existing[0].name.split('/')[-1])
else:
    p = c.create_processor(parent=parent, processor={'display_name': 'demo-ocr', 'type_': 'OCR_PROCESSOR'})
    print(p.name.split('/')[-1])
PY
)"
  echo "    Processor id: $DOCUMENTAI_PROCESSOR_ID"
fi

echo ">>> Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas google-auth google-cloud-documentai reportlab
uv add --dev -q dagster-dg-cli

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing synthetic_pdf_generator + document_ai_extractor"
$CLI add synthetic_pdf_generator  --auto-install 2>&1 | tail -2
$CLI add document_ai_extractor    --auto-install 2>&1 | tail -2

echo 'from .component import SyntheticPdfGeneratorComponent
__all__ = ["SyntheticPdfGeneratorComponent"]' > "src/$PKG/components/synthetic_pdf_generator/__init__.py"
echo 'from .component import DocumentAiExtractorComponent
__all__ = ["DocumentAiExtractorComponent"]' > "src/$PKG/components/document_ai_extractor/__init__.py"

# Remove auto-installed example defs (their asset names collide with ours)
rm -rf "src/$PKG/defs/synthetic_pdf_generator" "src/$PKG/defs/document_ai_extractor"

# 1) Synthetic PDFs (built-in invoice + letter)
mkdir -p "src/$PKG/defs/sample_documents"
cat > "src/$PKG/defs/sample_documents/defs.yaml" <<EOF
type: $PKG.components.synthetic_pdf_generator.component.SyntheticPdfGeneratorComponent
attributes:
  asset_name: sample_documents
  output_dir: /tmp/docai_demo_pdfs
  samples: default
  group_name: ingest
EOF

# 2) Document AI extractor
mkdir -p "src/$PKG/defs/documents_extracted"
cat > "src/$PKG/defs/documents_extracted/defs.yaml" <<EOF
type: $PKG.components.document_ai_extractor.component.DocumentAiExtractorComponent
attributes:
  asset_name: documents_extracted
  upstream_asset_key: sample_documents
  credentials_path: "$GOOGLE_APPLICATION_CREDENTIALS"
  project_id: "$GCP_PROJECT_ID"
  location: "$LOCATION"
  processor_id: "$DOCUMENTAI_PROCESSOR_ID"
  document_column: file_path
  extract_text: true
  extract_form_fields: false
  extract_entities: false
  extract_tables: false
  group_name: extract
EOF

cat <<MSG

>>> Setup complete (100% components — no custom Python in defs/).

Asset graph:
    sample_documents          ← synthetic_pdf_generator (2 default PDFs)
          │
          └── documents_extracted  ← document_ai_extractor (OCR)

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'
MSG
