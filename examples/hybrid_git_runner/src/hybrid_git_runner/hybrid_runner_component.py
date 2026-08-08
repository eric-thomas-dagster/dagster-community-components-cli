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
  3. **Adds `install_flow_requirements`** — Tier 1 dep-overlay support.
     On every build_defs() call, checks the flows repo for
     `requirements.txt` at the repo root (or `pyproject.toml` with
     `[project.dependencies]`) — if present, runs `pip install` before
     the ScriptGithubComponent parses + emits assets. Matches Prefect
     Managed's `pull` step ergonomics.

Everything else (asset emission, lineage, freshness policies, dbt/Cosmos
support, per-file overrides) is inherited unchanged.
"""
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from script_orchestrator.components.script_github_component import ScriptGithubComponent

logger = logging.getLogger(__name__)


class HybridRunnerComponent(ScriptGithubComponent):
    """Hybrid runner: fetch flows from git at load time.

    Uses ScriptGithubComponent's clone + parse machinery, but with defaults
    tuned for the "runner container deployed once, code iterated via git
    push" pattern. Adds a requirements.txt / pyproject.toml auto-install
    hook so flows can pin their own deps without rebuilding the image.
    """

    # Force git-only mode. Local-scripts mode would defeat the whole point
    # of a container-baked runner.
    use_local: bool = False

    # Reproducible images — the base orchestrators are baked in at build
    # time, not auto-installed per run.
    airflow_auto_install: bool = False
    prefect_auto_install: bool = False

    # Auto-install any flow-repo-declared deps via requirements.txt /
    # pyproject.toml. Runs `pip install` inside the runner container at
    # code-location load. Turn off if you'd rather rebuild the runner
    # image (--registry mode) than allow runtime installs.
    install_flow_requirements: bool = True

    # Eagerly compute state on first container start (rather than
    # waiting for a manual state-refresh action). Without this, a
    # fresh runner container loads but the code location has no assets
    # until the customer clicks "Refresh state" in the UI. With this,
    # the first startup clones the flows repo + emits assets directly.
    # Turn off if you have a large flows repo where the clone would
    # slow container startup unacceptably (and you prefer to wire an
    # explicit schedule/sensor to refresh state).
    eagerly_compute_state: bool = True

    # When running in a Dagster+ branch deployment, prefer the PR's
    # branch name (from DAGSTER_CLOUD_GIT_BRANCH) over the configured
    # SCRIPTS_REPO_BRANCH — so the branch deployment shows the flows
    # from the PR's version of the flows repo. Only works when the
    # flows repo IS your Dagster project repo (Scenario A); for
    # separate flows-repo setups (Scenario B), custom GitHub Actions
    # → Dagster+ Branch Deployments API wiring is required. See README.
    match_branch_deployment: bool = True

    def build_defs(self, context):  # type: ignore[override]
        # If we're in a branch deployment, override the configured
        # repo_branch with the PR's branch — makes the branch
        # deployment show the flows from the PR's code state.
        if self.match_branch_deployment:
            resolved = _resolve_branch_for_deployment(self.repo_branch)
            if resolved != self.repo_branch:
                logger.info(
                    f"[HybridRunner] branch deployment detected — using "
                    f"repo branch {resolved!r} instead of {self.repo_branch!r}"
                )
                self.repo_branch = resolved

        # Dep install first — needed BEFORE ScriptGithubComponent parses
        # any flow files (which may import the newly-installed packages).
        if self.install_flow_requirements and not self.use_local:
            _install_flow_requirements(
                repo_url=self.repo_url,
                repo_branch=self.repo_branch,
                github_token=self.github_token,
            )
        # Eager-compute state so a fresh container start = assets appear.
        if self.eagerly_compute_state and not self.use_local:
            _eagerly_compute_state(component=self, context=context)
        return super().build_defs(context)


def _resolve_branch_for_deployment(configured_branch: str) -> str:
    """When running in a Dagster+ branch deployment, prefer the PR's
    branch name over the location's configured branch — so a PR against
    the Dagster project repo shows the flows from THAT PR's version of
    the flows repo (assumes flows-repo IS the Dagster project repo — the
    "Scenario A" pattern documented in the README).

    Env vars Dagster+ sets on the code-location container:
      DAGSTER_CLOUD_IS_BRANCH_DEPLOYMENT   "1" if this is a branch deployment
      DAGSTER_CLOUD_GIT_BRANCH             the PR's branch name

    Falls back to the configured branch if either is missing (i.e. we're
    running in prod, or Dagster+ didn't populate the branch env var).
    """
    if os.environ.get("DAGSTER_CLOUD_IS_BRANCH_DEPLOYMENT") != "1":
        return configured_branch
    pr_branch = os.environ.get("DAGSTER_CLOUD_GIT_BRANCH", "").strip()
    return pr_branch or configured_branch


def _eagerly_compute_state(*, component, context) -> None:
    """Compute the ScriptGithubComponent's state directly on first container
    start, so the code location emits assets immediately rather than
    waiting for a manual state-refresh action. Best-effort — a failure
    logs a warning but doesn't kill the code-location load.

    Uses the StateBackedComponent's own `refresh_state` method (async)
    so state ends up in the exact location the base class expects to
    read from — avoids "I wrote state to X, but the parent looks at Y"
    path-mismatch bugs.
    """
    import asyncio

    try:
        from dagster.components.utils.project_paths import get_local_state_path
    except ImportError:
        get_local_state_path = None  # type: ignore[assignment]

    try:
        # Determine project_root the same way StateBackedComponent does.
        # In our Docker container Dagster's WORKDIR is /opt/dagster/app;
        # ComponentLoadContext exposes a project_root but not on every
        # version — fall back to the WORKDIR.
        project_root = Path(os.environ.get("DAGSTER_APP_DIR", "/opt/dagster/app"))

        # Short-circuit if state is already computed (subsequent container
        # restarts should reuse the cached state, not re-clone every time).
        if get_local_state_path is not None:
            key = component.defs_state_config.key
            state_path = get_local_state_path(key, project_root)
            if state_path.exists():
                logger.info(f"[HybridRunner] state already cached at {state_path}")
                return

        logger.info("[HybridRunner] no state cached — computing on first load…")
        # refresh_state is async — run it synchronously here since build_defs
        # is sync. This blocks until the git clone + parse completes.
        version = asyncio.run(component.refresh_state(project_root=project_root))
        logger.info(f"[HybridRunner] state computed (version={version})")
    except Exception as e:
        logger.warning(f"[HybridRunner] eager state compute failed: {e}", exc_info=True)


def _install_flow_requirements(
    *,
    repo_url: Optional[str],
    repo_branch: str,
    github_token: Optional[str],
) -> None:
    """Clone the flows repo (shallow), check for requirements.txt or
    pyproject.toml at the root, pip install if present. Logs but doesn't
    raise — a failed install shouldn't kill the whole code-location load.
    """
    resolved_url = repo_url or os.environ.get("SCRIPTS_REPO_URL")
    if not resolved_url:
        return  # nothing to install from

    # Inject GitHub token for private repos.
    token = github_token or os.environ.get("GITHUB_TOKEN")
    if token and resolved_url.startswith("https://github.com/"):
        clone_url = resolved_url.replace(
            "https://github.com/",
            f"https://x-access-token:{token}@github.com/",
            1,
        )
    else:
        clone_url = resolved_url

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", repo_branch, clone_url, str(tmp_path / "repo")],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.warning(f"[HybridRunner] git clone failed for requirements check: {e}")
            return

        req_txt = tmp_path / "repo" / "requirements.txt"
        pyproject = tmp_path / "repo" / "pyproject.toml"

        install_args = None
        if req_txt.exists():
            logger.info(f"[HybridRunner] found requirements.txt — installing")
            install_args = [sys.executable, "-m", "pip", "install", "-r", str(req_txt), "--upgrade"]
        elif pyproject.exists():
            # Best-effort: try installing the project itself so
            # [project.dependencies] gets picked up.
            logger.info(f"[HybridRunner] found pyproject.toml — installing project deps")
            install_args = [sys.executable, "-m", "pip", "install", str(tmp_path / "repo")]

        if install_args is None:
            return

        try:
            result = subprocess.run(install_args, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.warning(f"[HybridRunner] pip install failed (rc={result.returncode}):")
                logger.warning(result.stderr[-2000:] if result.stderr else "(no stderr)")
            else:
                logger.info(f"[HybridRunner] pip install completed")
        except subprocess.TimeoutExpired:
            logger.warning("[HybridRunner] pip install timed out after 5 minutes")
