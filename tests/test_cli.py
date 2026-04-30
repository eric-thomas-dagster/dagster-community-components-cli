"""Smoke tests for the CLI — run with `pytest`."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from dagster_component_cli.cli import main
from dagster_component_cli.installer import file_url_for, parse_requirements
from dagster_component_cli.project import (
    CATEGORY_DIRS,
    find_project_root,
    resolve_install_dir,
)
from dagster_component_cli.registry import Registry


# ── fixtures ──────────────────────────────────────────────────────────────────


SAMPLE_MANIFEST = {
    "version": "1.0.0",
    "components": [
        {
            "id": "s3_parquet_io_manager",
            "name": "S3 Parquet IO Manager",
            "category": "io_manager",
            "description": "Stores assets as Parquet files on Amazon S3.",
            "tags": ["io_manager", "s3", "parquet"],
            "component_url": "https://raw.example.com/io_managers/s3_parquet_io_manager/component.py",
            "readme_url": "https://raw.example.com/io_managers/s3_parquet_io_manager/README.md",
        },
        {
            "id": "postgres_resource",
            "name": "PostgreSQL Resource",
            "category": "resource",
            "description": "Connect to a PostgreSQL database.",
            "tags": ["resource", "postgres", "database"],
            "component_url": "https://raw.example.com/resources/postgres_resource/component.py",
        },
    ],
}


def _registry_with(manifest):
    """Return a Registry whose .manifest is preset (skips network)."""
    r = Registry(url="https://example.invalid/manifest.json")
    r._manifest = manifest
    return r


# ── registry ──────────────────────────────────────────────────────────────────


def test_registry_get():
    r = _registry_with(SAMPLE_MANIFEST)
    c = r.get("s3_parquet_io_manager")
    assert c is not None
    assert c["category"] == "io_manager"
    assert r.get("does_not_exist") is None


def test_registry_search_by_id():
    r = _registry_with(SAMPLE_MANIFEST)
    matches = r.search("postgres")
    assert len(matches) == 1
    assert matches[0]["id"] == "postgres_resource"


def test_registry_search_by_tag():
    r = _registry_with(SAMPLE_MANIFEST)
    matches = r.search("parquet")
    assert {m["id"] for m in matches} == {"s3_parquet_io_manager"}


def test_registry_search_filter_by_category():
    r = _registry_with(SAMPLE_MANIFEST)
    matches = r.search("", category="resource")
    assert {m["id"] for m in matches} == {"postgres_resource"}


def test_registry_categories():
    r = _registry_with(SAMPLE_MANIFEST)
    cats = dict(r.categories())
    assert cats == {"io_manager": 1, "resource": 1}


# ── installer helpers ─────────────────────────────────────────────────────────


def test_file_url_for_swaps_filename():
    component = {
        "id": "x",
        "component_url": "https://raw.example.com/path/to/component.py",
    }
    assert file_url_for(component, "README.md") == "https://raw.example.com/path/to/README.md"
    assert file_url_for(component, "io_manager.py") == "https://raw.example.com/path/to/io_manager.py"


def test_parse_requirements(tmp_path: Path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "# comment line\n"
        "dagster\n"
        "\n"
        "pandas>=1.5.0\n"
        "  # leading whitespace comment\n"
        "  pyarrow  \n"
    )
    assert parse_requirements(tmp_path) == ["dagster", "pandas>=1.5.0", "pyarrow"]


def test_parse_requirements_missing_file(tmp_path: Path):
    assert parse_requirements(tmp_path) == []


# ── project ───────────────────────────────────────────────────────────────────


def test_find_project_root_via_pyproject(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert find_project_root(sub) == tmp_path


def test_find_project_root_via_defs_dir(tmp_path: Path):
    (tmp_path / "defs").mkdir()
    sub = tmp_path / "deeper"
    sub.mkdir()
    assert find_project_root(sub) == tmp_path


def test_find_project_root_returns_none_when_absent(tmp_path: Path):
    assert find_project_root(tmp_path) is None


def test_resolve_install_dir_uses_category_mapping(tmp_path: Path):
    component = {"id": "s3_parquet_io_manager", "category": "io_manager"}
    target = resolve_install_dir(tmp_path, component)
    assert target == tmp_path / "components" / CATEGORY_DIRS["io_manager"] / "s3_parquet_io_manager"


def test_resolve_install_dir_explicit_target_overrides(tmp_path: Path):
    component = {"id": "x", "category": "resource"}
    target = resolve_install_dir(tmp_path, component, target_dir=str(tmp_path / "custom"))
    assert target == tmp_path / "custom"


# ── CLI smoke tests ───────────────────────────────────────────────────────────


def test_cli_version():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "dagster-component" in result.output


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Search, install, and manage" in result.output


def test_cli_info_not_found():
    runner = CliRunner()
    with patch("dagster_component_cli.cli.Registry") as MockReg:
        MockReg.return_value = _registry_with(SAMPLE_MANIFEST)
        result = runner.invoke(main, ["info", "does_not_exist"])
    assert result.exit_code == 1


def test_cli_info_found():
    runner = CliRunner()
    with patch("dagster_component_cli.cli.Registry") as MockReg:
        MockReg.return_value = _registry_with(SAMPLE_MANIFEST)
        result = runner.invoke(main, ["info", "postgres_resource"])
    assert result.exit_code == 0
    assert "postgres_resource" in result.output
    assert "PostgreSQL Resource" in result.output


def test_cli_search_finds_matches():
    runner = CliRunner()
    with patch("dagster_component_cli.cli.Registry") as MockReg:
        MockReg.return_value = _registry_with(SAMPLE_MANIFEST)
        result = runner.invoke(main, ["search", "postgres"])
    assert result.exit_code == 0
    assert "postgres_resource" in result.output


def test_cli_search_no_matches():
    runner = CliRunner()
    with patch("dagster_component_cli.cli.Registry") as MockReg:
        MockReg.return_value = _registry_with(SAMPLE_MANIFEST)
        result = runner.invoke(main, ["search", "completely_unrelated_xyz"])
    assert result.exit_code == 0
    assert "No components match" in result.output


# ── init ──────────────────────────────────────────────────────────────────────


def test_cli_init_writes_three_files(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--target-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / ".cursorrules").exists()
    assert (tmp_path / ".github" / "copilot-instructions.md").exists()
    assert "dagster-component" in (tmp_path / "CLAUDE.md").read_text()


def test_cli_init_skips_existing_without_force(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("existing content")
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--target-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "CLAUDE.md").read_text() == "existing content"
    assert "exists" in result.output


def test_cli_init_overwrites_with_force(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("existing content")
    runner = CliRunner()
    result = runner.invoke(main, ["init", "--target-dir", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert "existing content" not in (tmp_path / "CLAUDE.md").read_text()
    assert "Dagster" in (tmp_path / "CLAUDE.md").read_text()


def test_cli_init_skip_flags(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["init", "--target-dir", str(tmp_path), "--no-cursor", "--no-copilot"],
    )
    assert result.exit_code == 0
    assert (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / ".cursorrules").exists()
    assert not (tmp_path / ".github" / "copilot-instructions.md").exists()
