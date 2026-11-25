# What the StreamSets skill still needs from the components session

Checked against `dagster-community-components` **0.10.188**. Most of the earlier asks now exist; this file keeps only what is open, and says for each existing component how far it has been checked.

## 1. Still needed

### 1.1 `data_masking`: hash a field's native bytes (verified against a real engine)
The component's README says the native-bytes request "doesn't hold" because StreamSets' open-source `FieldHasherProcessor.java` hashes `getValueAsString()`. **The current engine does not do that.** Data Collector 6.4.1, MD5 Field Hasher, in place, on a record `{n: 42, f: 2.5, b: true, s: "42"}`:

| field | engine's hash equals MD5 of | not MD5 of |
|---|---|---|
| `n` (INTEGER 42) | 4 little-endian bytes `2a 00 00 00` (`9824a703...`) | the text `42` |
| `f` (DOUBLE 2.5) | 8 little-endian bytes (`1ac0a424...`) | the text `2.5` |
| `b` (BOOLEAN) | 1 byte `01` (`55a54008...`) | the text `true` |
| `s` (STRING "42") | UTF-8 bytes (`a1d0c6e8...`) | |

`data_masking` (0.10.188, `algorithm: md5`) agrees on strings and **differs on all three non-string types**. The test is `tests/test_sdc_registry_crypto_parity.py::test_field_hasher_hashes_native_bytes_and_the_registry_masker_does_not` (an xfail until the component changes; it needs a running Data Collector). Ask: an option (for example `encoding: streamsets`) that hashes integers as 4 little-endian bytes (8 beyond 32 bits), doubles as 8, booleans as 1, strings as UTF-8, taking the width from the dtype. Without it, hashes of non-string columns cannot be joined against hashes StreamSets already wrote.

### 1.2 `script_evaluator`: Groovy
Groovy is explicitly not supported (needs a JVM and a Groovy distribution). The skill runs Groovy through its own JVM runner, so this stays in the skill unless the component grows an optional JVM path.

### 1.3 `dataframe_to_kafka`: a partition strategy
`topic_column` and `key_column` now exist. The Kafka Producer destination also has a partitioner setting; the component has none.

### 1.4 JDBC table-pattern replication with per-table offsets
Still not confirmed covered: the JDBC Multitable origin takes table patterns (`%`) with an offset column per table, discovered at run time. Either `sql_to_database_asset` / `sling_replication_asset` cover a `tables:` pattern with a watermark per table (say which), or add it.

### 1.5 `polars_pipeline`: JSON sinks
`sinks:` still take only csv, parquet, avro and table (`ndjson`/`json` only for `encrypted_file`), so JSON output needs `dataframe_to_json` as a second asset. A `kind: ndjson` sink would remove that.

## 2. Built, and how far each was checked

| Component (0.10.188) | Checked how | Result |
|---|---|---|
| `field_encryption` (`compatibility: streamsets`) | **Real Data Collector 6.4.1, both directions** (`tests/test_sdc_registry_crypto_parity.py`) | engine ciphertext is read by the component with types preserved; the component's ciphertext is read by the engine. KMS not exercised |
| `data_masking` Field Masker modes (fixed, variable, custom, regex with groups) | README read; not run against the engine | the described behavior matches what the skill recorded |
| `base64_transform`, `script_evaluator` (JavaScript, Jython), `field_merger`, `text_to_columns` (Splitter), `array_exploder` (`move_exploded_to_end`), `json_flatten` and `dataframe_flatten_nested_columns` (`l.0` naming), `regex_parser` (Common/Combined), `lookup` | README and field list read; not run | each states the StreamSets behavior the skill recorded. Unchecked here |
| `per_row_http_fetcher` (`body_template`, auth, retry), `dataframe_to_kafka` (`topic_column`, `key_column`), `salesforce_ingestion` (`soql_query`, `initial_offset`) | field lists read; not run | the asks in the earlier version are present |
| `postgres_cdc_monitor`, `sqlserver_cdc_monitor`, `oracle_cdc_monitor` | field lists read; not run | siblings of `mysql_cdc_monitor` |
| `polars_pipeline` new ops (`explode`, Java-pattern `to_datetime`, `coalesce`, `route`), `inline` and `sql` sources, append and partitioned sinks | README read; `route`-free pipelines run through `--dataframe-route` tests | the Data Collector dataframe route runs `drop`, `unique`, `cast`-class ops for real |

## 1b. Adoption status of the 0.10.187 components in the converter

| Component | Status | Why |
|---|---|---|
| `streamsets_engine` | **emitted** (`convert --engine-tier1`), loaded in demo mode in tests | whole-pipeline, no dataframe needed |
| `dataframe_to_http`, `dataframe_to_kafka` | **emitted** for the simple case | a pipeline whose only destination is an HTTP Client (POST/PUT, static URL and headers, NONE/BASIC auth, JSON) or a Kafka Producer (static topic and brokers, JSON) is split in two assets: the record graph returns a DataFrame and the component sends it. Tested end to end against a local web server. Anything dynamic stays in the graph |
| `polars_pipeline` for Data Collector pipelines | **opt-in** `convert --dataframe-route` (`sdc_frame.py`) | linear pipeline: raw-data or Directory origin with a header, Field Remover/Renamer/Type Converter(basic)/Dedup/replace-if-null, Local FS CSV or JSON destination. Run for real in tests; NOT compared with a real Data Collector (error records, header attributes, rolling) |
| `geoip_lookup`, `mysql_cdc_monitor`, `scd_type_*` | not emitted | Geolocation is a mid-pipeline processor (splitting there is a bigger change); the CDC sensor needs a CDC apply asset; no Transformer export or definition has SCD or the Transformer Kafka/HTTP destinations, so their property names are unverified |

## 2. Decision for the components session

**Should the skill emit components instead of its own record-graph stages for stateless transforms?** It plans this already (`components_plan.py`) but generates only the Transformer route. If yes, each candidate component needs to match the
behaviour the skill verified; these are the points where StreamSets differs from the obvious implementation:

| StreamSets stage | Verified behaviour | Candidate component |
|---|---|---|
| Splitter | a value with fewer parts than split fields is a **record error** | `text_to_columns` |
| Field Flattener | list elements named `l.0`, `l.1`; in-place flattening stays inside the field; root target drops the parent name | `json_flatten`, `dataframe_flatten_nested_columns` |
| Field Merger | maps or lists only; the source is **moved**; lists append; a map key clash is an error unless overwrite | none |
| Field Replacer (6.x) | the replacement is an expression with a typed result; missing field: add / continue / error | `find_replace`, `data_cleansing` |
| List Pivot | the pivoted field moves to the **end** of the record | `array_exploder` |
| Dedup | first occurrence out, repeats to a second output | `unique_dedup` |
| Local Lookup | a missing key leaves the field **unset** | `lookup` |
| Log Parser | Common/Combined formats use grok field names (`clientip`, `verb`, `response`, `bytes`); parsing into `/` **replaces** the record | `regex_parser` |
| XML Parser | attributes `attr|name`, text `value`, children as lists of maps | `xml_parser` (XPath) |
| JDBC Lookup | modes FIRST_ONLY / SPLIT_INTO_MULTIPLE_RECORDS / ALL_AS_LIST; missing: pass on or error | `lookup`, `warehouse_join` |
| Slowly Changing Dimension (Transformer) | types 1 and 2 | `scd_type_1`, `scd_type_2` (exist; **not emitted**: no Transformer SCD stage exists in any export or definition set available, so its property names are unverified) |

If the answer is no, the skill keeps generating these as record-graph stages and none of §2 is needed.
