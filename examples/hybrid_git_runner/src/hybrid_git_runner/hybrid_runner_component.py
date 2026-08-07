"""HybridRunnerComponent — light subclass of ScriptGithubComponent, tuned for Hybrid.

The base primitive (`ScriptGithubComponent`) is a `StateBackedComponent`
that clones a git repo, parses Airflow DAGs / Prefect flows / plain
Python scripts, and emits Dagster assets. We layer a subclass here that:

  1. Forces `use_local=False` — a Hybrid runner ALWAYS fetches from git.
     No path where you accidentally point at a local dir baked into an
     image that no one else can see.
  2. Turns OFF `airflow_auto_install` + `prefect_auto_install` by default.
     Auto-install at runtime = the container's dep set changes across
     runs. For reproducible images, deps should be baked into the image
     at build time. If users want auto-install, override in defs.yaml
     explicitly.
  3. Exposes the same config surface as the base — no new fields. Users
     configure via env vars on the Dagster+ location (SCRIPTS_REPO_URL,
     SCRIPTS_REPO_BRANCH, SCRIPTS_DIR, GITHUB_TOKEN).

Everything else (asset emission, lineage, freshness policies, dbt/Cosmos
support, per-file overrides) is inherited unchanged.
"""
from script_orchestrator.components.script_github_component import ScriptGithubComponent


class HybridRunnerComponent(ScriptGithubComponent):
    """Hybrid runner: fetch flows from git at load time.

    Uses ScriptGithubComponent's clone + parse machinery, but with defaults
    tuned for the "runner container deployed once, code iterated via git
    push" pattern.
    """

    # Force git-only mode. Local-scripts mode would defeat the whole point
    # of a container-baked runner.
    use_local: bool = False

    # Reproducible images — deps baked in at build time, not auto-installed
    # per run. Override in defs.yaml if you want runtime auto-install.
    airflow_auto_install: bool = False
    prefect_auto_install: bool = False
