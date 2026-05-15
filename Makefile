.PHONY: help install install-pi bootstrap login pi smoke-pi test test-live \
        lint lint-fix typecheck typecheck-mypy typecheck-pyright format \
        lint-md lint-md-tables format-md format-md-check \
        all clean clean-sandbox clean-all

PYTHON_311 := /opt/miniforge/envs/base-py3-11/bin/python3.11
VENV := local.venv
VENV_PY := $(VENV)/bin/python
VENV_PYTEST := $(VENV)/bin/pytest
VENV_RUFF := $(VENV)/bin/ruff
VENV_MYPY := $(VENV)/bin/mypy
VENV_PYRIGHT := $(VENV)/bin/pyright

# Markdown toolchain: mdformat (Python, in the dev venv) for formatting;
# markdownlint-cli2 (Node, in /opt/miniforge/envs/dev-tools/) for linting.
# Explicit absolute paths for both — see CLAUDE.md "explicit tool paths".
# `dev-tools` is a Node-only conda env; never reference it as a Python source.
NODE              := /opt/miniforge/envs/dev-tools/bin/node
MARKDOWNLINT_CLI2 := /opt/miniforge/envs/dev-tools/bin/markdownlint-cli2
TABLE_MAX_COLS    := 150

# Markdown discovery: filesystem walk of these roots plus any *.md at the
# project root. Not git-aware on purpose — works in fresh checkouts.
MD_PATHS := docs dev-notes scripts
MD_FILES := $$(find $(MD_PATHS) -name '*.md' 2>/dev/null) $(wildcard *.md)

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
	@echo "  Quality (code):"
	@echo "    lint            — ruff check"
	@echo "    lint-fix        — ruff check --fix"
	@echo "    typecheck       — mypy + pyright"
	@echo "    format          — ruff format"
	@echo "    all             — lint + typecheck + test"
	@echo ""
	@echo "  Quality (markdown — run on demand, not part of \`all\`):"
	@echo "    lint-md         — markdownlint-cli2 + table-line-length check"
	@echo "    lint-md-tables  — table-line-length check only (the 150-col rule)"
	@echo "    format-md       — mdformat --wrap keep on all discovered .md files"
	@echo "    format-md-check — mdformat --check (CI-style)"
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

# --- markdown ---------------------------------------------------------------
#
# Per-file workflow recommended for live edits (see CLAUDE.md):
#   $(VENV_PY) -m mdformat --wrap keep <FILE>
#   $(NODE) $(MARKDOWNLINT_CLI2) <FILE>
#
# The Make targets below are the occasional-batch counterpart. They are
# NOT part of `make all` so the default dev loop stays fast.

# 150-column-on-tables check: markdownlint's MD013 has only one line_length
# setting, so we can't enforce a length on tables while leaving prose alone.
# This awk pass flags any line whose first non-whitespace character is `|`
# (i.e. table rows, headers, separators).
lint-md-tables:
	@awk -v max=$(TABLE_MAX_COLS) ' \
	    /^[[:space:]]*\|/ { \
	        if (length > max) { \
	            printf "%s:%d:%d error table-line-length (actual: %d, max: %d)\n", \
	                FILENAME, FNR, max+1, length, max; \
	            failed = 1 \
	        } \
	    } \
	    END { exit failed }' $(MD_FILES)

lint-md:
	@test -x $(MARKDOWNLINT_CLI2) || { \
	    echo "markdownlint-cli2 not found at $(MARKDOWNLINT_CLI2)" >&2; \
	    echo "fix: conda create -n dev-tools -c conda-forge nodejs markdownlint-cli2" >&2; \
	    exit 127; }
	@test -x $(NODE) || { \
	    echo "node not found at $(NODE)" >&2; \
	    exit 127; }
	$(NODE) $(MARKDOWNLINT_CLI2) $(MD_FILES)
	@$(MAKE) --no-print-directory lint-md-tables

format-md:
	$(VENV_PY) -m mdformat --wrap keep $(MD_FILES)

format-md-check:
	$(VENV_PY) -m mdformat --wrap keep --check $(MD_FILES)

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
