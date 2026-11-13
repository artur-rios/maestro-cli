#!/bin/sh
# The `build` check: builds the package's sdist and wheel into dist/ (python -m build, from pyproject.toml), installs
# the wheel, with its dependencies, into a throwaway virtual environment, and runs its `maestro` entry point once
# (--help), so a wheel that does not install or start fails. yggdrasil.yml runs it from the repository's root in the
# pinned Python image, so it runs the same on your own machine:
#   docker run --rm -v "$PWD:/src" -w /src <yggdrasil.yml's build image> ci/build.sh
#
# The test suite (python -m unittest discover -s tests -t .) is not run: on Linux nine of its tests fail on main as it
# is (test_promptlib's %VAR% expansion, which is Windows syntax, and eight of test_shell's StartingRuns). Add it here
# once they pass in this image.
#
# build is pinned to an exact version; the build backend is pyproject.toml's [build-system].
set -eu

# The repository's root, wherever this is run from.
cd "$(dirname "$0")/.."

# renovate: datasource=pypi depName=build
BUILD_VERSION=1.6.1

venv=$(mktemp -d)
trap 'rm -rf "$venv"' EXIT
python3 -m venv "$venv"
"$venv/bin/pip" install --quiet --disable-pip-version-check "build==$BUILD_VERSION"

rm -rf dist
"$venv/bin/python" -m build --outdir dist .
"$venv/bin/pip" install --quiet --disable-pip-version-check dist/*.whl
"$venv/bin/maestro" --help
