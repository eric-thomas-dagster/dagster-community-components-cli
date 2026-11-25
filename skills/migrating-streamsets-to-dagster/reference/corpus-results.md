# Corpus results

The only public StreamSets pipelines found are six Data Collector samples (`tests/fixtures/sdc`, Apache 2.0, from the `pipeline-library` repository).
There is no larger corpus and no Transformer export, so these numbers describe the converter on six pipelines, not on an estate.

| Pipeline | Stages | Translated automatically | By hand | Runs end to end (`tests/`) |
|---|---|---|---|---|
| AWS Marketplace Data - Disbursed | 7 | 7 | 0 | yes: in-memory S3, SQLite; event-driven executor retires older rows; a second run reads nothing new |
| AWS Marketplace Data | 11 | 10 | 1 Groovy evaluator (`tests/overrides/forward_fill.py`) | yes, with the override |
| MySQL Schema replication to Delta Lake | 5 | 5 | 0 | yes: every table copied, pipeline ends on the `no-more-data` event |
| Salesforce Accounts to Databricks Delta Lake | 2 | 2 | 0 | yes, against a fake Salesforce client; a second run reads only new accounts (stored offset) |
| MySQL CDC to Delta Lake | 7 | 6 | 1 origin (`tests/overrides/cdc_feed.py`, a stand-in feed) | yes: inserts, an update and a delete applied by key. The binlog reader itself is not translated |
| Drift Synchronization for Hive | 5 | 1 | 4 Hive/HDFS stages retired by hand (`tests/overrides/`) | yes, with the Hadoop half replaced by a local file |

Every Delta Lake run writes to SQLite, standing in for Databricks: the generated SQL is tested, the Databricks dialect and COPY INTO semantics are not.
"Runs end to end" means the converted stage graph moves the data the way the tests describe; it has not been compared with a real StreamSets run.

## A second sample: 27 other public exports

27 more pipeline exports from public GitHub repositories (tutorials, demos and one vendor's agent pipelines, 14 of the 27) were used to learn real property
names and to test shapes the six samples lack. They are not redistributed. Their stages were converted, not run against the sources they name.

| After | Stages translated | Pipelines with a stub |
|---|---|---|
| First pass (six samples' stages only) | 100 of 196 (51%) | 19 of 27 |
| JavaScript, HTTP and vendor HTTP destination, Jython origin, Local Lookup, date conversion, `time:` functions | 174 of 196 (89%) | 19 of 27 |
| File Tail, LOG format, SDC_JSON, Geolocation, Field Mask, Kafka consumer and producer, raw data | 187 of 196 (95%) | 4 of 27 |
| Elasticsearch, MongoDB (through the registry's resources), HTTP origin, older Geolocation schema | 192 of 196 (98%) | 1 of 27 |
| JDBC lookup, Groovy (JVM), Dev Simulator, Base64, Field Value Replacer, pivot, dedup, parsers, XML flattener | 194 of 196 (99%) | 0 of 27 (all 27 have no stubs; 2 JDBC lookups are templates with no query configured and are refused) |

Translated is not correct: a script stage that translates still has to be reviewed (the 25 JavaScript scripts run in QuickJS; 19 ran on a trivial record, 6 raised).
What is left (2 stages): two JDBC lookups whose query is empty, i.e. template stages nobody configured; they stay stubs on purpose.
The vendor's 14 pipelines inflate the JavaScript and HTTP counts: a different customer's mix will differ.
The 99% counts stages the converter produces code for. Kafka, Geolocation, Elasticsearch, MongoDB, HTTP, Groovy and the scripting stages were run only against fakes, in-memory databases, local servers
and trivial records, never against the real systems (the Groovy evaluator not yet against a JVM); the Encrypt/Decrypt stage did not appear in any sample and is deliberately not translated.

JavaScript evaluators in the 27-pipeline sample: **25 of 25 transpile to Python** (`jstopy.py`) and match QuickJS on random inputs (see *Scripting* in mapping.md); the stages that used to be QuickJS-only are now plain Python modules.

A further 17 pipelines are Data Collector's own default-config test pipelines (versions 1.1 to 2.2, every stage with its defaults). Converting them is a robustness check, not a coverage number:
all 17 convert without error and every unconfigured or untranslatable stage becomes a stub with its reason.

Gates on the six samples' generated project: 1 (compiles), 2 (loads), 3 (units match) pass; gate 4 dry run: 1 ok, 5 fail on a stub as designed, 0 unexpected.

## Could registry pipeline components run these pipelines?

`components_plan.py` maps each pipeline's stages onto the ops of `polars_pipeline` / `warehouse_pipeline` and says what has no op yet (it plans; nothing is generated from it).
**1 of 6** sample pipelines (Salesforce to Delta Lake: a select) and **1 of 27** public ones are fully covered. Ops that map today: select, drop, rename, cast (46 uses on the public pipelines).
Biggest gaps on the public pipelines: Expression Evaluator needs EL translated to SQL (32 stages), script evaluators (31), vendor/HTTP destinations (20), lookups (8), Field Order (6), geolocation (2).

## Real exports from an IBM StreamSets trial (14 pipelines)

Fourteen sample pipelines exported from a trial Control Hub (10 Data Collector, 4 Transformer; not redistributed). With the first version of the Transformer route: **30 of 81 stages translate** as record-graph stages;
the Data Collector ones are stopped by CDC origins (Oracle, PostgreSQL, Salesforce subscription), Snowflake and ADLS Gen2 destinations, Avro output, TensorFlow and HDFS; **all four Transformer pipelines had 0 translatable stages** as record graphs
because their stages are Spark DataFrame operations. With the components route, **Retail Inventory (join, aggregate, repartition) now converts to registry components**; the two Slowly Changing Dimension pipelines and the Scala UDF pipeline do not
(no `scd` op, raw-data origins, Scala).
