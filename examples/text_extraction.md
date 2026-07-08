# Text extraction — pull structured fields from semi-structured columns

**Validated end-to-end** — RUN_SUCCESS in seconds. The same `raw_events`
DataFrame fans out to 6 extraction transforms, each parsing a different
text-shaped column.

```
raw_events (synthetic source, 20 rows × 8 mixed-text cols)
       │
       ├── flattened_addresses     ← json_flatten
       ├── extracted_user_fields   ← json_path_extractor
       ├── extracted_address_parts ← nested_field_extractor
       ├── parsed_product_xml      ← xml_parser
       ├── parsed_html_content     ← html_parser
       └── extracted_phone_parts   ← regex_parser
```

## Components covered (6)

| Component | Input shape | Output |
|---|---|---|
| `json_flatten` | A column of JSON strings | Flat columns with `.`-joined keys (max_depth configurable) |
| `json_path_extractor` | A column of JSON strings | Named columns extracted via JSONPath expressions |
| `nested_field_extractor` | A column of dict objects (already-parsed) | Named columns extracted via dotted paths |
| `xml_parser` | A column of XML strings | Named columns extracted via XPath expressions |
| `html_parser` | One or more HTML columns | Stripped-text or specific-tag columns (BeautifulSoup) |
| `regex_parser` | A single column | Capture-group columns from a regex pattern |

## Cost

**$0.** Pure local — pandas + stdlib + BeautifulSoup + lxml. No network.

## Run it

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_text_extraction_demo.sh | bash
cd text-extraction-demo
uv run dg launch --assets '*'
```

Or open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

Each extraction asset lives in the `extracted` group, with the parsed
columns visible in the asset's preview metadata. Use these as inputs to
a downstream transformation or sink.

## Patterns to copy from this demo

- **JSON column with deep nesting** → `json_flatten` (auto-flatten) or
  `json_path_extractor` (cherry-pick specific paths).
- **Already-parsed dict column** → `nested_field_extractor` (skips the
  JSON parse, useful when upstream already gave you a dict).
- **XML feeds** → `xml_parser` with XPath. Add `namespace:` for
  namespaced XML.
- **HTML scraped pages** → `html_parser` with `mode: strip_tags` for
  raw text, or with explicit `tags: [h1, p]` to grab specific elements.
- **Regex capture groups** → `regex_parser` with `mode: extract` and
  `output_columns: [...]` mapping each group to a named column.

---

## Also: build this from natural language

The [`planned_catalog_agent`](./planned_catalog_agent.md) component can plan and cache this pipeline from a natural-language task — no defs.yaml per component needed. Drop the following into a single defs.yaml, run `dg utils refresh-defs-state` once, and the real component assets appear in your graph.

```yaml
type: dagster_community_components.PlannedCatalogAgentComponent
attributes:
  task: |
    Take the JSONPlaceholder posts API at
    https://jsonplaceholder.typicode.com/posts — it returns an array of 100
    posts with fields (userId, id, title, body). Ingest that endpoint, then:
      1. Flatten any nested JSON fields (in this case the top level is flat but
         demonstrate the transform anyway).
      2. Use a regex parser to extract the FIRST sentence from the body column
         into a new column called first_sentence.
      3. Also extract the number of words in each post's body into a column
         called word_count (use formula: body.str.split().str.len()).
    Write the enriched posts to /tmp/posts_enriched.csv.
  include_ids: ['rest_api_fetcher', 'file_ingestion', 'json_flatten', 'regex_parser', 'formula', 'dataframe_to_csv']
  llm_model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prefilter_llm: true
  prefilter_max_entries: 40
  max_iterations: 20
  defs_state:
    management_type: LOCAL_FILESYSTEM
    refresh_if_dev: false
```

Live-validated on gpt-4o-mini: **3/4 clean picks in 10s, ~$0.0028 total cost.** Outputs written: `/tmp/posts_enriched.csv`.

After the trajectory runs once, materialization is pure cached-plan execution — no LLM per run.
