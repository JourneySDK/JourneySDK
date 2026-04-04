# Cloud Email Journey

This stage shows how to use the official Journey email tool with a hosted Journey Cloud inbox.

The journey resolves the default inbox for the current API key, sends one welcome email to that inbox, then waits for
the matching message through Journey Cloud.

## What this teaches

- how `journey.tools.email.get_email_inbox(...)` returns a serializable inbox handle
- how `journey.tools.email.send_email(...)` defaults to the hosted inbox address
- how `journey.tools.email.wait_for_email(...)` fits into the same retry model as any other step

## Files to read

- `example/cloud_email_journey/cloud_email_journey.py`

## Configure the hosted journey cloud

Export the API key and base URL for your hosted journey cloud service:

```bash
export JOURNEY_CLOUD_API_KEY=journey-demo-key
export JOURNEY_CLOUD_BASE_URL=https://journey-cloud.example.test
```

What to expect:

- the same API key works for inbox lookup, email sending, and email polling
- Journey Cloud keeps the actual SMTP and IMAP server credentials private on the service side

## Run it

1. Plan the journey:

```bash
uv run journey plan --file example/cloud_email_journey/cloud_email_journey.py
```

What to expect:

- one discovered journey called `cloud_email_journey`
- one planned case
- labels for inbox lookup, email sending, and email receiving

2. Execute the full flow:

```bash
uv run journey execute --file example/cloud_email_journey/cloud_email_journey.py
```

What to expect:

- `get_email_inbox` resolves the hosted inbox for the current API key
- `send_email` sends one welcome email to that inbox
- `receive_email` retries until the matching message is available
- the journey finishes successfully

## Why this matters

The email tool keeps the same step-oriented authoring model as the webhook helpers, but it lets Journey Cloud own the
mail server credentials and the default inbox configuration for each API key.

## Next step

Continue with [`simple_journey/README.md`](../simple_journey/README.md) to compare the cloud-only email flow with the
browser-and-webhook example.

