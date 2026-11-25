# Estate: how the units relate

Facts from the extracted model, so a rewrite can choose asset boundaries across units. Lines marked *inferred* are suggestions to confirm, not facts.

## Units (2)

- `FIN_MIXED_RETRY` (chain): 2 call(s), converter disposition **degraded**, schedules: none, queues: BATCH_QUEUE
- `FIN_NIGHTLY` (chain): 3 call(s), converter disposition **degraded**, schedules: FIN_SCHED, queues: BATCH_QUEUE

## Chains calling chains

- none
- top-level chains (nothing in the export calls them): FIN_MIXED_RETRY, FIN_NIGHTLY

## Shared queues

- queue `BATCH_QUEUE` -> pool `batch_queue`, limit 3: units FIN_MIXED_RETRY, FIN_NIGHTLY


## Shared definitions and outputs

- definition `PULL` is called from FIN_MIXED_RETRY, FIN_NIGHTLY

## Schedules, events

- `FIN_NIGHTLY` is submitted by FIN_SCHED

## Candidate asset boundaries (inferred)

- queue BATCH_QUEUE is used by FIN_MIXED_RETRY, FIN_NIGHTLY: one pool `batch_queue` with one limit shared by all of them (not one pool per unit) *inferred*
- definition PULL is called from FIN_MIXED_RETRY, FIN_NIGHTLY: one reusable function or component configured per caller, not copied *inferred*
