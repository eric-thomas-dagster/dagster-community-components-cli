# Equivalence between the converter route and the rewrite route

Behavior, not code text. Each side is measured against the source facts in the spec; a dimension is *equivalent* when both sides match them (or differ identically). What replaced a precondition, status handler, event or notification is not machine-compared: the two statements are shown side by side. Also not machine-compared: timeouts, run windows, success exit codes and sensor settings (poll interval, minimum size and age): read them in the spec and in each side's YAML.

## `FIN_NIGHTLY`

- assets: converter 1 (fin_nightly); rewrite 3 (fin_publish, fin_pull, fin_transform)
- asset checks: converter 0, rewrite 0
- equivalent on every machine-checked dimension: **yes**

| Dimension | Source | Converter | Rewrite | Verdict |
|---|---|---|---|---|
| graph order (earlier -> later pairs kept) | 2 pair(s) | all kept | all kept | equivalent |
| schedules (cron, time zone) | [('30 22 * * *', 'America/New_York')] | [('30 22 * * *', 'America/New_York')] | [('30 22 * * *', 'America/New_York')] | equivalent |
| pools | ['batch_queue'] | ['batch_queue'] | ['batch_queue'] | equivalent |
| retries | per call | match | match | equivalent |
| commands and SQL present | 3 command(s) | all present | all present | equivalent |
| file events (a sensor each) | ['FIN_FILE_ARRIVED'] | all present | all present | equivalent |
| loud stubs left | none wanted | none | none | equivalent |

## `FIN_MIXED_RETRY`

- assets: converter 2 (fin_mixed_retry__alone, fin_mixed_retry__pull); rewrite 2 (mixed_alone, mixed_pull)
- asset checks: converter 0, rewrite 0
- equivalent on every machine-checked dimension: **yes**

| Dimension | Source | Converter | Rewrite | Verdict |
|---|---|---|---|---|
| graph order (earlier -> later pairs kept) | 1 pair(s) | all kept | all kept | equivalent |
| schedules (cron, time zone) | none | [] | [] | equivalent |
| pools | ['batch_queue'] | ['batch_queue'] | ['batch_queue'] | equivalent |
| retries | per call | match | match | equivalent |
| commands and SQL present | 2 command(s) | all present | all present | equivalent |
| file events (a sensor each) | ['FIN_FILE_NO_CHECKS'] | all present | all present | equivalent |
| loud stubs left | none wanted | none | none | equivalent |
