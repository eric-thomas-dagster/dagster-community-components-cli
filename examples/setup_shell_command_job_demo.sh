#!/usr/bin/env bash
# shell_command_job demo — schedules a shell maintenance task as a Dagster
# op job (no asset materialized).
#
# Demo task: counts files in /tmp older than 1 day. Verifies subprocess +
# env_vars + exit-code handling end-to-end.

set -euo pipefail
PROJECT_DIR="${1:-shell-command-job-demo}"

echo ">>> Scaffolding"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing component"
$CLI add shell_command_job --auto-install

cat > "src/$PKG/defs/shell_command_job/defs.yaml" <<EOF
type: $PKG.components.shell_command_job.component.ShellCommandJobComponent
attributes:
  job_name: count_old_tmp_files
  schedule: "0 3 * * *"
  default_status: STOPPED
  command: 'echo "Counting old /tmp files..."; find /tmp -maxdepth 2 -type f -mtime +0 2>/dev/null | wc -l | xargs echo "old_files_count:"'
  timeout_seconds: 30
  fail_on_nonzero: true
EOF

cat <<MSG

>>> Setup complete.

Materialize the job once to test:
    cd $PROJECT_DIR && uv run dg launch --job count_old_tmp_files

Or open the UI to see the schedule:
    cd $PROJECT_DIR && uv run dg dev
MSG
