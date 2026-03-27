# Cloud Webhook Journey

This stage shows how to use the journey cloud webhook tool without hosting a local endpoint inside the test process.

The journey acquires one cloud-hosted endpoint, posts a demo webhook to that URL, then waits for the next queued
request with the official cloud webhook helpers.

## What this teaches

- how `journey.tools.webhook.get_webhook_endpoint(...)` returns a step value
- how `journey.tools.webhook.wait_for_webhook_request(...)` fits into the same retry model as any other step
- how to point the journey SDK at a hosted cloud webhook service

## Files to read

- `example/cloud_webhook_journey/cloud_webhook_journey.py`

## Configure the hosted journey cloud

Export the API key and base URL for your hosted journey cloud service:

```bash
export JOURNEY_CLOUD_API_KEY=journey-demo-key
export JOURNEY_CLOUD_BASE_URL=https://journey-cloud.example.test
```

What to expect:

- the same API key works for endpoint creation and request polling
- the service itself is hosted separately from the public Journey SDK framework checkout

## Run it

1. Plan the journey:

```bash
uv run journey plan --file example/cloud_webhook_journey/cloud_webhook_journey.py
```

What to expect:

- one discovered journey called `cloud_webhook_journey`
- one planned case
- labels for acquiring the cloud endpoint, sending the demo request, and receiving it

2. Execute the full flow:

```bash
uv run journey execute --file example/cloud_webhook_journey/cloud_webhook_journey.py
```

What to expect:

- `get_webhook_invoice_paid` creates one cloud webhook endpoint
- `send_invoice_paid_webhook_later` posts a JSON webhook to the returned URL
- `receive_webhook_invoice_paid` retries until the queued request is ready
- the journey finishes successfully

## Why this matters

The cloud webhook tool keeps the same step-oriented authoring model as the local webhook host, but it moves endpoint
hosting out of the test process. That makes it easier to test systems that need a stable externally reachable callback
URL.

## Next step

Continue with [`simple_journey/README.md`](../simple_journey/README.md) to compare the cloud webhook flow with the
existing local Playwright and webhook walkthrough.
