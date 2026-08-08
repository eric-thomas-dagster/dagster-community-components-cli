# Hybrid runner image — deploy once, iterate by pushing flows to git.
#
# Base: python:3.12-slim + git (needed for the runtime clone). Everything
# else (dagster + dagster-cloud + script_orchestrator + gitpython) baked in
# via pip install.
FROM python:3.12-slim

# git required — runner clones the flows repo at code-location load time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/dagster/app

# Copy project files (project layout: src/hybrid_git_runner/...)
COPY pyproject.toml ./
COPY src/ ./src/

# Install the runner + all deps (pinned in pyproject.toml).
# (The upstream script_orchestrator __init__.py bug that previously required
# a workaround here was fixed in
# https://github.com/eric-thomas-dagster/script_scheduling_and_orchestration/commit/481edc0
# — pyproject.toml now pins script-orchestrator to a post-fix ref.)
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

# Dagster's convention — dagster-cloud looks for definitions at this
# module path per the dagster_cloud.yaml `code_source.module_name`.
ENV DAGSTER_HOME=/opt/dagster/dagster_home
RUN mkdir -p $DAGSTER_HOME

# Optional: if you're bundling airflow + prefect libs into the image
# (recommended — reproducible runs), install them here too so scripts
# have their orchestrator deps available at runtime.
# RUN pip install --no-cache-dir "apache-airflow==3.1.*" "prefect>=3.0"
