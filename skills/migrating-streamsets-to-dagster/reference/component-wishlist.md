# What the StreamSets skill still needs from the components session

Checked against `dagster-community-components` **0.10.187**. Everything the earlier tally asked for that now exists is not listed here: HTTP and Kafka sinks, GeoIP lookup and its
database updater, a MySQL binlog micro-batch sensor, offset reads on the Mongo and Elasticsearch readers, CDC apply (`delete_column`) on the Delta and SQL sinks, Field Hasher modes in
`data_masking`, `ndjson` / `json` sinks on `polars_pipeline`, the StreamSets engine and workspace components. `scd_type_1` and `scd_type_2` also exist (the skill had missed them).
Each item below says what is missing and gives the behaviour **verified against a real Data Collector 6.4.1** (and 3.18.1) so it can be built to match.

## 1. Needed, in order of value

### 1.1 `field_encryption`: a StreamSets-compatible mode
The component says its StreamSets interop is unverified and uses its own plaintext layout (`{"t","v"}` JSON, base64 text, a per-field `field` context entry). It cannot read ciphertext StreamSets
wrote, and StreamSets cannot read its output. The real layout, observed from the stage in both directions (tests in the skill: `tests/test_sdc_crypto_parity.py`):

- an AWS Encryption SDK message; with the **User Supplied Key** provider the key provider id is **`JceMasterKeyProvider`**, the key id is the stage's *Key ID*, the key is the Base64 AES key (16/24/32 bytes);
- the **plaintext is the value's text in UTF-8** (`42`, `2.5`, `true`; a byte array stays bytes), and the **field type is in the encryption context** as `SDC_FIELD_TYPE` (`STRING`, `INTEGER`, `LONG`, `DOUBLE`, `BOOLEAN`, `DECIMAL`, `BYTE_ARRAY`), next to the user's own context entries;
- Data Collector writes (and reads) the older non-committing suites, so the Python SDK needs `CommitmentPolicy.FORBID_ENCRYPT_ALLOW_DECRYPT` to read and write it; output is raw bytes, not base64 text;
- the AWS KMS provider is `StrictAwsKmsMasterKeyProvider` with the same plaintext and context (not exercised: no AWS account).

Ask: a `compatibility: streamsets` option on the component that does exactly this. Reference implementation: `runtime.encrypt_value / decrypt_value` in the skill.

### 1.2 `data_masking`: type-native hashing and the missing mask modes
- Field Hasher: StreamSets hashes a field's **native bytes**, not its text: an integer as 4 little-endian bytes (8 past 32 bits), a double as 8, a boolean as 1, a string as UTF-8. Hashes of non-string columns only match old ones if the component does the same (`encoding: streamsets`, taking the width from the dtype). The `algorithm`, `hash_mode` and `field_separator` options are already there.
- Field Masker modes the README does not show: **regex with groups to show** (`(.*)([0-9]{4})`, show group 2; a value that does not match is **left as it is**), **custom** (`#` shows the character, any other character is the mask), **fixed length** (ten `x`), **variable length** (one `x` per character). Mask character is `x`.

### 1.3 `dataframe_to_kafka`: per-row topic and key
Only a fixed `topic` is documented. The Kafka Producer destination takes a **topic expression** (a topic per record), a message key, and a partitioner. Ask: `topic_column` (or a template), `key_column`, and a partition strategy.

### 1.4 CDC micro-batch sensors beyond MySQL
`mysql_cdc_monitor` covers MySQL and gives the `_cdc_op` / `before` / `after` contract. Still missing, with the same contract: **PostgreSQL** (logical replication, `wal2json` or `pgoutput`), **SQL Server** (change tracking), **Oracle** (LogMiner). Two of the 14 sample pipelines from a StreamSets trial are Oracle and PostgreSQL CDC, and CDC is the main thing that otherwise keeps a pipeline running in StreamSets.

### 1.5 A base64 transform
Encode and decode (standard and URL-safe) on bytes and strings. StreamSets' Base64 stage works on **byte arrays only** (a string field is a record error) and 6.x takes a list of (origin, result) field pairs. No registry component does base64 (`text_codec_convert_asset` converts character codecs).

### 1.6 A script evaluator
Groovy, JavaScript and Jython scripts are customer code. The skill transpiles JavaScript to Python (25 of 25 sampled scripts), runs Jython on CPython and Groovy on the JVM, but a component would own this. Behaviour learned from the real engine:
- scripts see **Java collections** for list and map fields (`list.add/size/get`, `map.put/get/containsKey`; `list.push` does not exist); Jython `/` on two integers is integer division;
- a `userParams` map is passed to the script (`sdc.userParams`); `state` persists across batches (and should across runs); records out, error records, and events to a second output; BATCH and RECORD modes; init and destroy scripts; `scriptErrorAsRecordError`.

### 1.7 `polars_pipeline` / `warehouse_pipeline` / `pyspark_pipeline`: ops and sources a StreamSets pipeline needs
The Transformer (Spark) pipelines convert to `polars_pipeline` steps today (join, aggregate, sort, filter, SQL expressions, CSV/Parquet/JSON sinks). Missing:
- **inline source** (rows or text in the YAML, for raw-data origins), a **`sql`/JDBC source kind** on `polars_pipeline`, and an **incremental source** (read since the last offset), since `warehouse_pipeline` supports only `upstream_table` ("inline SQL coming");
- ops: **`explode`**, **`to_datetime` with a Java-style pattern**, nested/struct paths in `select`/`drop`/`rename` (StreamSets fields are paths like `/a/b`), `coalesce`, a **multi-output routing step** (one frame to several by predicate with a default lane);
- sinks: an **Avro** write (an IBM sample pipeline writes Avro), an **append** write mode, directory/partitioned output (Spark writes a directory of parts);
- join **prefix/suffix** settings; Spark-SQL function compatibility in `with_columns`/`filter` (e.g. `monotonically_increasing_id()`); `output_type: pandas` should declare its `pyarrow` dependency (it fails with `No module named 'pyarrow'`).
- No Structured Streaming is needed: Dagster does not run unbounded streams, and most "streaming" pipelines are bounded or pollable.

### 1.8 Per-row HTTP with a request body
`per_row_http_fetcher` takes a URL from a column and writes the response back, with method and static headers. The StreamSets HTTP Client processor also sends a **request body built from the record** (an expression template), authenticates (basic, bearer, OAuth2 as `dataframe_to_http` does), retries, and writes the parsed response to a chosen field. Ask: `body_template`, the auth modes and retry from `dataframe_to_http`, and a response field path.

### 1.9 Salesforce: arbitrary SOQL with a stored offset
`salesforce_ingestion` takes `sf_objects`, a `where_clause` and an `incremental_field`. The StreamSets origin takes a **full SOQL query** (`SELECT Id, Name, ... FROM Account WHERE Id > '${OFFSET}' ORDER BY Id`) with an initial offset and offset column. If an explicit field list is not supported, add a `soql_query` with an `${OFFSET}` placeholder. (Subscriptions / push topics are unbounded and out of scope.)

### 1.10 JDBC table-pattern replication with per-table offsets
`sql_to_database_asset` has `watermark_column` for one table or query. The JDBC Multitable origin takes **table patterns** (`%`) with an offset column per table, discovered at run time. Ask: a `tables:` pattern that fans out to one asset (or one multi-asset) per matching table with its own watermark, or confirm `sling_replication_asset` covers it.

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
| Slowly Changing Dimension (Transformer) | types 1 and 2 | `scd_type_1`, `scd_type_2` (exist; the skill does not emit them yet) |

If the answer is no, the skill keeps generating these as record-graph stages and none of §2 is needed.
