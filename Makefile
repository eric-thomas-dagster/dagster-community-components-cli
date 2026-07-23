.PHONY: help test ruff pyright check fix

help:
	@echo "Available targets:"
	@echo "  make test      — run pytest"
	@echo "  make ruff      — run ruff lint"
	@echo "  make pyright   — run pyright type check"
	@echo "  make check     — run ruff + pyright + test (matches upstream community-integrations 'make check')"
	@echo "  make fix       — auto-fix ruff issues"

test:
	uv run pytest tests/

ruff:
	uvx ruff check src tests

pyright:
	uvx pyright src

check: ruff pyright test

fix:
	uvx ruff check --fix src tests
	uvx ruff format src tests
