# Google Drive + Docs + Gemini — full lineage on real Workspace data

**Validated end-to-end against a real Google account.** RUN_SUCCESS in
~6s for the three-asset chain — Drive listing → Docs text → Gemini
summary. Real Doc owned by `ethomasii@gmail.com`, 661 words extracted,
Gemini 2.5 Flash one-sentence summary materialized.

```
drive_listing      ← google_drive_ingestion
                     (Drive API, q="mimeType='application/vnd.google-apps.document'")
        │
        ▼
doc_texts          ← google_docs_extractor
                     (Docs API → plain text + headings + word_count)
        │
        ▼
doc_summaries      ← gemini_llm
                     (gemini-2.5-flash, one-sentence summary, thinking_budget=0)
```

## Components covered (3)

| Component | What it does |
|---|---|
| [`google_drive_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_drive_ingestion) | List files in Drive matching a query / folder, optionally download contents. Service-account auth. |
| [`google_docs_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_docs_extractor)  | Extract plain text + headings from Google Docs by ID. Pairs with Drive listings via `upstream_asset_key`. |
| [`gemini_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/gemini_llm)             | Per-row text generation with Gemini text models. Drop-in peer of [`openai_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/openai_llm) / [`anthropic_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/anthropic_llm). |

## Validation status — all three live

| Component | Run | Outcome |
|---|---|---|
| [`google_drive_ingestion`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_drive_ingestion) | 935ms | Listed 1 Doc owned by `ethomasii@gmail.com`, 3,377 bytes |
| [`google_docs_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_docs_extractor) | 1.46s | Extracted 661 words from Docs API |
| [`gemini_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/gemini_llm) | 1.85s | Real summary: "The user proposes five changes to improve clarity and conciseness…" |

## Cost

**$0.** Drive + Docs APIs are free at this volume; Gemini 2.5 Flash has free-tier quota (5 RPM).

## Required env vars

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
export GEMINI_API_KEY=...      # or GOOGLE_API_KEY (gemini_llm only)
```

## Setup checklist

1. Create a service-account JSON (see [google_sheets.md](google_sheets.md#service-account-setup) for the full UI walkthrough).
2. **Enable three APIs on the SA's GCP project**:
   - Google Drive API
   - Google Docs API
   - Generative Language API (for Gemini)

   The components surface a `403 SERVICE_DISABLED` with the exact activation URL the first time each isn't enabled. Click and Enable, ~30s to propagate.
3. **Share Drive items with the SA email** (the `client_email` in your JSON, e.g. `<name>@<project>.iam.gserviceaccount.com`). Anything not shared is invisible to the SA.

## Run it

```bash
./setup_google_drive_docs_demo.sh
cd google-drive-docs-demo
uv run dg launch --assets '*'
```

## Bugs surfaced and fixed validating this demo

1. **Docs API not enabled per-project.** Same gotcha as Sheets — service accounts often live in a project that hasn't enabled the API yet. Component surfaces the activation URL.
2. **Gemini 2.5 thinking-token consumption.** Caught the silent-truncation issue: with `max_output_tokens=80`, Gemini 2.5 Flash burned 73 tokens on internal "thinking" and emitted only 3 visible tokens, producing a 3-word "summary." Added `thinking_budget` config field to [`gemini_llm`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/gemini_llm); setting `thinking_budget: 0` for short structured outputs frees all the budget for visible response.
3. **Failed Doc rows dropped columns.** Original [`google_docs_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/google_docs_extractor) only wrote `id/title/word_count` for failed rows, breaking downstream components that expected a `text` column. Now writes `text=None` / `headings=None` consistently.

## Asset graph in the UI

`uv run dg dev` → http://localhost:3000 shows the chain. Click any asset to see metadata: row counts, model, preview tables, and the actual extracted text / summary.

## Drop-in extensions

The same pattern composes with any AI/transform component:

```yaml
# Replace gemini_llm with openai_llm or anthropic_llm — same field shape
type: dagster_component_templates.OpenAILLMComponent
attributes:
  asset_name: doc_summaries
  upstream_asset_key: doc_texts
  api_key: ${OPENAI_API_KEY}
  model: gpt-4o-mini
  system_prompt: "..."
  input_column: text
  output_column: summary
```

```yaml
# Or chunk + embed for RAG
type: dagster_component_templates.DocumentChunkerComponent
attributes:
  upstream_asset_key: doc_texts
  source_column: text
  chunk_size: 500
  chunk_overlap: 50
```
