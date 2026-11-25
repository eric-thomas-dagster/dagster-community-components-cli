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

Other stage types not translated: Field Merger (IBM's page does not say whether the source fields are removed or how lists merge), XML Parser (its output field naming is not documented), JDBC Tee, HBase and Redis lookups, the Spark processor, Aggregator, Delay, Kafka single-topic consumer, the Encrypt and Decrypt Fields stage (see Encryption). Also not translated: Transformer/Spark stages
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

StreamSets has one **Encrypt and Decrypt Fields** processor (the Mode property picks encrypt or decrypt; one per stage). From IBM's documentation: it uses the **AWS Encryption SDK**
(envelope encryption: a data key per operation, stored encrypted with the ciphertext), key provider **AWS KMS** (a key ARN) or a **user-supplied Base64 key** (with an optional key id),
cipher suites that are AES-GCM (default `ALG_AES_256_GCM_IV12_TAG16_HKDF_SHA384_ECDSA_P384`; 12-byte IV, 16-byte tag), a frame size (default 4096), an optional
**encryption context** (additional authenticated data that must match on decrypt), and optional data-key caching. The field's original type is stored in the ciphertext and restored on decrypt.
Related stages: **Field Hasher** (MD5/SHA1/SHA256/SHA512/MURMUR3_128; in place, to a target field or header, or whole record) and **Field Masker**.

What the converter does:

- The Encrypt/Decrypt stage is a **stub**, on purpose. No real export of it was found, so its property names are unverified, and **ciphertext written by StreamSets cannot be read by a replacement
  unless the byte layout inside the AWS Encryption SDK message is known** (StreamSets stores the type with the data in a layout this project has not seen). Both are questions for the customer
  (the report asks them): is data encrypted by StreamSets still read after the move, with which key provider, and is an encryption context used?
- `runtime.py` has what a hand-written replacement needs: `encrypt_value` / `decrypt_value` (AWS Encryption SDK, KMS or a Base64 AES key, encryption context enforced, types restored) and a stage
  `crypto_fields(run, inst, mode, fields, kms_key_arn_fn=..., key_fn=..., context=...)`. The AWS Encryption SDK message format is interoperable between its Java and Python implementations,
  so KMS-encrypted data is readable if the **plaintext layout** matches; the layout used here (`{"t": type, "v": value}` JSON) is this project's own.
- Keys never go in generated code: pass them from the environment (`SDC_SECRET_*`) or KMS.
- Hashes: only in-place hashing is translated (see the catalog). A pipeline that hashes to join against old hashes must be compared on one value first.

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
- Log formats: the Common/Combined Log Format field names (`/remoteHost /request /status ...`) and the grok names (`/clientip /verb /response /bytes ...`) are the documented/standard ones, not checked against a Data Collector's output.
- Kafka and Geolocation are tested against a fake client and a generated `.mmdb`, not a broker or MaxMind's database.
- Encrypt/Decrypt: see *Encryption*; the stage itself is not translated.
