# Text Codec Convert — sanitize multilingual text to ASCII

**Validated end-to-end** (pure Python). 10 multilingual support tickets → ASCII-sanitized via codec round-trip. Same component handles ANY codec pair: ASCII↔EBCDIC, UTF-8↔UTF-16, Windows-1252→UTF-8, Latin-1→UTF-8.

```
support_tickets               ← synthetic_data_generator (multilingual tickets)
       │
       └── tickets_ascii_sanitized   ← text_codec_convert_asset (utf-8 → ascii)
```

## Components covered (2)

| Component | What it does |
|---|---|
| [`synthetic_data_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/synthetic_data_generator) | `schema_type: support_tickets` — multilingual ticket text with German, Spanish, French content + embedded PII. |
| [`text_codec_convert_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/text_codec_convert_asset) | Convert text between codecs. Two modes: `string` (in-DataFrame text columns) and `file` (read file bytes, recode, write new files — the EBCDIC mainframe ingest pattern). |

## Live output

10 tickets sanitized. Sample diffs (BEFORE → AFTER):

```
BEFORE: Hi, my name is Klaus Müller and my email is klaus.muller@support.example.fr...
AFTER : Hi, my name is Klaus M?ller and my email is klaus.muller@support.example.fr...

BEFORE: Site is down for me — getting 502 errors since 9am EST.
AFTER : Site is down for me ? getting 502 errors since 9am EST.

BEFORE: El sistema está caído. Llámame al 555-6404 lo antes posible.
AFTER : El sistema est? ca?do. Ll?mame al 555-6404 lo antes posible.
```

The `?` characters are where non-ASCII codepoints (`ü`, `á`, `í`, em-dash `—`) couldn't survive the round-trip through ASCII.

## Two modes

| Mode | Use for | Knobs |
|---|---|---|
| `string` | Normalize text COLUMNS in a DataFrame before sink (this demo). Input is already Python str / Unicode. | `source_column`, `target_column`, `to_codec`, `errors` |
| `file` | The canonical **EBCDIC mainframe ingest** pattern: read a directory of files, recode bytes, write new files. | `source_path_column`, `output_dir`, `from_codec`, `to_codec`, `errors` |

## ASCII ↔ EBCDIC and other codec pairs

`from_codec` / `to_codec` accept any name Python's stdlib `codecs` module recognizes (~200 codecs):

| Use case | from_codec | to_codec |
|---|---|---|
| **EBCDIC → Unicode** (US z/OS) | `cp037` | `utf-8` |
| EBCDIC → Unicode (Germany) | `cp273` | `utf-8` |
| EBCDIC → Unicode (Latin-1) | `cp500` or `cp1047` | `utf-8` |
| Windows-1252 → UTF-8 | `cp1252` | `utf-8` |
| Latin-1 → UTF-8 | `latin-1` | `utf-8` |
| UTF-16 → UTF-8 | `utf-16` | `utf-8` |
| Strip non-ASCII (this demo) | `utf-8` (ignored in string mode) | `ascii` |

## `errors` modes

| Mode | Behavior |
|---|---|
| `strict` (default) | Raise on un-encodable / un-decodable char |
| `replace` (this demo) | Substitute `?` (encode) / `�` (decode) |
| `ignore` | Silently drop the char |
| `backslashreplace` | Escape as `\xNN` |

## File-mode example (EBCDIC ingest)

```yaml
type: dagster_component_templates.TextCodecConvertAssetComponent
attributes:
  asset_name: utf8_files
  upstream_asset_key: ebcdic_files     # DataFrame of {file_id, file_path}
  mode: file
  source_path_column: file_path
  from_codec: cp037      # IBM US EBCDIC
  to_codec:   utf-8
  errors:     replace
  output_dir: /tmp/utf8_out
```

This is what banking, insurance, and federal-agency systems use every day to ingest daily z/OS-exported flat files.

## Run it

```bash
./setup_codec_convert_demo.sh
cd codec-convert-demo
uv run dg launch --assets '*'
```

## Sister components

- [`dataframe_flatten_nested_columns`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/dataframe_flatten_nested_columns) — common pre-warehouse normalization (pairs with this)
- [`bigquery_load_from_gcs_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ingestion/bigquery_load_from_gcs_asset) — common downstream after EBCDIC → UTF-8
- [`hl7_v2_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/hl7_v2_parser), [`iso20022_payment_parser`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/iso20022_payment_parser) — common downstreams after mainframe codec conversion
