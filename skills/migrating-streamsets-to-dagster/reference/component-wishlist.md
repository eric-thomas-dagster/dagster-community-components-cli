# Components session tally (StreamSets to Dagster)

A running list of what to tell the components session so the StreamSets skill can emit registry components instead of generated code. Newest asks at the
top of each section. Keep this file current whenever the converter gains or drops a stage.

## Open asks

| # | Need | Why the skill needs it | Today |
|---|---|---|---|
| 1 | **HTTP/REST destination and per-record call** (POST/PUT a JSON body per record or per batch as an array or newline-delimited; basic/bearer/OAuth2; headers; retry and backoff on status and timeout; optional response written back into the record) | HTTP Client processor and destination are among the most common stages in real estates (6 of 27 sampled pipelines, plus a vendor destination that is the same POST in 14) | The converter generates raw `requests` code (`runtime.py`: `http_processor`, `http_target`). The registry has REST *sources* (`rest_api_fetcher`, `oauth_rest_ingestion`, `dlt_rest_api_source`) but no sink |
| 2 | **Record-graph runtime as a component** (`RecordPipelineComponent`: stages, lanes, events, batches, offsets, error policy) | Every generated project copies a ~1,000-line `runtime.py`; fixes must be re-applied per project | Generated into each project |
| 3 | **Delta Lake / Databricks writer with merge-by-key** (insert, update, delete by key columns, table auto-create, new-column drift) | The Databricks Delta Lake destination appears in 4 of the 6 public samples; CDC needs delete-by-key | Generated SQL via SQLAlchemy (`delta_target`), delete-then-insert. `dataframe_to_delta_table` (delta-rs) and `SQLToDatabaseAssetComponent` upsert exist: confirm they cover CDC deletes and a Databricks SQL warehouse target |
| 4 | **Binlog/CDC micro-batch source** (MySQL binary log; later Oracle, PostgreSQL, SQL Server) that reads from a stored position to "caught up" and stops | CDC is the main thing that keeps pipelines on StreamSets; today those pipelines are tier 1 only | Not translated. No CDC component in the registry. Tests use a stand-in feed (`tests/overrides/cdc_feed.py`) with an assumed record shape |
| 5 | **StreamSetsWorkspaceComponent: list jobs, job metrics, job-to-pipeline binding, schedules** | Tier 1 for continuous pipelines needs jobs declared by id; a Control Hub export carries pipelines but not their schedules | `jobs:` declared by hand; status only |
| 6 | **Kafka producer sink** (JSON messages per record, topic expression, key, partitioner, flush per batch) | Kafka destinations are common; the registry has a consumer and monitor only | Interim generated code (`kafka_target`, tested against a fake client only). Still wanted as a registry component |
| 7 | **MaxMind GeoIP component: a licensed database, kept current, with a lookup transform** (not just a file path). (a) *Acquire*: download GeoLite2 or GeoIP2 editions (City, Country, ASN) with a MaxMind **account ID and license key** from environment variables, a list of edition IDs, and a target directory; GeoLite2 downloads have required a MaxMind account and accepting the EULA since 2019-12-30, and the databases may not be redistributed, so the project cannot ship one. (b) *Refresh*: on a schedule or sensor (MaxMind publishes updates on a regular cadence; the `geoipupdate` tool and the `pygeoipupdate` package are the reference), skipping unchanged builds by checking the release date, and writing the build date and checksum as asset metadata so a lookup is traceable to a database version. (c) *Lookup*: an IP-to-attributes transform (latitude, longitude, country name and ISO code, city, postal code, subdivision, ASN), with a defined result for private, malformed and unknown addresses, IPv4 and IPv6. (d) optional MaxMind web-service client for customers without a local database | The Geolocation processor appears in web-log pipelines; StreamSets stored a file path and left licensing and updating to the operator. A converted pipeline that reads a stale or unlicensed file is a quiet failure | Interim generated code (`geolocation`, tested against a generated `.mmdb`) reads a file path only; the report asks the customer who holds the license and who refreshes the file. The registry's `geocoder` is address-based, not IP-based. Still wanted as a registry component |
| 8 | **JDBC multitable incremental read** (offset columns, per-table offsets, partitioning) | The multitable origin is the usual database-to-warehouse origin | Generated full-table read every run (`jdbc_multitable_origin`). `SlingReplicationAssetComponent` may cover it: confirm incremental keys and table patterns |
| 9 | **Salesforce SOQL with stored offset** | The Salesforce origin runs `WHERE Id > '${OFFSET}' ORDER BY Id` | Generated `simple_salesforce` loop. `SalesforceIngestionComponent` is dlt-based: confirm custom SOQL and offsets |
| 10 | **Record transforms with StreamSets semantics**: field type conversion (incl. Java date patterns), rename, order, split, flatten (incl. whole record), mask, lookup, JDBC lookup, base64, replace-if-null, list pivot, dedup, XML flatten and parse, log parse | Eight processors are generated as record-level Python | Registry has dataframe versions (`type_coercer`, `dynamic_rename`, `data_masking`, `lookup`, `dataframe_flatten_nested_columns`, `filter`); confirm they can replace the record-level ones, or say they cannot (edge semantics differ) |
| 11 | **Script evaluators**: Jython (runs unchanged under CPython), JavaScript (QuickJS), Groovy (not translated) | 25 JavaScript and 11 Jython-origin stages in the 27-pipeline sample | Generated shims. Nothing to ask of the registry; recorded so the components session knows why a scripting component is not requested |

| 12 | **Field-level encrypt/decrypt component** (AWS Encryption SDK message format, KMS or user-supplied key, encryption context, type-preserving; ideally able to read ciphertext written by StreamSets' Encrypt and Decrypt Fields stage) | The prospect is believed to encrypt and decrypt; StreamSets' stage uses the AWS Encryption SDK. Interop with existing ciphertext hinges on the plaintext layout inside the SDK message, which is not documented | `runtime.encrypt_value/decrypt_value/crypto_fields` (own plaintext layout, not StreamSets-compatible). The registry has `data_masking` but no encryption component. Needs a real export and a ciphertext sample to decide |
| 13 | **Hashing/masking transforms with StreamSets semantics** (Field Hasher in place/target/record, MURMUR3_128, Field Masker custom) | Hash joins against old hashes need identical output | Generated in-place MD5/SHA family only |

| 14 | **Offset-based incremental reads for Mongo and Elasticsearch** (stored last-offset on a field, repeat until empty, resume next run) | The skill now uses the registry's `MongoDBResource` and `ElasticsearchResource` for connections, but `mongodb_reader` and `elasticsearch_reader` take a query and a limit with no stored offset, so the skill generates the offset loop itself | Generated `mongo_origin` / `es_origin` on top of the registry resources (tested against mongomock and a fake client). Confirm whether the readers can take an offset field and initial offset |

| 15 | **Script evaluator component, as the fallback** (Groovy on a JVM sidecar or container; JavaScript on QuickJS for the scripts the transpiler refuses; Jython-compatible Python). StreamSets scripting objects (`records`, `output.write`, `error.write`, `state`, `log`, `sdcFunctions.createRecord`, `sdc.createEvent/toEvent`), BATCH and RECORD modes, init and destroy scripts, `state` persisting across batches **and runs**, error records to a dead-letter output, events to a second output, limits and no file or network access by default | The skill now **transpiles JavaScript to Python** (25 of 25 sampled scripts), so this component is needed mainly for **Groovy** (no transpiler) and for JavaScript that uses Nashorn-only features. The less hand-written override code, the better | Groovy: `groovy_processor` (JVM; not yet run against a JVM). JavaScript fallback: `js_processor` (QuickJS) |

| 16 | **Base64 encode/decode transform** (bytes and strings, standard and URL-safe) | The registry has no base64 component (`text_codec_convert_asset` converts character codecs, not base64) | Hand-rolled `base64_field` |
| 17 | **Component-graph emission** (a decision, then an ask of each covered component): emit a pipeline as an asset graph of registry components instead of one record-graph asset. Registry transforms are DataFrame-asset components whose logic lives inside `build_defs` closures, so the skill cannot call them as libraries; the only way to use them is to emit their YAML. Each covered component should accept an upstream asset and the StreamSets stage options (see the table below) | Hand-rolled stages reimplement what the registry already has. Emitting components is more idiomatic (per-step assets, lineage, retries) but changes the shape from record streams to DataFrame batches, so it fits stateless record transforms and not record routing, events or per-record error lanes | Not built. The skill's record-graph stages are the interim |

## Hand-rolled in the skill, and the registry component that already exists

Use the component where one exists (switch the converter to emit it once component-graph emission is chosen); ask only where there is a gap. "Candidate" means the registry component's description matches the stage but its edge semantics are not checked against StreamSets.

| Skill's hand-rolled stage | Registry component | Status |
|---|---|---|
| `dedup` (Record Deduplicator) | `unique_dedup`, `warehouse_dedup` | candidate: use it |
| `field_hasher` (in-place hash) | `hash` (MD5/SHA-1/SHA-256, one or more columns or the row), `pii_hasher` | candidate; confirm SHA-512 and in-place output, and that hex output matches StreamSets (hash joins) |
| `field_mask` | `data_masking` (hash, partial mask, redact, substitute) | candidate; regex-group masks differ |
| `field_type_converter` | `type_coercer`, `datetime_parser` | candidate; Java date patterns need checking |
| `field_renamer`, `field_order` | `dynamic_rename`, `select_columns` | candidate |
| `field_flattener`, `xml_flatten` | `json_flatten`, `dataframe_flatten_nested_columns`; `xml_parser` (XPath) | candidate for JSON flatten; XML flatten naming differs, so a gap |
| `list_pivot` | `array_exploder` | candidate: use it |
| `splitter` | `text_to_columns` | candidate |
| `log_parser`, regex parse | `regex_parser` | candidate |
| `json_parser`, JSON path | `json_path_extractor` | candidate |
| `local_lookup`, `jdbc_lookup` | `lookup`, `find_replace`, `warehouse_join` | candidate |
| `replace_if_null` | `data_cleansing`, `find_replace` | partial: confirm |
| `http_processor` (per-record request) | `per_row_http_fetcher` (URL from a column; appends body, status, parsed JSON) | candidate for GET; confirm POST body, headers, auth |
| `http_target` (batch POST) | `http_webhook_job` (a single POST/GET job) is not per-batch data | **gap: ask #1** |
| `base64_field` | none | **gap: ask #16** |
| `crypto_fields` (encrypt/decrypt) | none; `hashicorp_vault` and `key_vault_resource` can supply keys | **gap: ask #12** (keys from those resources) |
| `geolocation` | none (`geocoder` is address-based) | **gap: ask #7** |
| `kafka_target` | none (consumer only: `kafka_to_database_asset`) | **gap: ask #6** |
| `kafka_origin` | `kafka_to_database_asset`, `kafka_monitor` | candidate |
| `mongo_origin`, `es_origin`, `es_target` | `mongodb_reader`/`MongoDBResource`, `elasticsearch_reader`/`elasticsearch_asset`/`ElasticsearchResource` (resources already used) | readers lack stored offsets: ask #14 |
| Redis / Cassandra lookups (not translated) | `redis_reader`, `cassandra_reader`/`cassandra_writer` | candidate when needed |
| Python-callable stage, R scripts | `python_callable_asset`, `r_script` | n/a |

## Registry components the skill already uses or points customers at (verify each still exists)

Used by generated code: `MongoDBResource` (`resources/mongodb_resource`) and `ElasticsearchResource` (`resources/elasticsearch_resource`), for connections in `mongo_origin`, `es_origin`, `es_target`. Pointed at: 

`StreamSetsWorkspaceComponent` (tier 1), `KafkaToDatabaseAssetComponent` + `kafka_monitor`, `SalesforceIngestionComponent`, `DataframeToDeltaTableComponent`,
`SlingReplicationAssetComponent`, `S3ToDatabaseAssetComponent` + `s3_monitor`, `data_masking`, `lookup`,
`rest_api_fetcher`, `dlt_rest_api_source`.

## Resolved

(none yet)
