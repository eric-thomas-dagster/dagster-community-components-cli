# StreamSets to Dagster mapping

What is translated, what is a stub, and what is *verified* versus *assumed*. Rule: nothing outside the translated list is guessed at; it becomes
a loud stub (`SDC-CAVEAT: ...`) that fails the run before any data is read.

## The model

| StreamSets | Dagster |
|---|---|
| Pipeline (Data Collector, one JSON) | One asset `ss_<pipeline>` (group `streamsets`). Stages run as a record graph inside the asset (`runtime.py`). |
| Origin / processor / destination / executor | A call into the runtime, or a stub that keeps its place in the graph |
| Lane (`outputLanes`, `inputLanes`) | Wiring between stage calls; a Stream Selector fans a record out to the lanes whose predicate is true |
| Event lane (`eventLanes`) | Event records. The S3 origin emits `finished-file`; a batch origin emits `no-more-data` when it is exhausted |
| Runtime parameters (`constants`) | `C[...]` in the generated code. Override with `SDC_PARAM_<NAME>`. Credential-named parameters are removed and read from `SDC_SECRET_<NAME>` |
| Offset (incremental state) | Asset metadata `sdc_offsets`, read from the previous materialization (S3 origin only so far) |
| Job in Control Hub (schedule, start/stop) | Not in a pipeline export. No schedule is generated; the report asks how the pipeline is triggered |
| Continuous job | Tier 1: the registry `StreamSetsWorkspaceComponent` observes and starts jobs. Tier 2 cannot run it: a Dagster run is a batch |

## Stage catalog (tier 2)

Translated (stage class = last `_` segment of `stageName`):

| Stage | Notes |
|---|---|
| `AmazonS3DSource` | DELIMITED (CSV/TDF/custom delimiter) and JSON lines. Emits `Name` and `finished-file` events. Objects are read in `LastModified` order and each run continues after the last one of the previous run. Post-processing (archive/delete) is **not** performed. Other data formats are stubs. |
| `TableJdbcDSource` | Full-table reads of each matching table pattern, `jdbc.tables` attribute set. **Offsets are not tracked**: every run reads every row (StreamSets resumes). Offset columns and partitioning make it a stub. |
| `ExpressionDProcessor` | Field and header expressions. Field attribute expressions are a stub. |
| `FieldFilterDProcessor` (Field Remover) | KEEP and REMOVE. Other operations are stubs. |
| `FieldFlattenerDProcessor` | SPECIFIC_FIELDS. ENTIRE_RECORD is a stub. |
| `SelectorDProcessor` (Stream Selector) | Predicates and `default`. |
| `FieldMapperDProcessor` | FIELD_NAMES and FIELD_VALUES with conditional and mapping expressions. Aggregation expressions and other modes are stubs. |
| `JdbcQueryDExecutor` | One query per record. Each query commits separately (batch commit is not applied). |
| `JdbcDTarget` | INSERT; fields are matched to columns by name and unmatched fields are dropped. CDC change-log formats and UPDATE/DELETE are stubs. |
| `NullDTarget`, `ToErrorNullDTarget`, `TrashDTarget` | Discard. |
| `PipelineFinisherDExecutor` | A batch run ends when origins are exhausted. |
| `SpoolDirDSource` (Directory) | GLOB, DELIMITED/JSON. **Polls forever in StreamSets**; one run reads the files present at the start. Last file's order key is stored as an offset. Archive/delete post-processing is not performed. |
| `ForceDSource` (Salesforce) | Query mode (`queryExistingData`, NO_REPEAT). `${OFFSET}` in the SOQL is replaced by the stored offset and the query repeats until it returns nothing; the last `offsetColumn` value is stored. REST query API (Bulk API setting not applied); needs `simple-salesforce`. Subscriptions are stubs. |
| `FieldTypeConverterDProcessor` | By field; BOOLEAN, BYTE, SHORT, INTEGER, LONG, FLOAT, DOUBLE, DECIMAL, STRING; exact paths and `/*`. Date/time targets are stubs. Locale, scale and rounding are not applied. |
| `FieldRenamerDProcessor` | Literal paths; TO_ERROR/CONTINUE for a missing source, TO_ERROR/REPLACE for an existing target. Regular-expression sources are stubs. |
| `FieldOrderDProcessor` | LIST_MAP, TO_ERROR for missing, TO_ERROR/DISCARD for extra fields. |
| `SplitterDProcessor` | Separator is a regular expression; TO_LAST_FIELD; keep or remove the original. |
| `DataParserDProcessor` | JSON string field, first value only. |
| `JythonDProcessor` | BATCH and RECORD modes, NATIVE_OBJECTS, init/destroy scripts. **Runs unchanged under CPython**, with `sdc.records`, `sdc.output.write(record[, lane])`, `sdc.error.write`, `sdc.state`, `sdc.log` and the globals `records output error log state record`. Python 2 syntax, Java imports and Jython-only calls fail at run time with the interpreter's error. |
| `JavaScriptDProcessor` | BATCH mode, NATIVE_OBJECTS. **Transpiled to Python** (see *Scripting*); if the transpiler refuses, runs in **QuickJS (ES2020), not Nashorn**: `records`, `output.write`, `error.write`, `state`, `log`, `sdcFunctions.createRecord/createMap`, `sdc.createEvent/toEvent` (events go to the stage's event lane). Records cross as JSON (dates, decimals become strings); Java objects (`Java.type`, `new java.*`) do not exist and fail at run time. Init script runs once. |
| `JythonDSource` | Runs under CPython with `sdc.userParams`, `sdc.lastOffsets`, `sdc.createBatch/createRecord`, `batch.add/process`, `sdc.isStopped`. **StreamSets runs the script until the pipeline stops; here one run is one poll** (the loop ends after the first processed batch); offsets are stored. Schedule the asset at the script's interval. |
| `LocalLookupDProcessor` | Static key/value table; RECORD and BATCH give the same result. A missing key sets the field to null (assumed). |
| `HttpDProcessor` (HTTP Client processor) | JSON; GET/POST/PUT/PATCH/DELETE/HEAD; EL in URL, headers and body; NONE and BASIC auth; response parsed into the output field. No pagination (stub). Retry/backoff, TLS, proxy and response-header capture are not applied. HTTP error status sends the record to the error policy. |
| `HttpClientDTarget`, `AnodotDTarget` (vendor) | JSON POST/PUT etc.; one request per batch (ARRAY_OBJECTS: a JSON array; otherwise one object per line) or per record. The response is ignored; an error status sends the batch's records to the error policy. The Anodot stage is translated as the HTTP POST it sends; its agent-offset callback is not performed. |
| `FileTailDSource` | LOG data format only. One run reads the lines added since the last run (byte offset stored); a partial last line waits for the next run; rolled files are not followed. Attributes `offset`, `filename`, `file`, `mtime`. |
| `RawDataDSource` (dev) | JSON objects or delimited text, emitted once. |
| `GeolocationDProcessor` | One GeoIP2 **City** `.mmdb` (path from the stage, or `SDC_GEOIP_DB`); LATITUDE, LONGITUDE, COUNTRY_NAME, COUNTRY_ISO_CODE, CITY_NAME, POSTAL_CODE; an unknown address is a record error. Needs `maxminddb`. |
| `FieldMaskDProcessor` | VARIABLE_LENGTH, FIXED_LENGTH (ten `x`), REGEX with groups to show. Mask character `x`; a value that does not match a regex is masked entirely (assumed, fail-closed). CUSTOM is a stub. |
| `FieldHasherDProcessor` | Hash **in place** with MD5, SHA1, SHA256, SHA512 (lower-case hex of the UTF-8 string form; maps and lists element by element). Hash to target / header / whole record, and MURMUR3_128, are stubs. Property names come from one real (old-schema) export. |
| `MultiKafkaDSource` | JSON values, no key capture. One run reads from the group's committed offsets until a poll returns nothing, and commits only after the run succeeds. Records carry `topic`, `partition`, `offset`. Tested against a fake client only. |
| `KafkaDTarget` | JSON, one message per record, topic from the stage (or the topic expression when runtime resolution is on). Partitioner and serializer settings are not applied. Tested against a fake client only. |
| `ElasticsearchDSource` | Micro-batch: `${OFFSET}` in the query body is replaced by the stored offset (raw text), the query repeats until no hits, and the last hit's offset field is stored. Connection through the registry's `ElasticsearchResource` (credentials `SDC_SECRET_<STAGE>_API_KEY` or `_USERNAME`/`_PASSWORD`); first URI only. |
| `ElasticSearchDTarget` (transport-protocol stage) | Bulk index over HTTP through the registry resource, **so `SDC_ES_URL` must point at the HTTP endpoint** (the stage used port 9300); no document type. |
| `MongoDBDSource` | Documents with the offset field greater than the stored offset, in order; OBJECTID (initial offset date becomes an ObjectId), STRING and DATE offsets. The connection string is removed from the export: `SDC_SECRET_<STAGE>_CONNECTION_STRING`. Connection through the registry's `MongoDBResource`. Tested against mongomock. |
| `HttpClientDSource` | Polling mode, no pagination, JSON or DELIMITED; one request per run (schedule at the polling interval). |
| `GroovyDProcessor` | Runs on the **real Groovy (JVM)**: the `groovy` command (JDK + Apache Groovy) on PATH or in `SDC_GROOVY`; one process per batch; records cross as JSON; `sdc.records/output/error/log/state/createRecord/createEvent/toEvent` and the globals `records output error log state` (`record` in RECORD mode). **Not yet run against a JVM**: the tests use a stand-in executable for the wiring, and `test_the_real_groovy_script_from_the_aws_sample_matches_the_hand_ported_one` runs the export's own script and compares it with the Python port as soon as `groovy` is available. |
| `JdbcLookupDProcessor` | Per-record query (EL substituted into the SQL text, as StreamSets does); FIRST_ONLY / SPLIT_INTO_MULTIPLE_RECORDS / ALL_AS_LIST; PASS_RECORD_ON / SEND_TO_ERROR; column mappings `columnName` -> `field` (none: every column to a same-named field). **The mapping property names are not verified** (no export with mappings was found). No cache. |
| `DevSimulatorDSource` | Delimited files replayed once, unpaced, routed to lanes by discriminator value, event timestamp = run start + relative delay. Dev stage. |
| `Base64EncodingDProcessor`, `Base64DecodingDProcessor` | Bytes in, bytes out (a string input is UTF-8); URL-safe option. |
| `FieldValueReplacerDProcessor` | Replace-if-null only (`fieldsToReplaceIfNull`); the value is set as written. Replace-with-null and conditional replacement are stubs. |
| `ListPivotDProcessor` | One record per list element; copy other fields; target path. "Save original field name" is not handled. |
| `DeDupDProcessor` | All fields or specified fields; duplicates to the second output; record-count window **for one run** (nothing carried across runs); time window not applied. |
| `JsonParserDProcessor`, `LogParserDProcessor` | JSON string field to a map; Apache Common/Combined/grok log line to a map (`/` merges into the record). |
| `XMLFlatteningDProcessor` | As IBM documents it, including its examples (tested against them): path names joined by the field delimiter, `#` for attributes, `name(0)` for repeated siblings, one record per delimiter element. Namespace URIs are dropped. |
| `FieldFlattenerDProcessor` (ENTIRE_RECORD) | Every leaf to a top-level field named by its path (`a.b`, `c[0]`); naming not verified against a Data Collector. |
| `LocalFileSystemDTarget` | DELIMITED, JSON (one object per line), TEXT. One file per directory per run (`<prefix>_<uuid>`); no rolling, compression or late-record handling. |
| `DatabricksDeltaLakeDTarget` | SQL writes through SQLAlchemy (`databricks-sqlalchemy`; the URL is derived from the JDBC URL, or set `SDC_SQLALCHEMY_URL_<STAGE>`). Auto-creates the table from the first record, adds columns when drift is on, and in CDC mode applies `sdc.operation.type` by key as delete-then-insert. **StreamSets stages files and runs COPY INTO / MERGE**; this is slow for bulk loads. A CDC stage with no key columns is a stub. |

Stubs with a pointer to the registry (check `dagster-community-components` before hand-building):

| StreamSets | Instead |
|---|---|
| `ForceDSource` (Salesforce) | `SalesforceIngestionComponent` (dlt's verified Salesforce source) |
| `KafkaDSource`, other streaming origins | Keep running in StreamSets (tier 1) or `KafkaToDatabaseAssetComponent` with `kafka_monitor` for a micro-batch |
| `DatabricksDeltaLakeDTarget` | `DataframeToDeltaTableComponent`, or a Databricks SQL step |
| Table-to-table replication (multitable origin to a warehouse) | `SlingReplicationAssetComponent` (bring a Sling replication YAML) |
| `AmazonS3DSource` to a table | `S3ToDatabaseAssetComponent` with `s3_monitor` |
| `HdfsDTarget`, Hive stages | Object storage or the warehouse; no Hadoop in the target |
| Encrypt / decrypt fields | `runtime.crypto_fields` / `encrypt_value` / `decrypt_value` via `--override`; see *Encryption* below |
| Groovy / JavaScript evaluators | Rewrite as Python and pass it with `--override`. The script text is kept in the manifest notes |
| Jython evaluator | Translated (runs under CPython); see the catalog |

Other stage types not translated: JDBC Tee, HBase and Redis lookups, the Spark processor, Aggregator, Delay, Kafka single-topic consumer, the Encrypt and Decrypt Fields stage (see Encryption). Also not translated: Transformer/Spark stages
, MongoDB, REST client, Kafka producer, ...) have no translation yet and are reported as stubs.

## Expression language (EL)

Translated: `${...}` with `record:value/valueOrDefault/attribute/attributeOrDefault/exists/eventType`, `str:` (`toLower toUpper trim length substring
concat replace replaceAll contains startsWith endsWith matches regExCapture indexOf truncate isNullOrEmpty`), `math:` (`abs ceil floor round max min`),
`time:now`, `file:` (`fileName parentPath pathElement removeExtension`), `f:` (Field Mapper context), `uuid`, `YYYY() MM() DD() hh() mm() ss()`,
runtime parameters by name, `SECONDS MINUTES HOURS DAYS`, the operators `== != < > <= >= && || ! ? : + - * / %` and `eq ne lt gt le ge and or not empty div mod`.
Any other function raises Untranslatable and the stage becomes a stub.

## Scripting: JavaScript, Jython and Groovy

Order of preference: **transpile to Python**, then a native component, then run the script in an engine.

| Evaluator | What the converter does | Fallback |
|---|---|---|
| JavaScript | **Transpiled to a Python module** (`scripts/<stage>.py`, via `jstopy.py`; JavaScript semantics such as truthiness, `+`, `==` versus `===`, `undefined` versus `null`, string and array methods come from `jsrt.py`). No JavaScript engine at run time. A construct the transpiler does not handle (`this`, getters, destructuring, labels, `new Date`, `x++` inside an expression, ...) raises *Unsupported* and the stage runs in QuickJS instead, with the reason in the report. | QuickJS (in process). StreamSets uses Nashorn (ES5.1); records cross as JSON, so dates, decimals and bytes arrive as strings; Java objects do not exist. |
| Jython | Runs under CPython (Python 2 syntax and Java imports fail). | none |
| Groovy | Runs on the real Groovy (JVM): `groovy` on PATH or `SDC_GROOVY`. There is no Groovy transpiler: it could not be validated without a JVM. | the script-evaluator component (ask #15) |
| Custom Java stages | Not translatable. | rebuild by hand |

How the JavaScript transpiler is checked: `tests/test_js_transpile.py` runs 33 snippets in QuickJS and as transpiled Python and compares output records, error counts, events and state; 40 tests in all.
With `SDC_JS_CORPUS=<folder of exports>` it also compares every real script that transpiles on random inputs: **all 25 scripts in the 27-pipeline sample transpile, and all 25 match QuickJS** (on random inputs only 11 produced output records and 9 produced error records, so this is
evidence, not proof). The generated Python is meant to be read and kept: review it like inherited code.

All evaluators get the scripting objects (`records`, `output.write`, `error.write`, `state`, `log`, `sdcFunctions` / `sdc.createRecord`, `sdc.createEvent/toEvent`) and keep `state` across batches within a run.

## Encryption, decryption and hashing

**Encrypt and Decrypt Fields is translated, and its ciphertext is compatible in both directions** (checked against Data Collector 6.4.1: `tests/test_sdc_crypto_parity.py`).
What the real stage does, as observed:

- Ciphertext is a standard **AWS Encryption SDK message** (default suite `AES_256_GCM_IV12_TAG16_HKDF_SHA384_ECDSA_P384`, which is a *non-committing* suite).
- With the **User Supplied Key** provider, the key provider id is `JceMasterKeyProvider`, the key id is the stage's *Key ID*, and the key is the Base64 AES key (16, 24 or 32 bytes).
- The **plaintext is the value's text in UTF-8** (`42`, `2.5`, `true`), and the **field type is in the encryption context** as `SDC_FIELD_TYPE` (STRING, INTEGER, LONG, DOUBLE, BOOLEAN, DECIMAL, BYTE_ARRAY), next to your own context entries and the SDK's signing key. Decryption restores the type from there.
- The Python SDK refuses non-committing suites by default; reading and writing StreamSets-compatible data needs `CommitmentPolicy.FORBID_ENCRYPT_ALLOW_DECRYPT`. `runtime.py` sets that and says why. It is a deliberate weakening of the SDK's key-commitment default, for compatibility only; a project that
  only ever writes new data can use the SDK default and a committing suite.

So **data StreamSets encrypted can be read after the move, and data the converted code encrypts can be read by StreamSets**, given the same key, key id and context. `runtime.encrypt_value`, `decrypt_value` and the stage `crypto_fields` implement this; the translated stage
reads the Base64 key from `SDC_SECRET_<STAGE>_KEY` (the export drops it).

Not covered: the **AWS KMS** provider is translated (the SDK's KMS provider is interoperable by design) but was not exercised, since there was no AWS account in the tests; **date and time fields** are not supported yet (their plaintext form was not checked); the `cipher` setting is not applied
(the SDK's default suite is used; decryption reads any suite); data-key caching and frame size have no effect.

Other stages in this family: **Field Hasher** is translated for in-place and target-field/header hashing with MD5, SHA1, SHA256, SHA512 (hash of the field's **native bytes**: integers as 4 little-endian bytes, 8 past 32 bits; doubles as 8; strings as UTF-8; checked against 3.18.1 and 6.4.1);
whole-record hashing and MURMUR3_128 are stubs. **Field Masker** is translated for variable length, fixed length (ten `x`), custom (`#` shows the character) and regex (groups to show; a value that does not match is left as it is).

## Verified against a real Data Collector (3.18.1)

`tests/test_sdc_parity.py` feeds the same records through the same stage configuration in a running Data Collector (REST preview) and in the converted runtime. 14 cases pass.
The first run found **six real differences, all fixed**, and confirmed a number of assumptions:

| Found by running the real engine | Fixed |
|---|---|
| Splitter: a value with fewer parts than split fields is a **record error** (not nulls) | yes |
| Field Flattener names list elements with the separator (`l.0`), not `l[0]`; `flattenInPlace` flattens inside the field (`o = {k, "d.deep"}`) | yes |
| Field Masker REGEX: a value that does not match is **left as it is** (not masked) | yes |
| Field Hasher hashes a field's **native bytes**: an integer as 4 little-endian bytes (8 past 32 bits), a double as 8, a string as UTF-8; the property names are the `hasherConfig.*` schema | yes (in place and to a target field/header) |
| Base64 works on **byte arrays only**: a string field is a record error | yes |
| List Pivot puts the pivoted field at the **end** of the record | yes |
| Dedup adds its own `Info[...].hash` header attribute (ignored by the comparison) | noted |
| Field Merger: both fields must be maps or both lists (else a record error); the source field is **moved**; lists append after the target's items; maps merge and a clashing key is a record error unless Overwrite is on | yes (new stage translation) |
| XML Parser output shape: attributes as `attr|name`, text as `value`, children grouped by tag into **lists of maps**; no delimiter gives the document's content; a delimiter with FIRST_ONLY, ALL_AS_LIST or SPLIT_INTO_MULTIPLE_RECORDS | yes (new stage translation) |
| Field Replacer (6.x, replaces Field Value Replacer): `replacement` is an **expression evaluated per record** with a typed result; ADD_FIELD / CONTINUE / TO_ERROR for a missing field | yes (new stage translation) |
| Local Lookup: a key with no entry leaves the field **unset** (not null) | yes |
| Log formats: Common and Combined Log Format use the **grok field names** (`clientip, ident, auth, timestamp, verb, request, httpversion, rawrequest, response, bytes`; combined adds `referrer`, `agent`, quotes included); parsing into `/` **replaces** the record; a group that did not match is null. The 6.x service property names are `logParserServiceConfig.*` (grok patterns in `grokPatternList`) | yes |
| Scripting stages: `userParams` (script parameters, `sdc.userParams`) exists on Jython, JavaScript and Groovy; in JavaScript (Nashorn) **record lists and maps are Java collections** (`list.add`, `size`, `get`; `map.put`, `get`, `containsKey`) and `list.push` does not exist; in Jython `/` on two integers is **integer division** | yes (all three supported; the Jython division is emulated by rewriting `/`) |
| **JDBC and S3 stages against real Postgres and MinIO** (5 cases): the multitable origin, JDBC Lookup (mappings `columnName`/`field`, FIRST_ONLY / SPLIT_INTO_MULTIPLE_RECORDS / ALL_AS_LIST, SEND_TO_ERROR / PASS_RECORD_ON), the JDBC destination (columns matched by name, extra fields dropped) and the JDBC Query executor give the same rows and side effects; the S3 origin reports `Name` as the full object key, sets the object's metadata as record attributes (`ETag`, `Content-Length`, `Content-Type`, `Last-Modified`) and emits `new-file` and `finished-file` events | yes (attributes and events aligned; custom endpoints and path-style addressing added) |
| **6.x property names moved**: JDBC connection settings are `hikariConfigBean.connection.connectionString / .username / .password` (older: `hikariConfigBean.connectionString`); JDBC destination, lookup and executor settings sit under `jdbcTargetConfigBean.*`, `jdbcLookupProcessorConfigBean.*` and `config.*`; S3 credentials and endpoint under `s3ConfigBean.s3Config.connection.awsConfig.*` / `awsRegionConfig.nonRegionalEndpoint`; Databricks under `config.destinationConfig.connection.*`; Base64 `fieldsToEncode` / `fieldsToDecode`; Field Hasher `hasherConfig.*`; Field Replacer `conf.rules`. The translators read both generations | yes (the first IBM trial export showed a 6.x JDBC stage being refused for exactly this) |
| Confirmed: Expression Evaluator arithmetic, `/` versus `div`, string concatenation with null, ternary, `substring`, `record:exists`, missing fields, header attributes; `str:matches` and `str:regExCapture` are **whole-string** matches; Field Remover (KEEP/REMOVE, nested paths), Field Renamer, Field Type Converter (numbers, boolean, long), Stream Selector with a default lane, Field Order, Dedup, JSON Parser, Masker (variable, fixed, custom) | n/a |

Data Collector also publishes its own **stage definitions** (`/rest/v1/definitions`, readable without activation): every property name, default and enumeration of every installed stage and the full
EL function catalog (299 functions). Property names in the translators were checked against it for the stages installed in the 6.4.1 image, which found the Field Hasher schema change. `docs/PARITY.md` has the setup.

## Which pipelines can run in Dagster at all

Each pipeline is classified in the report (`workload:` line, and `shape` in the manifest):

| Shape | Meaning | What happens |
|---|---|---|
| **batch** | bounded origins (files, tables, queries); a Transformer pipeline in STREAMING mode over file origins counts | converts and runs as a Dagster run |
| **polling** | continuous in StreamSets but readable as micro-batches: Directory, File Tail, Kafka consumer, Elasticsearch, MongoDB, HTTP polling, Jython origin | converts; schedule the asset at the polling interval |
| **unbounded** | a stream that never ends with no micro-batch reader here: CDC (Oracle, PostgreSQL, SQL Server, MySQL binlog), HTTP/TCP/UDP/WebSocket servers, queues | stays in StreamSets (tier 1, observe it) or is redesigned; a Dagster run is a batch |

On the 14 IBM trial pipelines: 10 batch, 1 polling, 2 unbounded (both CDC); three of the four Transformer pipelines are in BATCH mode and the fourth reads files. Most "streaming" estates are mostly batch or polling.

## Transformer (Spark) pipelines: registry components

A Transformer pipeline is a DataFrame job, so the converter emits it as `defs.yaml` for the registry's **`polars_pipeline`** (named steps with file sources, `ref` joins, csv/parquet sinks) and **`dataframe_to_json`** (JSON output), one asset per destination carrying only the steps that feed it
(`components_emit.py`; tested end to end in `tests/test_transformer_components.py`: the generated project is loaded by Dagster, run, and its CSV and JSON output compared).

| Transformer stage | Component op |
|---|---|
| File origin (JSON, CSV, Parquet) | `source: {kind: file, path, format}` (JSON is read as JSON Lines, Spark's default) |
| Join (column list) | `join` with `right: {ref}` (INNER/LEFT_OUTER/RIGHT_OUTER/FULL_OUTER) |
| SQL Evaluator | `with_columns` expressions (Spark SQL run by Polars' SQL: functions can differ, e.g. `monotonically_increasing_id()` is Spark-only) |
| Selector | one `filter` step per lane; `default` becomes NOT of the other predicates |
| Aggregate | `group_by` with `aggregations` (AVG, SUM, COUNT, MIN, MAX) |
| Sort | `sort` |
| Repartition | dropped |
| File destination CSV / Parquet | `sinks: [{kind: csv|parquet, path}]` |
| File destination JSON | `dataframe_to_json` (JSON Lines) |

Not covered, so the pipeline falls back to the record graph and its Spark stages become stubs: **Slowly Changing Dimension** (no `scd` op), **Scala** evaluator, raw-data origins (no inline source), other file formats, join conditions that are not column lists.
Differences stated in the report: Spark writes a directory of part files, this writes one file; write mode APPEND is not applied; join column prefixes are not applied; a streaming-mode pipeline runs as a batch.
`pyspark_pipeline` is the Spark-faithful alternative when Spark semantics matter more than a lightweight run.

## Verified and not verified

Property names for Base64, Field Value Replacer, List Pivot, Dedup, JSON/Log/XML parsers, Field Merger and Jdbc Lookup (`query`) come from Data Collector's own default-config test pipelines (versions 1.1 to 2.2); defaults prove names, not behaviour, so each behaviour comes from IBM's documentation or is flagged above.

Verified against the six real Data Collector exports in `tests/fixtures/sdc` (schemaVersion 6): the envelope (`pipelineConfig`, `stages`,
`inputLanes/outputLanes/eventLanes`, `configuration` as name/value pairs, `services[].configuration` for data formats, `constants`, `badRecordsHandling`),
and the configuration property names of every translated stage above.

Not verified, so check on a real run before trusting parity:

- `str:regExCapture` and `str:matches` are implemented as whole-string matches. Confirm against the customer's Data Collector version.
- The S3 origin's `Name` header is taken to be the object key including the prefix (the sample pipelines' regexes only work if it is).
- That a batch origin's `no-more-data` event arrives after the last batch; and that event-lane records reach a stage only after the origin's last batch of that file.
- Error handling: `TO_ERROR` records are counted (asset metadata `error_records`) but not written anywhere; `STOP_PIPELINE` raises.
- Field Flattener with a root target (`/`) names the flattened fields without the parent's name (`/Data/id` becomes `/id`); with another target it prefixes the parent. The CDC-to-Delta sample only makes sense that way, but it is inferred from the pipeline, not from documentation.
- The MySQL binary-log origin's record shape (`/Table /Type /Data /OldData`, header `sdc.operation.type`) in `tests/overrides/cdc_feed.py` is assumed from the sample pipeline's field names. No binlog reader is translated.
- The Delta Lake stage's behaviour beyond the SQL-write translation (null defaults, newline replacement, trimming, COPY INTO atomicity) is not reproduced.
- Jython and JavaScript scripts run under CPython / QuickJS; whether a given script behaves the same is a per-script review. 19 of the 25 JavaScript scripts in the 27-pipeline sample run on a trivial record; the other 6 raise on missing input fields or an unsupported API, so they were not shown to be correct.
- HTTP: `ARRAY_OBJECTS` is taken from a destination's own `jsonMode`; the `MULTIPLE_OBJECTS` form (one object per line) and the response handling are assumed.
- Transformer (Spark) pipelines load, but none of their stages is translated, and no real Transformer export was available.
- Control Hub export archives are read (`.zip` of pipeline JSON); the Control Hub job and schedule objects are not parsed.
- Kafka and Geolocation are tested against a fake client and a generated `.mmdb`, not a broker or MaxMind's database.
