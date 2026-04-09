"""Tutorial journeys showing retry modes."""

from __future__ import annotations

from journey import checkpoint, journey, step

EVENTS: list[str] = []
_ATTEMPTS = {
    "same_step": 0,
    "report_issue": 0,
    "checkpoint_refresh": 0,
    "checkpoint_wait": 0,
}


def reset_demo_state() -> None:
    EVENTS.clear()
    for key in _ATTEMPTS:
        _ATTEMPTS[key] = 0


def prepare_same_step_demo() -> dict[str, str]:
    demo = {"task": "same_step_retry"}
    EVENTS.append("prepare_same_step_demo")
    return demo


def wait_for_same_step() -> bool:
    _ATTEMPTS["same_step"] += 1
    EVENTS.append(f"wait_for_same_step:{_ATTEMPTS['same_step']}")
    if _ATTEMPTS["same_step"] < 2:
        raise RuntimeError("still waiting for the same-step retry demo")
    return True


def issue_report_request() -> dict[str, str]:
    _ATTEMPTS["report_issue"] += 1
    request = {"request_id": f"report-{_ATTEMPTS['report_issue']}"}
    EVENTS.append(f"issue_report_request:{request['request_id']}")
    return request


def wait_for_report(request: dict[str, str]) -> dict[str, str]:
    request_id = request["request_id"]
    EVENTS.append(f"wait_for_report:{request_id}")
    if request_id == "report-1":
        raise RuntimeError("report not ready yet")
    return {
        "request_id": request_id,
        "status": "ready",
    }


def assert_report_ready(report: dict[str, str]) -> bool:
    EVENTS.append(f"assert_report_ready:{report['request_id']}")
    if report.get("status") != "ready":
        raise AssertionError(f"Unexpected report status: {report.get('status')!r}")
    return True


def load_status_request() -> dict[str, str]:
    request = {"request_id": "status-001"}
    EVENTS.append(f"load_status_request:{request['request_id']}")
    return request


def refresh_status_cache() -> dict[str, int]:
    _ATTEMPTS["checkpoint_refresh"] += 1
    cache = {"refresh_number": _ATTEMPTS["checkpoint_refresh"]}
    EVENTS.append(f"refresh_status_cache:{cache['refresh_number']}")
    return cache


def wait_for_checkpoint_retry(
    request: dict[str, str],
    cache: dict[str, int],
) -> dict[str, str]:
    _ATTEMPTS["checkpoint_wait"] += 1
    EVENTS.append(
        "wait_for_checkpoint_retry:"
        f"{request['request_id']}:refresh_{cache['refresh_number']}:attempt_{_ATTEMPTS['checkpoint_wait']}"
    )
    if _ATTEMPTS["checkpoint_wait"] < 2:
        raise RuntimeError("checkpoint retry demo is still waiting")
    return {
        "request_id": request["request_id"],
        "status": "ready",
        "refresh_number": str(cache["refresh_number"]),
    }


def assert_checkpoint_retry_ready(result: dict[str, str]) -> bool:
    EVENTS.append(f"assert_checkpoint_retry_ready:{result['request_id']}")
    if result.get("status") != "ready":
        raise AssertionError(f"Unexpected checkpoint retry status: {result.get('status')!r}")
    if result.get("refresh_number") != "2":
        raise AssertionError(
            f"Expected the second cache refresh to win, got {result.get('refresh_number')!r}."
        )
    return True


@journey
def retry_current_step_journey() -> None:
    step(prepare_same_step_demo)
    step(wait_for_same_step, retry=1, retry_delay=0)


@journey
def retry_from_step_result_journey() -> None:
    request = step(issue_report_request)
    report = step(
        wait_for_report,
        request,
        retry=1,
        retry_delay=0,
        retry_from=request,
    )
    step(assert_report_ready, report)


@journey
def retry_from_checkpoint_journey() -> None:
    request = step(load_status_request)
    retry_anchor = checkpoint()
    cache = step(refresh_status_cache)
    result = step(
        wait_for_checkpoint_retry,
        request,
        cache,
        retry=1,
        retry_delay=0,
        retry_from=retry_anchor,
    )
    step(assert_checkpoint_retry_ready, result)
