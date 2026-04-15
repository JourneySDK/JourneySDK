# Installation And CLI

Run every command in this guide from the repository root.

## Install The Python Package

Install Journey SDK into an existing Python environment:

```bash
pip install journey-sdk
```

Add Journey SDK to a `uv` project:

```bash
uv add journey-sdk
```

Import the Journey SDK API from `journeysdk`:

```python
from journeysdk import journey, step
```

## Install The CLI

Run the CLI once without installing it:

```bash
uvx --from journey-sdk journey --help
```

Install a persistent `journey` command with `uv`:

```bash
uv tool install journey-sdk
journey --help
```

If your shell cannot find the command yet, refresh the shell PATH integration:

```bash
uv tool update-shell
```

Install the CLI inside a virtual environment with `pip`:

```bash
python -m pip install journey-sdk
journey --help
```

Use the CLI from a project-local environment:

```bash
uv add journey-sdk
uv run journey --help
```

## Optional Browser Support

Install the persistent CLI with Playwright available:

```bash
uv tool install journey-sdk --with playwright
```

Run the CLI once with Playwright available:

```bash
uvx --from journey-sdk --with playwright journey execute --help
```

Install the browser binaries when you need them:

```bash
uv run --with playwright python -m playwright install chromium
```

## Local Development Installs

Install the current checkout with `pip`:

```bash
pip install -e .
```

Add the current checkout to a `uv` project:

```bash
uv add --editable /path/to/journey-sdk
```

Install the CLI directly from a local checkout:

```bash
uv tool install --editable /path/to/journey-sdk
journey --help
```

For the full contributor workflow, including package smoke tests and manual publishing, see
[`../CONTRIBUTING.md`](../CONTRIBUTING.md).
