#!/bin/sh
# The `lint` check: ruff over the Python sources, with the rules [tool.ruff.lint] in pyproject.toml selects (pyflakes
# and pycodestyle's errors). yggdrasil.yml runs it from the repository's root in the pinned Python image, so it runs
# the same on your own machine:
#   docker run --rm -v "$PWD:/src" -w /src <yggdrasil.yml's lint image> ci/lint.sh
# or, with Python 3 installed, `sh ci/lint.sh` (it needs nothing but python3 -m venv and PyPI).
#
# ruff is pinned to an exact version and installed into a throwaway virtual environment outside the checkout, and run
# without its cache, so the check leaves nothing behind.
set -eu

# The repository's root, wherever this is run from.
cd "$(dirname "$0")/.."

# renovate: datasource=pypi depName=ruff
RUFF_VERSION=0.17.4

venv=$(mktemp -d)
trap 'rm -rf "$venv"' EXIT
python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --disable-pip-version-check "ruff==$RUFF_VERSION"

"$venv/bin/ruff" --version
"$venv/bin/ruff" check --no-cache
