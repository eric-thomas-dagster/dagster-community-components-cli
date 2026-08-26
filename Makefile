.PHONY: help test ruff pyright check fix search-qa

help:
	@echo "Available targets:"
	@echo "  make test        — run pytest"
	@echo "  make ruff        — run ruff lint"
	@echo "  make pyright     — run pyright type check"
	@echo "  make search-qa   — assert canonical queries return expected components in top-5"
	@echo "  make check       — run ruff + pyright + test + search-qa"
	@echo "  make fix         — auto-fix ruff issues"

test:
	uv run pytest tests/

ruff:
	uvx ruff check src tests

pyright:
	uvx pyright src

search-qa:
	PYTHONPATH=src python3 tools/search_qa.py --top 5

check: ruff pyright test search-qa

fix:
	uvx ruff check --fix src tests
	uvx ruff format src tests
