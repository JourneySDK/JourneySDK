# Selection and JSON Output

This stage teaches two common CLI tasks:

- narrowing discovery with `--journey`
- asking the CLI for JSON with `--json`

The file contains two good journeys on purpose so you can see how selection changes the output.

## What this teaches

- how discovery behaves when one file defines multiple journeys
- how `--journey` narrows planning and execution to one decorated function
- how `--json` changes the output shape for scripting and tooling

## Files to read

- `example/selection_journeys/selection_journeys.py`

## Run it

1. Plan everything in the file:

```bash
uv run journey plan --file example/selection_journeys/selection_journeys.py
```

What to expect:

- two discovered journeys: `invoice_reminder_journey` and `welcome_email_journey`
- each journey plans to one case

2. Plan one journey and ask for JSON:

```bash
uv run journey plan --file example/selection_journeys/selection_journeys.py --journey welcome_email_journey --json
```

What to expect:

- valid JSON instead of the live text format
- one item in `journeys`
- `errors` is an empty list

3. Execute one journey and keep the output in JSON:

```bash
uv run journey execute --file example/selection_journeys/selection_journeys.py --journey invoice_reminder_journey --json
```

What to expect:

- one journey result in `journeys`
- `journey_name` set to `invoice_reminder_journey`
- a completed case report with the labels `load_invoice_reminder` and `assert_invoice_reminder`

## Why this matters

`--journey` is the safest way to focus on one journey when a file or directory contains several of them. `--json` is
useful when you want another tool, script, or CI job to read the results without parsing human-readable text.

## Next step

Continue with [`branching_journey/README.md`](../branching_journey/README.md) to see how one authored journey becomes
multiple executable cases.
