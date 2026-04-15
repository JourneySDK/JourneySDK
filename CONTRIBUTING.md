# Contributing

Run all commands in this file from `/Users/piotrsliwa/jny/public`.

## Local Setup

Install the dev environment and run the public test suite:

```bash
uv sync --extra dev
uv run pytest
```

If your change touches shared Journey Cloud behavior, also run the private compatibility suite:

```bash
cd ../private
uv run --with ../public --extra dev pytest
```

## Editable Installs

Install the library in editable mode with `pip`:

```bash
pip install -e .
```

Add the local checkout to a `uv` project in editable mode:

```bash
uv add --editable /path/to/journey-sdk
```

Install the CLI from a local checkout in editable mode:

```bash
uv tool install --editable /path/to/journey-sdk
```

## Local Package Smoke Test

Build the package, install the built wheel, and verify the `journey` CLI:

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
2. Run `uv run pytest`.
3. Run `cd ../private && uv run --with ../public --extra dev pytest`.
4. Run `./scripts/smoke_test_package.sh`.
5. Build the release artifacts with `uv build`.
6. Publish them with `uv publish`.
