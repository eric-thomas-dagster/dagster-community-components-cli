"""CLI to search, install, and manage Dagster community components."""

# Read the installed package version dynamically so `dagster-component --version`
# stays honest across releases (previous static assignment silently drifted
# — every 0.8.x release still reported "0.7.1" until this fix).
try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("dagster-community-components-cli")
except Exception:  # noqa: BLE001
    # Uninstalled (e.g. running from a git checkout without editable install).
    # Fall back to a sentinel that makes the drift obvious.
    __version__ = "0.0.0+unknown"

DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/"
    "eric-thomas-dagster/dagster-component-templates/main/manifest.json"
)
