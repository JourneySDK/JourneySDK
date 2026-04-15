#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}

trap cleanup EXIT

cd "$ROOT_DIR"

echo "Building wheel and sdist..."
uv build

WHEEL_PATH="$(find "$ROOT_DIR/dist" -maxdepth 1 -type f -name 'journey_sdk-*.whl' -print -quit)"

if [ -z "$WHEEL_PATH" ]; then
  echo "Built wheel not found in dist/." >&2
  exit 1
fi

echo "Smoke testing wheel in a virtualenv..."
VENV_DIR="$TMP_DIR/venv"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install "$WHEEL_PATH"
"$VENV_DIR/bin/python" -c "import journeysdk"
"$VENV_DIR/bin/journey" --help >/dev/null

echo "Smoke testing uv tool install from the wheel..."
TOOL_HOME="$TMP_DIR/tool-home"
mkdir -p "$TOOL_HOME"
HOME="$TOOL_HOME" uv tool install "$WHEEL_PATH"
TOOL_BIN="$(find "$TOOL_HOME" -type f -path '*/bin/journey' -print -quit)"

if [ -z "$TOOL_BIN" ]; then
  echo "Installed uv tool binary was not found." >&2
  exit 1
fi

PATH="$(dirname "$TOOL_BIN"):$PATH" journey --help >/dev/null

echo "Smoke testing one-off uv tool run from the wheel..."
HOME="$TOOL_HOME" uv tool run --from "$WHEEL_PATH" journey --help >/dev/null

echo "Package smoke tests passed."
