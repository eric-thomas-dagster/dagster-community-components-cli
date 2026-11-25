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
| 7 | **GeoIP (MaxMind) lookup transform** (GeoIP2 City/Country `.mmdb`: latitude, longitude, country, city, postal code) | Geolocation processor appears in web-log pipelines | Interim generated code (`geolocation`, tested against a generated `.mmdb`). The registry's `geocoder` is address-based. Still wanted as a registry component |
| 8 | **JDBC multitable incremental read** (offset columns, per-table offsets, partitioning) | The multitable origin is the usual database-to-warehouse origin | Generated full-table read every run (`jdbc_multitable_origin`). `SlingReplicationAssetComponent` may cover it: confirm incremental keys and table patterns |
| 9 | **Salesforce SOQL with stored offset** | The Salesforce origin runs `WHERE Id > '${OFFSET}' ORDER BY Id` | Generated `simple_salesforce` loop. `SalesforceIngestionComponent` is dlt-based: confirm custom SOQL and offsets |
| 10 | **Record transforms with StreamSets semantics**: field type conversion (incl. Java date patterns), rename, order, split, flatten, mask, lookup | Eight processors are generated as record-level Python | Registry has dataframe versions (`type_coercer`, `dynamic_rename`, `data_masking`, `lookup`, `dataframe_flatten_nested_columns`, `filter`); confirm they can replace the record-level ones, or say they cannot (edge semantics differ) |
| 11 | **Script evaluators**: Jython (runs unchanged under CPython), JavaScript (QuickJS), Groovy (not translated) | 25 JavaScript and 11 Jython-origin stages in the 27-pipeline sample | Generated shims. Nothing to ask of the registry; recorded so the components session knows why a scripting component is not requested |

| 12 | **Field-level encrypt/decrypt component** (AWS Encryption SDK message format, KMS or user-supplied key, encryption context, type-preserving; ideally able to read ciphertext written by StreamSets' Encrypt and Decrypt Fields stage) | The prospect is believed to encrypt and decrypt; StreamSets' stage uses the AWS Encryption SDK. Interop with existing ciphertext hinges on the plaintext layout inside the SDK message, which is not documented | `runtime.encrypt_value/decrypt_value/crypto_fields` (own plaintext layout, not StreamSets-compatible). The registry has `data_masking` but no encryption component. Needs a real export and a ciphertext sample to decide |
| 13 | **Hashing/masking transforms with StreamSets semantics** (Field Hasher in place/target/record, MURMUR3_128, Field Masker custom) | Hash joins against old hashes need identical output | Generated in-place MD5/SHA family only |

## Registry components the skill already points customers at (verify each still exists)

`StreamSetsWorkspaceComponent` (tier 1), `KafkaToDatabaseAssetComponent` + `kafka_monitor`, `SalesforceIngestionComponent`, `DataframeToDeltaTableComponent`,
`SlingReplicationAssetComponent`, `S3ToDatabaseAssetComponent` + `s3_monitor`, `mongodb_reader`, `elasticsearch_reader`, `elasticsearch_asset`, `data_masking`, `lookup`,
`rest_api_fetcher`, `dlt_rest_api_source`.

## Resolved

(none yet)
