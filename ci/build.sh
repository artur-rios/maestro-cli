#!/bin/sh
# The `build` check: builds the package's sdist and wheel into dist/ (python -m build, from pyproject.toml), installs
# the wheel, with its dependencies, into a throwaway virtual environment, runs its `maestro` entry point once
# (--help), so a wheel that does not install or start fails, and then runs the test suite (pytest under coverage,
# with the floor pyproject.toml's [tool.coverage.report] sets) in that same environment. yggdrasil.yml runs it from
# the repository's root in the pinned Python image, so it runs the same on your own machine:
#   docker run --rm -v "$PWD:/src" -w /src <yggdrasil.yml's build image> ci/build.sh
#
# The tests run here rather than in a check of their own: the environment is already built, and a separate check
# would build it again. They need git (the integration tests drive a real repository), which the image has.
#
# build, pytest and coverage are pinned to exact versions; the build backend is pyproject.toml's [build-system].
set -eu

# The repository's root, wherever this is run from.
cd "$(dirname "$0")/.."

# renovate: datasource=pypi depName=build
BUILD_VERSION=1.6.1
# renovate: datasource=pypi depName=pytest
PYTEST_VERSION=9.1.1
# renovate: datasource=pypi depName=coverage
COVERAGE_VERSION=7.13.1

venv=$(mktemp -d)
trap 'rm -rf "$venv"' EXIT
python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --disable-pip-version-check "build==$BUILD_VERSION"

rm -rf dist
"$venv/bin/python" -m build --outdir dist .
"$venv/bin/pip" install --quiet --disable-pip-version-check dist/*.whl
"$venv/bin/maestro" --help

"$venv/bin/pip" install --quiet --disable-pip-version-check "pytest==$PYTEST_VERSION" "coverage==$COVERAGE_VERSION"
# The data file lives in the throwaway environment, so the check leaves nothing in the checkout.
export COVERAGE_FILE="$venv/.coverage"
"$venv/bin/python" -m coverage run -m pytest -q -p no:cacheprovider
"$venv/bin/python" -m coverage report
