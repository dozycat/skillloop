.PHONY: install lint format test build demo clean check

PY ?= .venv/bin/python

install:  ## create .venv and install the package with dev tools
	python3 -m venv .venv
	$(PY) -m pip install -e '.[dev]'

lint:  ## static checks (what CI runs)
	$(PY) -m ruff check src tests examples
	$(PY) -m ruff format --check src tests examples

format:  ## auto-fix lint and formatting
	$(PY) -m ruff check --fix src tests examples
	$(PY) -m ruff format src tests examples

test:  ## run the offline test suite
	$(PY) -m pytest

check: lint test  ## everything CI checks

build:  ## build sdist and wheel into dist/
	rm -rf dist
	$(PY) -m build

demo:  ## run the offline demo end to end in ./demo
	$(PY) -m skillloop init demo --demo --force
	$(PY) -m skillloop evolve -p demo

clean:
	rm -rf dist build .pytest_cache .ruff_cache demo
	find . -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
