# Corpus results

The only public StreamSets pipelines found are six Data Collector samples (`tests/fixtures/sdc`, Apache 2.0, from the `pipeline-library` repository).
There is no larger corpus and no Transformer export, so these numbers describe the converter on six pipelines, not on an estate.

| Pipeline | Stages | Translated | Disposition |
|---|---|---|---|
| AWS Marketplace Data - Disbursed | 7 | 7 | REVIEW: translated with stated caveats (offsets, error destination); executes end to end in `tests/test_end_to_end.py` |
| AWS Marketplace Data | 11 | 10 | REVIEW: Groovy evaluator is a stub |
| MySQL Schema replication to Delta Lake | 5 | 4 | REVIEW: Delta Lake destination is a stub; multitable origin reads full tables |
| Drift Synchronization for Hive | 5 | 1 | REVIEW: Hive/HDFS stages are stubs |
| MySQL CDC to Delta Lake | 7 | 5 | NONE: continuous CDC origin and Delta destination are stubs |
| Salesforce Accounts to Databricks Delta Lake | 2 | 0 | NONE: Salesforce origin and Delta destination are stubs |

27 of 37 stages translated (73%), 10 stubs. That is dominated by what these samples happen to use (Databricks Delta Lake, Hive, CDC); do not extrapolate.

Gates on the generated project: 1 (compiles), 2 (loads), 3 (6 units match) pass; gate 4 dry run: 1 ok, 5 fail on a stub (`SDC-CAVEAT`) as designed, 0 unexpected.
Real execution: the Disbursed pipeline runs against an in-memory S3 and SQLite, retires older rows through its event-driven executor, and a second run reads nothing new.
