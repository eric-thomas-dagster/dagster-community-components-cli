# streamsets-to-dagster

Convert StreamSets Data Collector pipelines into a Dagster project, with a migration report, validation gates, and an agent skill that drives the workflow.

```
uv sync --extra dev
uv run streamsets-to-dagster inventory path/to/export         # read-only manifest
uv run streamsets-to-dagster convert   path/to/export --out-dir out --pkg my_migrated
uv run streamsets-to-dagster validate out --advance --execute
uv run streamsets-to-dagster status summary out/migration_manifest.json
```

Input is Data Collector or Control Hub pipeline JSON (a file, a folder, or a `.zip`).

- **Batch pipelines** become one asset each. The stages run as a record graph (origins, processors, destinations, lanes, events) in a small generated `runtime.py`.
- **Continuous pipelines** (CDC, Kafka, HTTP) are not run as batch: they are reported, with the question the customer must answer. Keep them in StreamSets and observe them with the registry `StreamSetsWorkspaceComponent`.
- **Anything not translated is a loud stub.** A pipeline that holds one fails before reading any data with `SDC-CAVEAT: <stage>: <reason>`.
- **Stages you rewrite by hand** (Groovy, JavaScript, Hive, a CDC origin) are passed with `--override` and stay labelled as hand-written in the report.
- **Credentials are never copied.** Credential-named runtime parameters are dropped and read from `SDC_SECRET_<NAME>`.

See `SKILL.md` for the workflow, `reference/mapping.md` for what is translated and what is only assumed, and `reference/corpus-results.md` for results on the public samples (six pipelines; no larger corpus exists).

Tests: `uv run pytest` (32; all six sample pipelines are converted and run end to end, against in-memory S3, SQLite, a fake Salesforce client and local files).

The six sample pipelines in `tests/fixtures/sdc` are from the public StreamSets `pipeline-library` (Apache License 2.0).
