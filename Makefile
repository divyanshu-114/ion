# Keep installation paths separate from the caller's repository/workspace.
ION_ROOT := $(shell CDPATH= cd "$$(dirname "$(strip $(MAKEFILE_LIST))")" && pwd)
UV ?= $(shell command -v uv 2>/dev/null || printf '%s' '$(ION_ROOT)/.uv-tools/bootstrap/bin/uv')
UV_CACHE_DIR ?= $(ION_ROOT)/.uv-cache
UV_PYTHON_INSTALL_DIR ?= $(ION_ROOT)/.uv-tools/python
export UV_CACHE_DIR
export UV_PYTHON_INSTALL_DIR

.PHONY: setup install run test clean

setup:
	python3 "$(ION_ROOT)/scripts/bootstrap.py"
	"$(UV)" sync --project "$(ION_ROOT)" --locked

install: setup
	"$(UV)" tool install --editable "$(ION_ROOT)" --python "$(ION_ROOT)/.venv/bin/python"

run:
	"$(UV)" run --project "$(ION_ROOT)" --locked ion

test:
	"$(UV)" run --directory "$(ION_ROOT)" --locked python -m pytest -q

clean:
	python3 "$(ION_ROOT)/scripts/clean.py"
