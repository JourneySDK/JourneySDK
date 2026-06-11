# HTTP Touchpoint Reference

Use the HTTP touchpoint for small deterministic HTTP calls from inside journey steps, especially app-specific fixtures
or health checks that should not clutter the journey spec.

## Public API

- `HttpResponse`: serializable response with `status`, `headers`, `body`, `text()`, and `json()`.
- `http_request(url, method="GET", headers=None, body=None, json_body=None, timeout=10.0) -> HttpResponse`
- `get_json(url, headers=None, timeout=10.0)`
- `post_json(url, payload, headers=None, timeout=10.0)`
- `wait_for_http(url, expected_status=200, timeout=60.0, poll_interval=1.0, method="GET", headers=None) -> HttpResponse`

## Authoring Pattern

Call HTTP helpers from step functions or app-specific helpers used by step functions. They intentionally fail outside
step execution so agents do not perform live network work during import or planning.

```python
from journeysdk.touchpoints.http import post_json, wait_for_http


def prepare_demo_content(app):
    post_json(f"{app.fixture_url}/content", {"state": "baseline"})
    wait_for_http(f"{app.base_url}/healthz", timeout=30)
    return app
```

Prefer these helpers over raw `urllib`, `requests`, `time.sleep`, or custom polling loops in journey files.

HTTP touchpoint lifecycle events appear in the structured Journey log under `.journey/logs/`. Use `journey evidence --show`
with a case or step filter when an agent needs to correlate HTTP polling with browser, Docker, or other touchpoint
evidence.
