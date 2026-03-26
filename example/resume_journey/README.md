# Resume Journey

This stage demonstrates `journey execute --state ...` with a self-contained step and a manual interruption.

The example prints tutorial notes to stderr so you can see what has already been saved, when to press `Ctrl-C`, and
what will happen when you rerun the command with the same state file.

## What this teaches

- how `--state` saves execution progress
- how an interrupted step resumes from the step boundary instead of from the middle of the function body
- how later runs reuse earlier successful step results without rerunning them

## Files to read

- `example/resume_journey/resume_journey.py`

## Run it

1. Reset the demo state file:

```bash
uv run python -c "from example.resume_journey import reset_demo_state; reset_demo_state(state_path='/tmp/journey-resume-tutorial.state')"
```

2. Start the journey with a state file:

```bash
uv run journey execute --file example/resume_journey/resume_journey.py --state /tmp/journey-resume-tutorial.state
```

When the run reaches `wait_for_resume_signal`, watch for the tutorial note that starts with
`[tutorial] Press Ctrl-C during the next 2.0 seconds...` and press `Ctrl-C` before that pause ends.

What to expect from the first run:

- `load_support_ticket` succeeds and saves its ticket result
- `wait_for_resume_signal` prints that it restarted from the top on resume with the same saved inputs
- the process exits as interrupted
- `/tmp/journey-resume-tutorial.state` is left behind on disk

3. Run the exact same command again, but let it finish this time:

```bash
uv run journey execute --file example/resume_journey/resume_journey.py --state /tmp/journey-resume-tutorial.state
```

What to expect from the second run:

- the case starts with `resume` instead of `start`
- `wait_for_resume_signal` starts again on attempt 2
- the tutorial notes explain that the step restarted from its boundary with the same saved ticket
- the journey finishes successfully without rerunning `load_support_ticket`

## Why this matters

Long journeys get interrupted in real life. A browser may close, a developer may stop the run, or a machine may
restart. `--state` keeps the progress that already succeeded and reruns the interrupted step with the same saved
inputs.

If you want to replay the interruption later, delete the state file first or run the reset command again.

## Next step

Continue with [`simple_journey/README.md`](../simple_journey/README.md) for the browser and webhook walkthrough.
