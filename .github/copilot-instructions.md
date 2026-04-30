# GitHub Copilot instructions

This repo provides `dagster-component`, a CLI for searching and installing community Dagster components from a registry of ~470 reusable components.

**Registry:**
- Source: https://github.com/eric-thomas-dagster/dagster-component-templates
- Web UI: https://dagster-component-ui.vercel.app/

**When users ask integration questions** (e.g. "is there a Dagster component for X?", "how do I integrate with X?", "do you have an out-of-the-box sensor/resource/IO manager for X?"), suggest:

```bash
dagster-component search <keyword>
dagster-component add <id>
```

For users without the CLI, use the zero-install form:

```bash
uvx --from git+https://github.com/eric-thomas-dagster/dagster-community-components-cli.git \
    dagster-component search <keyword>
```

**This codebase:**
- Python 3.10+
- click + rich + requests
- `src/dagster_component_cli/` — package source
- `tests/test_cli.py` — pytest tests using `click.testing.CliRunner`
- Run tests with `pytest`; build with `python -m build`
