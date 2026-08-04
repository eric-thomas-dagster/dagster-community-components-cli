# Document AI — OCR text extraction in Dagster
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

**Validated end-to-end against real APIs** (servicepulse-490502, OCR_PROCESSOR).
Generates 2 synthetic PDFs, sends them through Cloud Document AI's OCR
processor, extracts the full plain text into a DataFrame column.

```
sample_documents         ← synthetic_pdf_generator (built-in invoice + letter)
       │
       └── documents_extracted  ← document_ai_extractor (OCR_PROCESSOR)
                                  adds doc_text + doc_page_count columns
```

## Components used

| Component | What it does |
|---|---|
| `synthetic_pdf_generator` | Generate sample PDFs (built-in invoice + letter, or your own custom set). Emits a DataFrame of `(doc_id, kind, file_path, pages)` — feeds straight into `document_ai_extractor` / `vision_api_asset`. |
| `document_ai_extractor` | Per-row Cloud Document AI extraction. Supports any processor type — OCR, FORM_PARSER, INVOICE_PROCESSOR, LAYOUT_PARSER, US_DRIVER_LICENSE, custom (CDE), etc. Toggle output columns via `extract_text` / `extract_form_fields` / `extract_entities` / `extract_tables`. |

## Live run output

| doc_id | doc_text (first 200 chars) | doc_page_count |
|---|---|---|
| INV-2026-0042 | `INVOICE #INV-2026-0042\nFrom: Acme Corp\nTo: Bob Smith\nDate: 2026-05-11\nSubtotal: $1,200.00\nTax: $96.00\nTotal: $1,296.00` | 1 |
| shipping-notice | `Dear Customer,\nThank you for your recent purchase. Your order has shipped\nvia UPS tracking number 1Z999AA10123456784.\nEstimated delivery is 2026-05-15.\n...` | 1 |

Notice the **tracking number `1Z999AA10123456784`** was extracted perfectly — useful signal for downstream OCR validation.

## Processor types

| Type | Use for | Output flags to enable |
|---|---|---|
| **OCR_PROCESSOR** (this demo) | General text extraction | `extract_text: true` |
| **FORM_PARSER_PROCESSOR** | Key/value pairs from any form | `extract_form_fields: true` |
| **INVOICE_PROCESSOR** | Invoice line items + totals | `extract_entities: true` |
| **US_DRIVER_LICENSE_PROCESSOR** | US driver's license parsing | `extract_entities: true` |
| **LAYOUT_PARSER_PROCESSOR** | Full doc structure (sections, headers, tables) | `extract_tables: true` |
| **CUSTOM_EXTRACTION_PROCESSOR** | Your trained Custom Document Extractor | `extract_entities: true` |

Create processors at <https://console.cloud.google.com/ai/document-ai/processors> — OCR is free up to 1000 pages/month.

## Processor auto-creation

The setup script auto-creates an OCR processor if `DOCUMENTAI_PROCESSOR_ID` isn't set. It's idempotent — re-runs reuse the existing one.

## Cost

- OCR: $1.50/1000 pages above free tier (1000/month)
- Form parser: $30/1000 pages
- Invoice processor: $10/1000 pages
- See <https://cloud.google.com/document-ai/pricing>

This demo: **2 pages = free.**

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
export GCP_PROJECT_ID=your-project
# DOCUMENTAI_PROCESSOR_ID optional — auto-created if missing
# DOCUMENTAI_LOCATION=us  (default; `eu` also supported)
```

## Required IAM

- `roles/documentai.apiUser` — to invoke processors
- `roles/documentai.editor` — additionally needed if you want the setup script to auto-create the processor

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_document_ai_demo.sh | bash
cd document-ai-demo
uv run dg launch --assets '*'
```

## Teardown

If you want to remove the demo processor:
```bash
gcloud documentai processors delete <PROCESSOR_ID> \
  --location=us --project=$GCP_PROJECT_ID --quiet
```

Or leave it — OCR processors have no idle cost.

## See also

<!-- TODO: link related walkthroughs -->
