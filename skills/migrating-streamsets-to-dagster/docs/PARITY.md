# Parity against a real Data Collector

Everything in this project is checked against IBM's documentation and against exports, never against a running Data Collector. Several behaviours are therefore **assumed**
(`reference/mapping.md`, "Verified and not verified"). A local Data Collector closes that gap: run the same input through the original pipeline and through the converted one
and compare.

## Setup

```
docker run -d --name sdc-parity -p 18630:18630 -e SDC_CONF_http_authentication=form streamsets/datacollector
```

(admin / admin through the REST API with the header `X-Requested-By: sdc`; the base image carries few stage libraries, so JDBC, S3, Kafka and the others are installed through the
Package Manager or mounted.) The image is large (about 1 GB) and needs free space in the Docker VM. `streamsets/transformer` is the Spark-based equivalent.

## What to verify first, in order of what hurts most if wrong

1. **Property names and defaults** of stages whose names are unverified: Encrypt and Decrypt Fields, JDBC Lookup column mappings, Field Merger, XML Parser, Field Hasher (new schema), Aggregator.
   Build each stage once in the UI, export the pipeline, and read the JSON.
2. **Encrypt and Decrypt Fields**: encrypt a field with a user-supplied key, export the ciphertext, and decrypt it with `runtime.decrypt_value`; if it fails, record the real plaintext layout.
3. **Expression language edge cases**: `str:matches`, `str:regExCapture` (whole-string versus find), `record:value` on missing and null fields, `+` on strings, time functions.
4. **Origin behaviour**: the S3 origin's `Name` attribute and event records, JDBC multitable offsets and events, Directory origin ordering, File Tail offsets, log-format field names.
5. **Processor edge cases**: Field Flattener naming, Field Mapper, Field Masker (mask character, non-matching values), Field Hasher output, Splitter with too few parts, List Pivot, Dedup windows, Base64 types.
6. **Script evaluators**: run the same Groovy, JavaScript and Jython on the same records and compare with the Python port and the QuickJS/JVM runs.
7. **Destinations**: JDBC producer column matching, Delta Lake merge semantics, Local FS file naming.

## The method

Data Collector's REST API can import a pipeline (`/rest/v1/pipeline/{id}`) and run a **preview** (`/rest/v1/pipeline/{id}/preview`) with a test origin or raw data and return every
stage's output records: the conformance test feeds the same records to the converted stage (`tests/` harness style, as `tests/js_diff.py` does for JavaScript) and compares output
records, attributes, errors and events. The endpoints are written from memory of the REST documentation and have not been exercised by this project: the first run of the harness
settles them. Pipelines that need external systems (JDBC, S3, Kafka) are run end to end against containers (Postgres or MySQL, MinIO, Redpanda).

## Why this matters for a customer

A parity run on the customer's own pipeline and a sample of their data is the only thing that proves behaviour; the converter has never seen their data. This is Phase 4 of the workflow in `SKILL.md`.
