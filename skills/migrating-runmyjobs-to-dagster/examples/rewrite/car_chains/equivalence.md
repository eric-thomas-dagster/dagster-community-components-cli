# Equivalence between the converter route and the rewrite route

Behavior, not code text. Each side is measured against the source facts in the spec; a dimension is *equivalent* when both sides match them (or differ identically). What replaced a precondition, status handler, event or notification is not machine-compared: the two statements are shown side by side. Also not machine-compared: timeouts, run windows, success exit codes and sensor settings (poll interval, minimum size and age): read them in the spec and in each side's YAML.

## `ReplenishShop`

- assets: converter 1 (replenishshop); rewrite 4 (calculate_replenishment, confirmation_truck_loaded, pick_orders, wait_for_truck)
- asset checks: converter 0, rewrite 0
- equivalent on every machine-checked dimension: **no**

| Dimension | Source | Converter | Rewrite | Verdict |
|---|---|---|---|---|
| graph order (earlier -> later pairs kept) | 3 pair(s) | all kept | all kept | equivalent |
| schedules (cron, time zone) | none | [] | [] | equivalent |
| pools | none | [] | [] | equivalent |
| retries | per call | match | match | equivalent |
| commands and SQL present | 4 command(s) | all present | ['ReplenishShop/wait for truck', 'ReplenishShop/calculate replenishment', 'ReplenishShop/pick orders', 'ReplenishShop/confirmation truck loaded'] | converter matches the source |
| loud stubs left | none wanted | none | none | equivalent |

## `Process_Negative_Stock_Template`

- assets: converter 1 (process_negative_stock_template); rewrite 2 (count_negative_stock, create_pids)
- asset checks: converter 0, rewrite 0
- equivalent on every machine-checked dimension: **no**

| Dimension | Source | Converter | Rewrite | Verdict |
|---|---|---|---|---|
| graph order (earlier -> later pairs kept) | 1 pair(s) | all kept | all kept | equivalent |
| schedules (cron, time zone) | none | [] | [] | equivalent |
| pools | none | [] | [] | equivalent |
| retries | per call | match | match | equivalent |
| commands and SQL present | 2 command(s) | all present | ['Process_Negative_Stock_Template/count negative stock', 'Process_Negative_Stock_Template/create pids'] | converter matches the source |
| loud stubs left | none wanted | none | none | equivalent |
