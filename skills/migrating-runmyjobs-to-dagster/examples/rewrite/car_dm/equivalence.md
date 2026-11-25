# Equivalence between the converter route and the rewrite route

Behavior, not code text. Each side is measured against the source facts in the spec; a dimension is *equivalent* when both sides match them (or differ identically). What replaced a precondition, status handler, event or notification is not machine-compared: the two statements are shown side by side. Also not machine-compared: timeouts, run windows, success exit codes and sensor settings (poll interval, minimum size and age): read them in the spec and in each side's YAML.

## `NIGHTLY`

- assets: converter 2 (nightly__extract_1, nightly__load_1); rewrite 2 (extract, load)
- asset checks: converter 0, rewrite 0
- equivalent on every machine-checked dimension: **yes**

| Dimension | Source | Converter | Rewrite | Verdict |
|---|---|---|---|---|
| graph order (earlier -> later pairs kept) | 1 pair(s) | all kept | all kept | equivalent |
| schedules (cron, time zone) | [('30 2 * * *', 'UTC')] | [('30 2 * * *', 'UTC')] | [('30 2 * * *', 'UTC')] | equivalent |
| pools | none | [] | [] | equivalent |
| retries | per call | match | match | equivalent |
| commands and SQL present | 2 command(s) | all present | all present | equivalent |
| loud stubs left | none wanted | none | none | equivalent |

Not machine-compared:

- converter: chain-level restart count is not converted
- converter: step 'load' precondition {'time_window': 'BIZ'} not enforced
- rewrite, step load: time window BIZ must be open: dropped: time window BIZ is not defined in the export, so it cannot be enforced; needs its hours from the owner
- rewrite, step extract: on status Error: restart_step, max=2: replaced by retry_policy_max_retries 2 on `extract`
