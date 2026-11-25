# Estate: how the units relate

Facts from the extracted model, so a rewrite can choose asset boundaries across units. Lines marked *inferred* are suggestions to confirm, not facts.

## Units (4)

- `Forecast_Replenishment_Template` (chain): 16 call(s), converter disposition **degraded**, schedules: none, queues: none
- `Full_FR_Run` (chain): 10 call(s), converter disposition **degraded**, schedules: none, queues: none
- `Process_Negative_Stock_Template` (chain): 2 call(s), converter disposition **translated**, schedules: none, queues: none
- `ReplenishShop` (chain): 4 call(s), converter disposition **translated**, schedules: none, queues: none

## Chains calling chains

- `Forecast_Replenishment_Template` runs `Full_FR_Run`, `Process_Negative_Stock_Template`
- `Full_FR_Run` runs `ReplenishShop`
- top-level chains (nothing in the export calls them): Forecast_Replenishment_Template

## Shared queues

- none


## Shared definitions and outputs

- definition `System_DoNothing` is called from Forecast_Replenishment_Template, Full_FR_Run, Process_Negative_Stock_Template, ReplenishShop

## Schedules, events

- none

## Candidate asset boundaries (inferred)

- chain Forecast_Replenishment_Template runs chain(s) Full_FR_Run, Process_Negative_Stock_Template: either one pipeline whose assets are the kids' assets in order (asset dependencies across the units), or the parent as a thin orchestration job over kid pipelines that stay separate *inferred*
- chain Full_FR_Run runs chain(s) ReplenishShop: either one pipeline whose assets are the kids' assets in order (asset dependencies across the units), or the parent as a thin orchestration job over kid pipelines that stay separate *inferred*
- definition System_DoNothing is called from Forecast_Replenishment_Template, Full_FR_Run, Process_Negative_Stock_Template, ReplenishShop: one reusable function or component configured per caller, not copied *inferred*
