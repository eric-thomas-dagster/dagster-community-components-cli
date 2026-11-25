# Migration report

36 source objects: **34 converted**, **2 need review**, **0 unmapped** (94% converted without review).

Caveats: **0 stub(s)** (the asset exists in the right place and fails with `RMJ-CAVEAT: <reason>` until it is ported) and **2 converted with a known difference** (listed in the Notes column). Nothing is silently dropped.

Converted means the generated code loads and encodes the same structure. It does not mean behavior was verified against RunMyJobs; run both in parallel before cutover.

| Kind | Source | Dagster artifact | Status | Notes |
|---|---|---|---|---|
| chain | Forecast_Replenishment_Template | job forecast_replenishment_template | needs review | kept as one asset per call: step 'Nightly Processing' runs 2 calls in parallel, which one chain asset cannot express; step 'Nightly Processing' calls chain 'Full_FR_Run': its steps are inlined here as assets named forecast_replenishment_template__full_forecast_and_replenishment_run__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted); step 'Replenish the Shops' calls chain 'ReplenishShop': its steps are inlined here as assets named forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted); the call to 'ReplenishShop' waits for event(s) WaitForShop1: not enforced (see the first inlined step); step 'Replenish the Shops' calls chain 'ReplenishShop': its steps are inlined here as assets named forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted); the call to 'ReplenishShop' waits for event(s) WaitForShop2: not enforced (see the first inlined step); step 'Nightly Processing' calls chain 'Process_Negative_Stock_Template': its steps are inlined here as assets named forecast_replenishment_template__process_negative_stock__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted) |
| chain | Full_FR_Run | job full_fr_run | needs review | kept as one asset per call: step 'Replenish the Shops' runs 2 calls in parallel, which one chain asset cannot express; step 'Replenish the Shops' calls chain 'ReplenishShop': its steps are inlined here as assets named full_fr_run__replenish_shop_1__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted); the call to 'ReplenishShop' waits for event(s) WaitForShop1: not enforced (see the first inlined step); step 'Replenish the Shops' calls chain 'ReplenishShop': its steps are inlined here as assets named full_fr_run__replenish_shop_2__*; parameter 'P_REGION' (from 'P_REGION') has no value here (the caller supplies it when the chain is submitted); the call to 'ReplenishShop' waits for event(s) WaitForShop2: not enforced (see the first inlined step) |
| chain | Process_Negative_Stock_Template | asset process_negative_stock_template (TaskChainAssetComponent) | converted | 2 steps run in order as one asset and stop at the first failure; use chains=assets for one asset per call |
| chain | ReplenishShop | asset replenishshop (TaskChainAssetComponent) | converted | 4 steps run in order as one asset and stop at the first failure; use chains=assets for one asset per call |
| process_definition | Forecast_Replenishment_Template/calculate replenishment (System_DoNothing) | forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__calculate_replenishment | converted |  |
| process_definition | Forecast_Replenishment_Template/calculate replenishment (System_DoNothing) | forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__calculate_replenishment | converted |  |
| process_definition | Forecast_Replenishment_Template/confirmation truck loaded (System_DoNothing) | forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__confirmation_truck_loaded | converted |  |
| process_definition | Forecast_Replenishment_Template/confirmation truck loaded (System_DoNothing) | forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__confirmation_truck_loaded | converted |  |
| process_definition | Forecast_Replenishment_Template/count negative stock (System_DoNothing) | forecast_replenishment_template__process_negative_stock__count_negative_stock | converted |  |
| process_definition | Forecast_Replenishment_Template/create pids (System_DoNothing) | forecast_replenishment_template__process_negative_stock__create_pids | converted |  |
| process_definition | Forecast_Replenishment_Template/initialize (System_DoNothing) | forecast_replenishment_template__initialize | converted |  |
| process_definition | Forecast_Replenishment_Template/pick orders (System_DoNothing) | forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__pick_orders | converted |  |
| process_definition | Forecast_Replenishment_Template/pick orders (System_DoNothing) | forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__pick_orders | converted |  |
| process_definition | Forecast_Replenishment_Template/post pids (System_DoNothing) | forecast_replenishment_template__post_pids | converted |  |
| process_definition | Forecast_Replenishment_Template/post posdm idocs (System_DoNothing) | forecast_replenishment_template__post_posdm_idocs | converted |  |
| process_definition | Forecast_Replenishment_Template/preparation run (System_DoNothing) | forecast_replenishment_template__full_forecast_and_replenishment_run__preparation_run | converted |  |
| process_definition | Forecast_Replenishment_Template/promotion forecast (System_DoNothing) | forecast_replenishment_template__full_forecast_and_replenishment_run__promotion_forecast | converted |  |
| process_definition | Forecast_Replenishment_Template/reprocess idocs in error (System_DoNothing) | forecast_replenishment_template__reprocess_idocs_in_error | converted |  |
| process_definition | Forecast_Replenishment_Template/wait for truck (System_DoNothing) | forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_1__wait_for_truck | converted |  |
| process_definition | Forecast_Replenishment_Template/wait for truck (System_DoNothing) | forecast_replenishment_template__full_forecast_and_replenishment_run__replenish_shop_2__wait_for_truck | converted |  |
| process_definition | Full_FR_Run/calculate replenishment (System_DoNothing) | full_fr_run__replenish_shop_1__calculate_replenishment | converted |  |
| process_definition | Full_FR_Run/calculate replenishment (System_DoNothing) | full_fr_run__replenish_shop_2__calculate_replenishment | converted |  |
| process_definition | Full_FR_Run/confirmation truck loaded (System_DoNothing) | full_fr_run__replenish_shop_1__confirmation_truck_loaded | converted |  |
| process_definition | Full_FR_Run/confirmation truck loaded (System_DoNothing) | full_fr_run__replenish_shop_2__confirmation_truck_loaded | converted |  |
| process_definition | Full_FR_Run/pick orders (System_DoNothing) | full_fr_run__replenish_shop_1__pick_orders | converted |  |
| process_definition | Full_FR_Run/pick orders (System_DoNothing) | full_fr_run__replenish_shop_2__pick_orders | converted |  |
| process_definition | Full_FR_Run/preparation run (System_DoNothing) | full_fr_run__preparation_run | converted |  |
| process_definition | Full_FR_Run/promotion forecast (System_DoNothing) | full_fr_run__promotion_forecast | converted |  |
| process_definition | Full_FR_Run/wait for truck (System_DoNothing) | full_fr_run__replenish_shop_1__wait_for_truck | converted |  |
| process_definition | Full_FR_Run/wait for truck (System_DoNothing) | full_fr_run__replenish_shop_2__wait_for_truck | converted |  |
| process_definition | Process_Negative_Stock_Template/count negative stock (System_DoNothing) | step count_negative_stock of asset process_negative_stock_template | converted |  |
| process_definition | Process_Negative_Stock_Template/create pids (System_DoNothing) | step create_pids of asset process_negative_stock_template | converted |  |
| process_definition | ReplenishShop/calculate replenishment (System_DoNothing) | step calculate_replenishment of asset replenishshop | converted |  |
| process_definition | ReplenishShop/confirmation truck loaded (System_DoNothing) | step confirmation_truck_loaded of asset replenishshop | converted |  |
| process_definition | ReplenishShop/pick orders (System_DoNothing) | step pick_orders of asset replenishshop | converted |  |
| process_definition | ReplenishShop/wait for truck (System_DoNothing) | step wait_for_truck of asset replenishshop | converted |  |

## Needs your input

The export cannot answer these; each answer changes what the converter generates. Work through them chain by chain.

### Forecast_Replenishment_Template

- [ ] What does the caller pass for P_AREA, P_REGION? (no value in the export; the Python output takes them as run config at launch)
- [ ] Is P_REGION the business key this chain runs for (a date, period, region or plant)? If so it can become a partition, so you can backfill and see which keys ran; if not, leave it as a parameter
- [ ] When does this chain run? The export has no submit frame or file event for it (it may be started by a person, another chain or an event)
- [ ] What raises event WaitForShop1, WaitForShop2, and should it be cleared after use? Dagster needs the producer (a sensor on a file or asset, or another chain) to model it
- [ ] Who owns this chain, and how fresh must its output be (an SLA)? Owners and freshness are added as Dagster metadata once known

### Full_FR_Run

- [ ] What does the caller pass for P_AREA, P_REGION? (no value in the export; the Python output takes them as run config at launch)
- [ ] Is P_REGION the business key this chain runs for (a date, period, region or plant)? If so it can become a partition, so you can backfill and see which keys ran; if not, leave it as a parameter
- [ ] When does this chain run? The export has no submit frame or file event for it (it may be started by a person, another chain or an event)
- [ ] What raises event WaitForShop1, WaitForShop2, and should it be cleared after use? Dagster needs the producer (a sensor on a file or asset, or another chain) to model it
- [ ] Who owns this chain, and how fresh must its output be (an SLA)? Owners and freshness are added as Dagster metadata once known

### Process_Negative_Stock_Template

- [ ] What does the caller pass for P_REGION? (no value in the export; the Python output takes them as run config at launch)
- [ ] Is P_REGION the business key this chain runs for (a date, period, region or plant)? If so it can become a partition, so you can backfill and see which keys ran; if not, leave it as a parameter
- [ ] When does this chain run? The export has no submit frame or file event for it (it may be started by a person, another chain or an event)
- [ ] Who owns this chain, and how fresh must its output be (an SLA)? Owners and freshness are added as Dagster metadata once known

### ReplenishShop

- [ ] What does the caller pass for P_AREA, P_REGION? (no value in the export; the Python output takes them as run config at launch)
- [ ] Is P_REGION the business key this chain runs for (a date, period, region or plant)? If so it can become a partition, so you can backfill and see which keys ran; if not, leave it as a parameter
- [ ] When does this chain run? The export has no submit frame or file event for it (it may be started by a person, another chain or an event)
- [ ] Who owns this chain, and how fresh must its output be (an SLA)? Owners and freshness are added as Dagster metadata once known

