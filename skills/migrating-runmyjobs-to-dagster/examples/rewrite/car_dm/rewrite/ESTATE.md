# Estate: how the units relate

Facts from the extracted model, so a rewrite can choose asset boundaries across units. Lines marked *inferred* are suggestions to confirm, not facts.

## Units (1)

- `NIGHTLY` (chain): 2 call(s), converter disposition **degraded**, schedules: JG, queues: none

## Chains calling chains

- none
- top-level chains (nothing in the export calls them): NIGHTLY

## Shared queues

- none

- queues declared in the export that no call uses: Q1. They belong to no unit, so no rewrite gate completes them: give each a disposition with `status advance` (say where the pool is created) or `status defer` (say why it is not needed)

## Shared definitions and outputs

- none

## Schedules, events

- `NIGHTLY` is submitted by JG

## Candidate asset boundaries (inferred)

- none
