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
| [`json_flatten`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/json_flatten) | A column of JSON strings | Flat columns with `.`-joined keys (max_depth configurable) |
| [`json_path_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/json_path_extractor) | A column of JSON strings | Named columns extracted via JSONPath expressions |
| [`nested_field_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/nested_field_extractor) | A column of dict objects (already-parsed) | Named columns extracted via dotted paths |
| [`xml_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/xml_parser) | A column of XML strings | Named columns extracted via XPath expressions |
| [`html_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/html_parser) | One or more HTML columns | Stripped-text or specific-tag columns (BeautifulSoup) |
| [`regex_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/regex_parser) | A single column | Capture-group columns from a regex pattern |

## Cost

**$0.** Pure local — pandas + stdlib + BeautifulSoup + lxml. No network.

## Run it

```bash
./setup_text_extraction_demo.sh
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

- **JSON column with deep nesting** → [`json_flatten`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/json_flatten) (auto-flatten) or
  [`json_path_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/json_path_extractor) (cherry-pick specific paths).
- **Already-parsed dict column** → [`nested_field_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/nested_field_extractor) (skips the
  JSON parse, useful when upstream already gave you a dict).
- **XML feeds** → [`xml_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/xml_parser) with XPath. Add `namespace:` for
  namespaced XML.
- **HTML scraped pages** → [`html_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/html_parser) with `mode: strip_tags` for
  raw text, or with explicit `tags: [h1, p]` to grab specific elements.
- **Regex capture groups** → [`regex_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/regex_parser) with `mode: extract` and
  `output_columns: [...]` mapping each group to a named column.
