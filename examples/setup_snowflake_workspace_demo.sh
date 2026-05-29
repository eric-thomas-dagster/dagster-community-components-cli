#!/usr/bin/env bash
# setup_snowflake_workspace_demo.sh
#
# Friendly redirect — the snowflake_workspace demo is a TWO-STEP interactive
# flow (seed.sh provisions Snowflake + optional AWS Iceberg/Snowpipe, then
# bootstrap.sh scaffolds the Dagster project). Neither step can run from a
# `curl | bash` pipe because both prompt for credentials, auth method, and
# governance fixes.
#
# This wrapper exists so the UI's auto-generated
#   curl … setup_snowflake_workspace_demo.sh | bash
# command surfaces clear next-step instructions instead of a cryptic
# "needs TTY" error.

cat <<'INSTRUCTIONS'
─────────────────────────────────────────────────────────────────────
  Dagster + Snowflake — full-surface demo (two interactive steps)
─────────────────────────────────────────────────────────────────────

This demo is two scripts, both interactive (prompt for Snowflake
credentials, auth method, AWS setup). They can't run from a piped
curl. Run them like this:

  # 1. Provision Snowflake (+ optionally AWS for Iceberg / Snowpipe).
  curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/seed.sh -o seed.sh
  curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/seed.sql -o seed.sql
  chmod +x seed.sh
  ./seed.sh
  # ↑ prompts for credentials + auth method, runs Day-0 governance
  #   probes, creates DAGSTER_DEMO with ~30 Snowflake entities + a
  #   scoped DAGSTER_RUNNER role. Writes .env for step 2 to consume.

  # 2. Scaffold the Dagster project (reads .env from step 1).
  curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/bootstrap.sh -o bootstrap.sh
  chmod +x bootstrap.sh
  ./bootstrap.sh
  # ↑ creates snowflake-demo/ with ~30 assets covering every
  #   Snowflake-native primitive. Use --lean for the minimum spine.

  # 3. Run it.
  cd snowflake-demo
  uv run dg dev
  # ↑ opens http://localhost:3000

──── Full walkthrough ───────────────────────────────────────────────
  https://github.com/eric-thomas-dagster/dagster-community-components-cli/blob/main/examples/snowflake_workspace.md

──── Account permission matrix (what your Snowflake admin grants) ───
  https://github.com/eric-thomas-dagster/dagster-community-components-cli/blob/main/examples/snowflake_demo_account_requirements.md

INSTRUCTIONS
