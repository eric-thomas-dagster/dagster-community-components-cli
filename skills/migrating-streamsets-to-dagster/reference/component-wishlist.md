# Registry component wishlist (StreamSets)

Registry components that would replace generated code. Check `dagster-community-components` first; this lists what is missing or unverified.

| Need | Today | Ask |
|---|---|---|
| Continuous pipeline lifecycle | `StreamSetsWorkspaceComponent` (jobs declared by id, status polled) | A list-jobs call, and job metrics (records in/out, errors) as asset metadata, so a job can be discovered and observed with data |
| Control Hub job and schedule export | Not parsed | A way to read job-to-pipeline bindings and their schedules, so a Dagster schedule can be generated |
| JDBC table replication with offsets | Generated `jdbc_multitable_origin` reads full tables every run | `SlingReplicationAssetComponent` covers table-to-table replication; confirm it can replace a multitable-origin-to-warehouse pipeline including incremental keys |
| Record-graph runtime | `runtime.py` generated into each project | A registry `RecordPipelineComponent`, so the runtime is not copied per project |
| Salesforce, Kafka, Delta targets | Stubs that point at the registry | Confirm `SalesforceIngestionComponent`, `KafkaToDatabaseAssetComponent` and `DataframeToDeltaTableComponent` cover the stage options the customer uses (query, offset column, topic, merge keys) |
| HTTP/REST stages | Not translated (HTTP Client processor and destination are common in real estates) | A registry REST-call component with per-record/per-batch bodies and auth, so the converter can emit it instead of generating requests code |
| Script stages | Jython runs under CPython; Groovy/JavaScript need a rewrite | Nothing to ask of the registry; a customer-by-customer rewrite with tests |
