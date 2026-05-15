.PHONY: help install install-pi bootstrap login pi smoke-pi test test-live lint lint-fix typecheck typecheck-mypy typecheck-pyright format all clean clean-sandbox clean-all

PYTHON_311 := /opt/miniforge/envs/base-py3-11/bin/python3.11
VENV := local.venv
VENV_PY := $(VENV)/bin/python
VENV_PYTEST := $(VENV)/bin/pytest
VENV_RUFF := $(VENV)/bin/ruff
VENV_MYPY := $(VENV)/bin/mypy
VENV_PYRIGHT := $(VENV)/bin/pyright

help:
	@echo "libharness targets:"
	@echo ""
	@echo "  Python environment:"
	@echo "    install         — create 3.11 venv at local.venv/ and install dev deps"
	@echo ""
	@echo "  Pi sandbox (external tool — Node + pi-coding-agent):"
	@echo "    install-pi      — install sandboxed Node + pi into .sandbox/"
	@echo "    login           — open the sandboxed pi TUI for /login"
	@echo "    pi              — run the sandboxed pi CLI (passthrough)"
	@echo "    smoke-pi        — confirm pi RPC mode responds"
	@echo ""
	@echo "  Setup:"
	@echo "    bootstrap       — install + install-pi"
	@echo ""
	@echo "  Tests:"
	@echo "    test            — pytest, excluding live marker"
	@echo "    test-live       — pytest with live marker (requires pi + auth)"
	@echo ""
	@echo "  Quality:"
	@echo "    lint            — ruff check"
	@echo "    lint-fix        — ruff check --fix"
	@echo "    typecheck       — mypy + pyright"
	@echo "    format          — ruff format"
	@echo "    all             — lint + typecheck + test"
	@echo ""
	@echo "  Cleanup:"
	@echo "    clean           — remove Python build/cache artifacts"
	@echo "    clean-sandbox   — remove .sandbox/ (Node + pi)"
	@echo "    clean-all       — clean + clean-sandbox + local.venv"

install:
	$(PYTHON_311) -m venv $(VENV)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -e '.[dev]'

install-pi:
	scripts/setup-node.sh
	scripts/setup-pi.sh

bootstrap: install install-pi
	@echo
	@echo "Sandbox + venv ready. Next:"
	@echo "  make login       (one-time OAuth)"
	@echo "  make test"

login:
	scripts/login.sh

pi:
	scripts/pi.sh $(ARGS)

smoke-pi:
	scripts/smoke-pi.sh

test:
	scripts/test.sh -m "not live"

test-live:
	scripts/test.sh -m live

lint:
	$(VENV_RUFF) check src/ tests/

lint-fix:
	$(VENV_RUFF) check --fix src/ tests/

typecheck: typecheck-mypy typecheck-pyright

typecheck-mypy:
	$(VENV_MYPY) src/

typecheck-pyright:
	$(VENV_PYRIGHT) src/

format:
	$(VENV_RUFF) format src/ tests/

all: lint typecheck test

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +

clean-sandbox:
	scripts/clean.sh --yes

clean-all: clean clean-sandbox
	rm -rf $(VENV)
