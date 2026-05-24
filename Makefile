.PHONY: help install install-311 install-ft install-pi install-pi-node-deprecated bootstrap login pi smoke-pi \
        test test-311 test-ft test-live test-live-311 test-live-ft \
        lint lint-311 lint-ft lint-fix \
        typecheck typecheck-mypy typecheck-mypy-311 typecheck-mypy-ft \
        typecheck-pyright typecheck-pyright-311 typecheck-pyright-ft \
        format \
        lint-md lint-md-tables format-md format-md-check \
        all all-311 all-ft clean clean-sandbox clean-all

# --- interpreter sources ----------------------------------------------------
# Two supported runtimes (see dev-notes/2026-05-15-concurrency-model-discussion.md):
#   311  — standard CPython 3.11 (default; what `make all` historically used)
#   ft   — CPython 3.14 free-threading build (3.14t, GIL disabled)
PYTHON_311  := /opt/miniforge/envs/base-py3-11/bin/python3.11
PYTHON_314T := /opt/miniforge/envs/base-py3-14-nogil/bin/python3.14t

# --- venvs ------------------------------------------------------------------
VENV     := local.venv
VENV_FT  := local-ft.venv

# Tool paths per venv. Both venvs install the same dev extras from
# pyproject.toml; tool binaries live in each venv's bin/.
PY_311      := $(VENV)/bin/python
PYTEST_311  := $(VENV)/bin/pytest
RUFF_311    := $(VENV)/bin/ruff
MYPY_311    := $(VENV)/bin/mypy
PYRIGHT_311 := $(VENV)/bin/pyright

PY_FT       := $(VENV_FT)/bin/python
PYTEST_FT   := $(VENV_FT)/bin/pytest
RUFF_FT     := $(VENV_FT)/bin/ruff
MYPY_FT     := $(VENV_FT)/bin/mypy
PYRIGHT_FT  := $(VENV_FT)/bin/pyright

# Markdown toolchain: mdformat (Python, in the 3.11 dev venv) for formatting;
# markdownlint-cli2 (Node, in /opt/miniforge/envs/dev-tools/) for linting.
# Explicit absolute paths for both — see CLAUDE.md "explicit tool paths".
# `dev-tools` is a Node-only conda env; never reference it as a Python source.
NODE              := /opt/miniforge/envs/dev-tools/bin/node
MARKDOWNLINT_CLI2 := /opt/miniforge/envs/dev-tools/bin/markdownlint-cli2
TABLE_MAX_COLS    := 150

# Markdown discovery: filesystem walk of these roots plus any *.md at the
# project root. Not git-aware on purpose — works in fresh checkouts.
# `dev-notes/predecessors/` is excluded: those are vendored AI-iteration
# artifacts (see dev-notes/predecessors/README.md "Editorial policy") and
# must not be reformatted in-place.
MD_PATHS := docs dev-notes scripts
MD_FILES := $$(find $(MD_PATHS) -name '*.md' \
                    -not -path 'dev-notes/predecessors/v*/*' \
                    2>/dev/null) $(wildcard *.md)

help:
	@echo "libharness targets:"
	@echo ""
	@echo "  Python environments (two supported runtimes — 3.11 and 3.14t):"
	@echo "    install         — install both venvs (install-311 + install-ft)"
	@echo "    install-311     — create 3.11 venv at local.venv/ and install dev deps"
	@echo "    install-ft      — create 3.14t freethreaded venv at local-ft.venv/"
	@echo ""
	@echo "  Pi runtime (the precompiled binary published by earendil-works):"
	@echo "    install-pi      — fetch + sha256-verify + extract pi $$($(PY_311) -m libharness._pi_vendor --version 2>/dev/null || echo 0.75.5)"
	@echo "                      into src/libharness/_vendor/pi/ (the production path)"
	@echo "    login           — open the sandboxed pi TUI for /login"
	@echo "    pi              — run pi via the resolver (vendored / LIBHARNESS_PI_PATH)"
	@echo "    smoke-pi        — confirm pi RPC mode responds"
	@echo ""
	@echo "  Pi (legacy — node-pi via npm; will be removed once binary path is vetted):"
	@echo "    install-pi-node-deprecated — install sandboxed Node + pi into .sandbox/"
	@echo ""
	@echo "  Setup:"
	@echo "    bootstrap       — install + install-pi"
	@echo ""
	@echo "  Tests (run against both venvs by default):"
	@echo "    test            — pytest -m 'not live' on both venvs"
	@echo "    test-311        — pytest on 3.11 only"
	@echo "    test-ft         — pytest on 3.14t only"
	@echo "    test-live       — pytest -m live on both venvs"
	@echo ""
	@echo "  Quality (code, run against both venvs by default):"
	@echo "    lint            — ruff check on both venvs"
	@echo "    lint-fix        — ruff check --fix (3.11 only; formatter is single-source)"
	@echo "    typecheck       — mypy + pyright on both venvs"
	@echo "    format          — ruff format (3.11 only)"
	@echo "    all             — lint + typecheck + test on both venvs"
	@echo "    all-311 / all-ft — same, one venv only"
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
	@echo "    clean-all       — clean + clean-sandbox + both venvs"

# --- install ---------------------------------------------------------------

install: install-311 install-ft

install-311:
	$(PYTHON_311) -m venv $(VENV)
	$(PY_311) -m pip install --upgrade pip
	$(PY_311) -m pip install -e '.[dev]'

install-ft:
	$(PYTHON_314T) -m venv $(VENV_FT)
	$(PY_FT) -m pip install --upgrade pip
	$(PY_FT) -m pip install -e '.[dev]'

install-pi:
	@test -x $(PY_311) || { echo "error: $(PY_311) not found — run 'make install' first" >&2; exit 1; }
	$(PY_311) -m libharness._pi_vendor install

install-pi-node-deprecated:
	scripts/setup-node.sh
	scripts/setup-pi.sh

bootstrap: install install-pi
	@echo
	@echo "Sandbox + venvs ready. Next:"
	@echo "  make login       (one-time OAuth)"
	@echo "  make test"

login:
	scripts/login.sh

pi:
	scripts/pi.sh $(ARGS)

smoke-pi:
	scripts/smoke-pi.sh

# --- tests -----------------------------------------------------------------
# scripts/test.sh reads VENV from scripts/lib/env.sh, which honors the
# LIBHARNESS_VENV env override.

test: test-311 test-ft

test-311:
	scripts/test.sh -m "not live"

test-ft:
	LIBHARNESS_VENV=$(abspath $(VENV_FT)) scripts/test.sh -m "not live"

test-live: test-live-311 test-live-ft

test-live-311:
	scripts/test.sh -m live

test-live-ft:
	LIBHARNESS_VENV=$(abspath $(VENV_FT)) scripts/test.sh -m live

# --- lint ------------------------------------------------------------------
# Ruff config is target-version=py311 in pyproject.toml; running the same
# check from both venvs catches dep/typeshed differences, not syntax differences.

lint: lint-311 lint-ft

lint-311:
	$(RUFF_311) check src/ tests/

lint-ft:
	$(RUFF_FT) check src/ tests/

lint-fix:
	$(RUFF_311) check --fix src/ tests/

# --- typecheck -------------------------------------------------------------
# mypy/pyright are configured for python_version=3.11 in pyproject.toml. The
# -ft variants override on the CLI to check the codebase as it would type on
# 3.14, so we catch typing API drift between versions.

typecheck: typecheck-mypy typecheck-pyright

typecheck-mypy: typecheck-mypy-311 typecheck-mypy-ft

typecheck-mypy-311:
	$(MYPY_311) src/

typecheck-mypy-ft:
	$(MYPY_FT) --python-version 3.14 src/

typecheck-pyright: typecheck-pyright-311 typecheck-pyright-ft

typecheck-pyright-311:
	$(PYRIGHT_311) src/

typecheck-pyright-ft:
	$(PYRIGHT_FT) --pythonversion 3.14 src/

# --- format ----------------------------------------------------------------

format:
	$(RUFF_311) format src/ tests/

# --- markdown ---------------------------------------------------------------
#
# Per-file workflow recommended for live edits (see CLAUDE.md):
#   $(PY_311) -m mdformat --wrap keep <FILE>
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
	$(PY_311) -m mdformat --wrap keep $(MD_FILES)

format-md-check:
	$(PY_311) -m mdformat --wrap keep --check $(MD_FILES)

# --- aggregate -------------------------------------------------------------

all: lint typecheck test

all-311: lint-311 typecheck-mypy-311 typecheck-pyright-311 test-311

all-ft: lint-ft typecheck-mypy-ft typecheck-pyright-ft test-ft

# --- cleanup ---------------------------------------------------------------

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +

clean-sandbox:
	scripts/clean.sh --yes

clean-all: clean clean-sandbox
	rm -rf $(VENV) $(VENV_FT)
