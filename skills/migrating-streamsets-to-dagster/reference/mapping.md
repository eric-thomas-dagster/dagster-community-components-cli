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
| Groovy / JavaScript evaluators | Rewrite as Python and pass it with `--override`. The script text is kept in the manifest notes |
| Jython evaluator | Translated (runs under CPython); see the catalog |

Other stage types seen in real exports and not translated: HTTP Client (processor and destination), Local Lookup, JDBC Lookup, Geolocation, Kafka producer,
Jython origin, vendor destinations (Anodot, ...), MongoDB, Elasticsearch, Field Mask, SDC_JSON data format. Also not translated: Transformer/Spark stages
, MongoDB, REST client, Kafka producer, ...) have no translation yet and are reported as stubs.

## Expression language (EL)

Translated: `${...}` with `record:value/valueOrDefault/attribute/attributeOrDefault/exists/eventType`, `str:` (`toLower toUpper trim length substring
concat replace replaceAll contains startsWith endsWith matches regExCapture indexOf truncate isNullOrEmpty`), `math:` (`abs ceil floor round max min`),
`time:now`, `file:` (`fileName parentPath pathElement removeExtension`), `f:` (Field Mapper context), `uuid`, `YYYY() MM() DD() hh() mm() ss()`,
runtime parameters by name, `SECONDS MINUTES HOURS DAYS`, the operators `== != < > <= >= && || ! ? : + - * / %` and `eq ne lt gt le ge and or not empty div mod`.
Any other function raises Untranslatable and the stage becomes a stub.

## Verified and not verified

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
- Jython scripts run under CPython; whether a given script behaves the same is a per-script review.
- Transformer (Spark) pipelines load, but none of their stages is translated, and no real Transformer export was available.
- Control Hub export archives are read (`.zip` of pipeline JSON); the Control Hub job and schedule objects are not parsed.
