#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}

trap cleanup EXIT

cd "$ROOT_DIR"

rm -rf "$ROOT_DIR/build"

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
"$VENV_DIR/bin/journey" agent codex >/dev/null
INSTALL_PROJECT="$TMP_DIR/install-project"
mkdir -p "$INSTALL_PROJECT"
(
  cd "$INSTALL_PROJECT"
  "$VENV_DIR/bin/journey" agent codex --install >/dev/null
)
test -f "$INSTALL_PROJECT/AGENTS.md"
"$VENV_DIR/bin/journey" touchpoints docker >/dev/null
"$VENV_DIR/bin/journey" touchpoints all >/dev/null

echo "Smoke testing persistent uv CLI install from the wheel..."
CLI_HOME="$TMP_DIR/cli-home"
mkdir -p "$CLI_HOME"
HOME="$CLI_HOME" uv tool install "$WHEEL_PATH"
JOURNEY_BIN="$(find "$CLI_HOME" -type f -path '*/bin/journey' -print -quit)"

if [ -z "$JOURNEY_BIN" ]; then
  echo "Installed journey binary was not found." >&2
  exit 1
fi

PATH="$(dirname "$JOURNEY_BIN"):$PATH" journey --help >/dev/null
PATH="$(dirname "$JOURNEY_BIN"):$PATH" journey agent codex >/dev/null
PATH="$(dirname "$JOURNEY_BIN"):$PATH" journey touchpoints docker >/dev/null
PATH="$(dirname "$JOURNEY_BIN"):$PATH" journey touchpoints all >/dev/null

echo "Smoke testing one-off uv CLI run from the wheel..."
HOME="$CLI_HOME" uv tool run --from "$WHEEL_PATH" journey --help >/dev/null
HOME="$CLI_HOME" uv tool run --from "$WHEEL_PATH" journey agent codex >/dev/null
HOME="$CLI_HOME" uv tool run --from "$WHEEL_PATH" journey touchpoints docker >/dev/null
HOME="$CLI_HOME" uv tool run --from "$WHEEL_PATH" journey touchpoints all >/dev/null

echo "Package smoke tests passed."
