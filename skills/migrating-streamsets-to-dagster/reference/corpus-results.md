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
| JavaScript, HTTP and vendor HTTP destination, Jython origin, Local Lookup, date conversion, `time:` functions | 174 of 196 (89%) | 19 of 27 (most have one or two left) |

Translated is not correct: a script stage that translates still has to be reviewed (the 25 JavaScript scripts run in QuickJS; 19 ran on a trivial record, 6 raised).
What is left (22 stages): the SDC_JSON data format (4), JDBC lookup (2), geolocation (2), and one each of MongoDB, Elasticsearch, Kafka consumers and producer,
file tail, Groovy, Field Mask, HTTP origin and a dev-only source. The vendor's 14 pipelines inflate the JavaScript and HTTP counts: a different customer's mix will differ.

Gates on the six samples' generated project: 1 (compiles), 2 (loads), 3 (units match) pass; gate 4 dry run: 1 ok, 5 fail on a stub as designed, 0 unexpected.
