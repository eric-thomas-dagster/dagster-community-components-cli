# Estate: how the units relate

Facts from the extracted model, so a rewrite can choose asset boundaries across units. Lines marked *inferred* are suggestions to confirm, not facts.

## Units (3)

- `LEGACY_SAP_EXTRACT` (process_definition): 1 call(s), converter disposition **stubbed**, schedules: none, queues: none
- `NIGHTLY_SALES_LOAD` (chain): 6 call(s), converter disposition **stubbed**, schedules: NIGHTLY_SALES_SCHEDULE, queues: BATCH_QUEUE, DB_QUEUE
- `PURGE_TEMP` (process_definition): 1 call(s), converter disposition **degraded**, schedules: PURGE_HOURLY, queues: none

## Chains calling chains

- none
- top-level chains (nothing in the export calls them): NIGHTLY_SALES_LOAD

## Shared queues

- queue `BATCH_QUEUE` -> pool `batch_queue`, limit 4: units NIGHTLY_SALES_LOAD
- queue `DB_QUEUE` -> pool `db_queue`, limit 2: units NIGHTLY_SALES_LOAD


## Shared definitions and outputs

- none

## Schedules, events

- `NIGHTLY_SALES_LOAD` is submitted by NIGHTLY_SALES_SCHEDULE
- `PURGE_TEMP` is submitted by PURGE_HOURLY

## Candidate asset boundaries (inferred)

- none
