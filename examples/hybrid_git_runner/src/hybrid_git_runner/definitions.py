"""Hybrid runner — one container, code fetched from git at load time.

Deploy this once to Dagster+ Hybrid. Then push new .py flows to your
`SCRIPTS_REPO_URL` repo and Dagster+ picks them up on the next code-
location load. No Docker rebuild per iteration.

Wraps `ScriptGithubComponent` from
https://github.com/eric-thomas-dagster/script_scheduling_and_orchestration
— a StateBackedComponent that clones a git repo, discovers Airflow DAGs
+ Prefect flows + plain Python scripts, and emits Dagster assets for
each.

Configure via environment variables (set on the Dagster+ code location
env vars page):
  SCRIPTS_REPO_URL      — HTTPS git repo containing your flows (required)
  SCRIPTS_REPO_BRANCH   — branch to track (default: main)
  SCRIPTS_DIR           — subdir inside the repo (default: scripts)
  GITHUB_TOKEN          — for private repos (optional)
  AIRFLOW_ENABLED       — bool (default: true)  — set false if repo has no Airflow DAGs
  PREFECT_ENABLED       — bool (default: true)  — set false if repo has no Prefect flows

The component is a `StateBackedComponent`, so the git clone + parse
happens once per state refresh (not on every asset materialization).
Subsequent loads reuse the cached state until you trigger a refresh.
"""
import os
from pathlib import Path

import dagster as dg
from dagster import load_from_defs_folder


defs = load_from_defs_folder(project_root=Path(__file__).parent.parent.parent)
