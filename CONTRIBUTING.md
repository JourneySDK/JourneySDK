# Contributing

Run contributor commands from the Journey SDK repository root: the directory that contains this
`CONTRIBUTING.md` and `pyproject.toml`.

## Local Setup

Install the dev environment and run the test suite:

```bash
uv sync --extra dev
uv run pytest
```

## Editable Installs

Install this checkout in editable mode in the current project environment:

```bash
uv pip install -e .
uv run journey --help
```

Add the local checkout to another `uv` project in editable mode.
Run this command from that other project's root, not from the Journey SDK repository:

```bash
uv add --editable /path/to/journey-sdk
```

Install the CLI from a local checkout in editable mode:

```bash
uv tool install --editable /path/to/journey-sdk
journey --help
```

If your shell cannot find `journey` yet, run `uv tool update-shell` or open a new shell session.

## Local Package Smoke Test

From the Journey SDK repository root, build the package, install the built wheel, and verify the
`journey` CLI:

```bash
./scripts/smoke_test_package.sh
```

That script:

- runs `uv build`
- installs the built wheel into a clean virtual environment and verifies `import journeysdk`
- installs the built wheel as a temporary `uv` tool and verifies `journey --help`
- runs a one-off `uv tool run --from <wheel> journey --help`

## Manual Release Flow

1. Update the package version in `pyproject.toml`.
2. From the Journey SDK repository root, run `uv run pytest`.
3. From the Journey SDK repository root, run `./scripts/smoke_test_package.sh`.
4. Build the release artifacts with `uv build`.
5. Publish them with `uv publish`.
