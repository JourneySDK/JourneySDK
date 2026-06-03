#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"

release_tag="${JOURNEY_RELEASE_TAG:-${GITHUB_REF_NAME:-}}"
if [ -n "$release_tag" ]; then
  echo "Checking release tag $release_tag..."
  python3 "$ROOT_DIR/scripts/assert_release_tag.py" --tag "$release_tag"
fi

if [ -z "${UV_PUBLISH_TOKEN:-}" ]; then
  echo "UV_PUBLISH_TOKEN must be set before publishing." >&2
  echo "Create a PyPI API token, then run: export UV_PUBLISH_TOKEN='...'" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv must be installed and available on PATH." >&2
  exit 1
fi

cd "$ROOT_DIR"

echo "Removing old package build artifacts..."
rm -rf "$ROOT_DIR/build" "$DIST_DIR" "$ROOT_DIR/journey_sdk.egg-info"

echo "Running test suite..."
uv run --extra dev pytest

echo "Building and smoke testing package artifacts..."
"$ROOT_DIR/scripts/smoke_test_package.sh"

artifacts=("$DIST_DIR"/*)
if [ ! -e "${artifacts[0]}" ]; then
  echo "No package artifacts were built in $DIST_DIR." >&2
  exit 1
fi

echo "Checking package metadata..."
uv run --with twine twine check "${artifacts[@]}"

echo "Publishing package artifacts..."
uv publish "$@" "${artifacts[@]}"

echo "Package publish completed."
