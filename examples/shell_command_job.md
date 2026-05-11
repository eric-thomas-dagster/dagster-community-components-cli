# Shell Command Job demo

A scheduled shell-command op job — no asset materialized. Cron-driven file
cleanup, status pings, ad-hoc maintenance.

```
@dg.job
  └─ runs `bash -c "<your command>"` once per schedule tick
```

## Components used

| # | Component | Category | Role |
|---|---|---|---|
| 1 | [`shell_command_job`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/jobs/shell_command_job) | infrastructure | Single op job; subprocess wrapper |

## Demo command

Counts files in `/tmp` older than 1 day. Replace with anything: log rotation,
DB vacuum, cache flush, file cleanup, deploy hooks.

```yaml
job_name: count_old_tmp_files
schedule: "0 3 * * *"
command: 'find /tmp -maxdepth 2 -type f -mtime +0 | wc -l | xargs echo "old_files_count:"'
timeout_seconds: 30
fail_on_nonzero: true
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_shell_command_job_demo.sh | bash
cd shell-command-job-demo
uv run dg launch --job count_old_tmp_files
```

## Why "op job, not asset"

This work doesn't model a tracked artifact. There's no DataFrame to materialize,
no key worth lineage. Pretending it's an asset (e.g. via [`shell_command_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/infrastructure/shell_command_asset))
adds a fake catalog entry that drifts from reality every run.
