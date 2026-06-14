from __future__ import annotations

import pytest

import journeysdk.discover as discover_module
from journeysdk.discover import (
    ActionCapture,
    BrowserStartState,
    CandidateAction,
    DiscoverOptions,
    DiscoveredEdge,
    DiscoveredNode,
    PageSnapshot,
    ProbeSpec,
    _dedupe_candidates,
    _click_candidate,
    _deterministic_candidates_for_page,
    _discover_json_state_url,
    _dismiss_cookie_consent,
    _emit_discover_omission_summary,
    _first_stable_identifier,
    _form_submit_candidate,
    _form_submit_candidates,
    _json_contains_value,
    _normalize_options,
    _omission_reason_for_exception,
    browser_state_from_step_anchor,
    evidence_context_from_step_anchor,
    parse_candidate_actions,
    render_extension_source,
    render_journey_source,
    validate_generated_extension_source,
    validate_generated_source,
)
from journeysdk.logger import configure_logging
from journeysdk.touchpoints.webhook import CloudWebhookEndpoint


def _snapshot(url: str, title: str, text: str = "") -> PageSnapshot:
    return PageSnapshot(
        url=url,
        title=title,
        visible_text=text,
        semantic_dom="",
        signature=f"{url}|{title}|{text}",
    )


def test_parse_candidate_actions_accepts_unique_email_helper_and_code_fences() -> None:
    actions = parse_candidate_actions(
        """
        ```json
        {
          "actions": [
            {
              "name": "Create organizer workspace",
              "description": "Fill the organizer signup form.",
              "code": "page.locator(\\"[data-testid='organizer-email']\\").fill(unique_email(\\"organizer\\"), timeout=timeout_ms)"
            }
          ]
        }
        ```
        """
    )

    assert actions == [
        CandidateAction(
            name="Create organizer workspace",
            description="Fill the organizer signup form.",
            code=(
                "page.locator(\"[data-testid='organizer-email']\")"
                '.fill(unique_email("organizer"), timeout=timeout_ms)'
            ),
        )
    ]


def test_parse_candidate_actions_rejects_unsafe_python() -> None:
    with pytest.raises(RuntimeError, match="imports"):
        parse_candidate_actions(
            """
            {
              "actions": [
                {
                  "name": "steal files",
                  "description": "bad",
                  "code": "import os\\npage.goto('http://example.test', timeout=timeout_ms)"
                }
              ]
            }
            """
        )


def test_parse_candidate_actions_tolerant_mode_omits_invalid_model_snippets() -> None:
    actions = parse_candidate_actions(
        """
        {
          "actions": [
            {
              "name": "bad wait",
              "description": "bad",
              "code": "page.get_by_text('Ready').is_visible()"
            },
            {
              "name": "good click",
              "description": "good",
              "code": "page.get_by_text('Ready').click(timeout=timeout_ms)"
            }
          ]
        }
        """,
        strict=False,
    )

    assert actions == [
        CandidateAction(
            name="good click",
            description="good",
            code="page.get_by_text('Ready').click(timeout=timeout_ms)",
        )
    ]


def test_parse_candidate_actions_tolerant_mode_omits_hard_coded_identifiers() -> None:
    actions = parse_candidate_actions(
        """
        {
          "actions": [
            {
              "name": "brittle row",
              "description": "bad",
              "code": "assert 'MC-ABC123' in page.content()"
            },
            {
              "name": "generic row",
              "description": "good",
              "code": "page.get_by_text('Back Office').click(timeout=timeout_ms)"
            }
          ]
        }
        """,
        strict=False,
        forbidden_literals=("MC-ABC123",),
    )

    assert actions == [
        CandidateAction(
            name="generic row",
            description="good",
            code="page.get_by_text('Back Office').click(timeout=timeout_ms)",
        )
    ]


def test_rendered_journey_source_compiles_with_branches() -> None:
    root = DiscoveredNode(
        node_id="root",
        start_url="http://example.test/",
        snapshot=_snapshot("http://example.test/", "Home", "Create Admin"),
        depth=0,
    )
    checkout = DiscoveredEdge(
        edge_id="checkout",
        parent=root,
        action=CandidateAction(
            name="Complete checkout",
            description="Complete attendee checkout.",
            code='page.get_by_text("Checkout").click(timeout=timeout_ms)',
        ),
        snapshot=_snapshot("http://example.test/checkout", "Checkout"),
    )
    admin = DiscoveredEdge(
        edge_id="admin",
        parent=root,
        action=CandidateAction(
            name="Open back office",
            description="Inspect back-office registrations.",
            code='page.get_by_text("Admin").click(timeout=timeout_ms)',
        ),
        snapshot=_snapshot("http://example.test/admin", "Back Office"),
    )
    root.edges.extend([checkout, admin])

    source = render_journey_source((root,), journey_name="discovered_demo")

    assert "if branch(replay_from=open_home_page):" in source
    assert "elif branch(replay_from=open_home_page):" in source
    assert "def complete_checkout" in source
    assert "def open_back_office" in source
    assert "Generated by `journey discover`" in source
    validate_generated_source(source, journey_name="discovered_demo")


def test_generated_replay_helpers_settle_consent_and_poll_assertions() -> None:
    root = DiscoveredNode(
        node_id="root",
        start_url="http://example.test/",
        snapshot=_snapshot("http://example.test/", "Home", "Ready page text"),
        depth=0,
    )

    source = render_journey_source((root,), journey_name="discovered_demo")

    assert "def _dismiss_cookie_consent" in source
    assert "def _settle_replay_page" in source
    assert "    _settle_replay_page(page, timeout_ms=timeout_ms)" in source
    assert "while True:" in source
    validate_generated_source(source, journey_name="discovered_demo")

    namespace: dict[str, object] = {}
    exec(source, namespace, namespace)

    class FakeBody:
        def __init__(self) -> None:
            self.calls = 0

        def inner_text(self, *, timeout: int) -> str:
            assert timeout > 0
            self.calls += 1
            return "Ready page text" if self.calls >= 2 else ""

    class FakePage:
        url = "http://example.test/"

        def __init__(self) -> None:
            self.body = FakeBody()

        def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            assert state in {"load", "networkidle"}

        def evaluate(self, script: str) -> bool:
            assert "cookie" in script.lower()
            return False

        def wait_for_timeout(self, timeout: int) -> None:
            assert timeout == 250

        def title(self) -> str:
            return "Home"

        def locator(self, selector: str) -> FakeBody:
            assert selector == "body"
            return self.body

    namespace["_assert_page_state"](
        FakePage(),
        expected_path="/",
        expected_title="Home",
        expected_text="Ready page text",
        timeout_ms=3000,
    )


def test_generated_assertion_text_skips_weak_anchors() -> None:
    root = DiscoveredNode(
        node_id="root",
        start_url="http://example.test/",
        snapshot=_snapshot(
            "http://example.test/",
            "Home",
            "TEST\nLoading\nCookie consent\nStart a new chat",
        ),
        depth=0,
    )

    source = render_journey_source((root,), journey_name="discovered_demo")

    assert "expected_text='Start a new chat'" in source
    assert "expected_text='TEST'" not in source
    assert "expected_text='Loading'" not in source
    assert "expected_text='Cookie consent'" not in source


def test_rendered_extension_source_compiles_from_anchor_page() -> None:
    root = DiscoveredNode(
        node_id="root",
        start_url="http://example.test/main",
        snapshot=_snapshot("http://example.test/main", "Main", "Main page"),
        depth=0,
    )
    feature = DiscoveredEdge(
        edge_id="feature",
        parent=root,
        action=CandidateAction(
            name="Open new feature",
            description="Open the newly linked feature page.",
            code='page.get_by_text("New feature").click(timeout=timeout_ms)',
        ),
        snapshot=_snapshot("http://example.test/feature", "Feature", "Feature ready"),
    )
    root.edges.append(feature)

    source = render_extension_source(
        (root,),
        anchor_step="open_main_page",
        extension_name="discover_after_open_main_page",
    )

    assert "def discover_after_open_main_page(anchor_result: object) -> None:" in source
    assert "def _recover_discover_after_open_main_page_page(anchor_result: object) -> JourneyBrowserPage:" in source
    assert "return browser_page_from_step_result(anchor_result)" in source
    assert "# open_main_page_page = step(open_main_page)" in source
    assert "anchor_page = step(_recover_discover_after_open_main_page_page, anchor_result)" in source
    assert "open_new_feature_page = step(open_new_feature, anchor_page, anchor_result)" in source
    assert "@journey" not in source
    validate_generated_extension_source(
        source,
        extension_name="discover_after_open_main_page",
    )


def test_rendered_extension_branches_from_original_anchor_result() -> None:
    root = DiscoveredNode(
        node_id="root",
        start_url="http://example.test/checkout",
        snapshot=_snapshot("http://example.test/checkout", "Checkout", "Checkout"),
        depth=0,
    )
    for name, path in (("standard", "/standard"), ("workshop", "/workshop")):
        root.edges.append(
            DiscoveredEdge(
                edge_id=name,
                parent=root,
                action=CandidateAction(
                    name=f"submit_{name}",
                    description=f"Submit {name}.",
                    code="page.get_by_role('button').click(timeout=timeout_ms)",
                ),
                snapshot=_snapshot(f"http://example.test{path}", name.title(), name),
            )
        )

    source = render_extension_source(
        (root,),
        anchor_step="prepare_configured_workspace",
        extension_name="discover_after_prepare_configured_workspace",
    )

    assert "if branch(replay_from=anchor_result):" in source
    assert "elif branch(replay_from=anchor_result):" in source
    assert "branch(replay_from=anchor_page)" not in source
    validate_generated_extension_source(
        source,
        extension_name="discover_after_prepare_configured_workspace",
    )


def test_browser_state_from_step_anchor_uses_last_browser_side_output() -> None:
    from journeysdk.touchpoints.browser import JourneyBrowserPage, _PageSnapshot

    first_page = JourneyBrowserPage(
        snapshot=_PageSnapshot.from_payload(
            {"url": "http://example.test/start", "cookies": [], "local_storage": {}}
        )
    )
    second_page = JourneyBrowserPage(
        snapshot=_PageSnapshot.from_payload(
            {
                "url": "http://example.test/checkout",
                "cookies": [{"name": "session", "value": "abc", "url": "http://example.test"}],
                "local_storage": {"feature": "enabled"},
            }
        )
    )

    state = browser_state_from_step_anchor(
        object(),
        side_outputs={"browser_page": (first_page, second_page)},
        step_label="prepare_configured_workspace",
    )

    assert state.url == "http://example.test/checkout"
    assert state.cookies[0]["name"] == "session"
    assert state.local_storage == (("feature", "enabled"),)


def test_browser_state_from_step_anchor_errors_without_page() -> None:
    with pytest.raises(TypeError, match="return JourneyBrowserPage or open one"):
        browser_state_from_step_anchor(
            {"not": "a page"},
            side_outputs={"other": ("value",)},
            step_label="prepare_configured_workspace",
        )


def test_evidence_context_from_step_anchor_uses_generic_sdk_touchpoints() -> None:
    class FakeStack:
        def service_url(self, service_name: str, port: int) -> str:
            if service_name == "mailpit" and port == 8025:
                return "http://127.0.0.1:18025"
            if service_name == "webhook" and port == 9000:
                return "http://127.0.0.1:19000"
            raise RuntimeError("unknown service")

    endpoint = CloudWebhookEndpoint(
        endpoint_id="endpoint-1",
        path="/webhook",
        url="http://example.test/webhook",
        api_base_url="http://127.0.0.1:8780",
    )

    context = evidence_context_from_step_anchor(
        {"stack": FakeStack(), "endpoint": endpoint}
    )

    assert context.email_evidence_urls == ("http://127.0.0.1:18025",)
    assert context.webhook_evidence_urls == ("http://127.0.0.1:19000",)
    assert context.cloud_webhook_endpoints == (endpoint,)


def test_rendered_extension_source_threads_anchor_into_cloud_webhook_probe() -> None:
    root = DiscoveredNode(
        node_id="root",
        start_url="http://example.test/checkout",
        snapshot=_snapshot("http://example.test/checkout", "Checkout", "Checkout"),
        depth=0,
    )
    root.edges.append(
        DiscoveredEdge(
            edge_id="submit",
            parent=root,
            action=CandidateAction(
                name="submit_registration",
                description="Submit registration.",
                code="page.get_by_role('button').click(timeout=timeout_ms)",
            ),
            snapshot=_snapshot(
                "http://example.test/confirmation",
                "Confirmed",
                "Registration confirmed MC-ABC123",
            ),
            probes=(
                ProbeSpec(
                    kind="cloud_webhook_evidence",
                    url="",
                    description="Journey Cloud webhook evidence contains the visible identifier",
                ),
            ),
        )
    )

    source = render_extension_source(
        (root,),
        anchor_step="prepare_configured_workspace",
        extension_name="discover_after_prepare_configured_workspace",
    )

    assert "step(submit_registration, anchor_page, anchor_result)" in source
    assert "_wait_for_cloud_webhook_evidence(" in source
    validate_generated_extension_source(
        source,
        extension_name="discover_after_prepare_configured_workspace",
    )


def test_optional_evidence_textarea_is_filled_and_captured() -> None:
    candidate = _form_submit_candidate(
        {
            "action": "http://example.test/register",
            "fields": [
                {
                    "tag": "textarea",
                    "type": "textarea",
                    "selector": "[data-testid=\"accessibility-needs\"]",
                    "testid": "accessibility-needs",
                    "name": "accessibility_needs",
                    "label": "Accessibility needs",
                    "placeholder": "Accessibility needs or dietary notes",
                    "value": "",
                    "required": False,
                },
            ],
            "submit": {
                "selector": "[data-testid=\"submit\"]",
                "testid": "submit",
                "text": "Register",
            },
        }
    )

    assert candidate is not None
    assert "Wheelchair access near the front row" in candidate.code
    assert candidate.captures == (
        ActionCapture(
            variable="accessibility_needs",
            value="Wheelchair access near the front row",
            kind="evidence_field",
            label="Accessibility needs",
        ),
    )


def test_visible_assertion_from_prior_static_capture_is_rendered() -> None:
    root = DiscoveredNode(
        node_id="root",
        start_url="http://example.test/checkout",
        snapshot=_snapshot("http://example.test/checkout", "Checkout", "Checkout"),
        depth=0,
    )
    checkout = DiscoveredEdge(
        edge_id="checkout",
        parent=root,
        action=CandidateAction(
            name="register",
            description="Register attendee.",
            code="accessibility_needs = 'Wheelchair access near the front row'",
            captures=(
                ActionCapture(
                    variable="accessibility_needs",
                    value="Wheelchair access near the front row",
                    kind="evidence_field",
                    label="Accessibility needs",
                ),
            ),
        ),
        snapshot=_snapshot("http://example.test/confirmation", "Confirmation", "Registration confirmed"),
    )
    admin = DiscoveredEdge(
        edge_id="admin",
        parent=root,
        action=CandidateAction(
            name="open_admin",
            description="Open admin.",
            code="page.get_by_text('Admin').click(timeout=timeout_ms)",
        ),
        snapshot=_snapshot(
            "http://example.test/admin",
            "Admin",
            "Registrations Wheelchair access near the front row",
        ),
        visible_assertions=("Wheelchair access near the front row",),
    )
    root.edges.append(checkout)
    checkout.child = DiscoveredNode(
        node_id="confirmation",
        start_url=root.start_url,
        snapshot=checkout.snapshot,
        depth=1,
        path=(checkout,),
        edges=[admin],
    )

    source = render_journey_source((root,), journey_name="discovered_demo")

    assert "_assert_page_state(page, expected_text='Wheelchair access near the front row'" in source
    validate_generated_source(source, journey_name="discovered_demo")


def test_required_signup_form_becomes_one_submit_transition() -> None:
    candidate = _form_submit_candidate(
        {
            "action": "http://127.0.0.1:18081/signup",
            "fields": [
                {
                    "tag": "input",
                    "type": "text",
                    "selector": "[data-testid=\"organizer-name\"]",
                    "testid": "organizer-name",
                    "name": "full_name",
                    "label": "Organizer name",
                    "value": "Ada Morgan",
                    "required": True,
                },
                {
                    "tag": "input",
                    "type": "email",
                    "selector": "[data-testid=\"organizer-email\"]",
                    "testid": "organizer-email",
                    "name": "email",
                    "label": "Organizer email",
                    "value": "",
                    "required": True,
                },
                {
                    "tag": "input",
                    "type": "text",
                    "selector": "[data-testid=\"workspace-name\"]",
                    "testid": "workspace-name",
                    "name": "workspace_name",
                    "label": "Workspace name",
                    "value": "QA Conference",
                    "required": True,
                },
            ],
            "submit": {
                "selector": "[data-testid=\"create-workspace\"]",
                "testid": "create-workspace",
                "text": "Create conference workspace",
            },
        }
    )

    assert candidate is not None
    assert candidate.name == "create_workspace"
    assert candidate.code.count(".fill(") == 3
    assert '[data-testid="organizer-name"]' in candidate.code
    assert "unique_email(" in candidate.code
    assert "organizer-email" in candidate.code
    assert '[data-testid="workspace-name"]' in candidate.code
    assert 'page.locator(\'[data-testid="create-workspace"]\').click(timeout=timeout_ms)' in candidate.code


def test_checkout_select_and_submit_are_one_transition() -> None:
    candidate = _form_submit_candidate(
        {
            "action": "http://127.0.0.1:18081/checkout",
            "fields": [
                {
                    "tag": "input",
                    "type": "hidden",
                    "selector": "input[name=\"workspace_id\"]",
                    "name": "workspace_id",
                    "value": "1",
                },
                {
                    "tag": "input",
                    "type": "email",
                    "selector": "[data-testid=\"attendee-email\"]",
                    "testid": "attendee-email",
                    "name": "attendee_email",
                    "label": "Attendee email",
                    "required": True,
                },
                {
                    "tag": "select",
                    "selector": "[data-testid=\"ticket-type\"]",
                    "testid": "ticket-type",
                    "name": "ticket_type",
                    "options": [
                        {"value": "standard", "text": "Standard", "selected": True},
                        {"value": "vip", "text": "VIP", "selected": False},
                    ],
                },
            ],
            "submit": {
                "selector": "[data-testid=\"complete-registration\"]",
                "testid": "complete-registration",
                "text": "Complete registration",
            },
        }
    )

    assert candidate is not None
    assert candidate.name == "complete_registration"
    assert "attendee_email = unique_email(" in candidate.code
    assert "ticket_type = 'standard'" in candidate.code
    assert ".select_option(ticket_type, timeout=timeout_ms)" in candidate.code
    assert "workspace_id" not in candidate.code


def test_select_options_expand_into_bounded_transition_variants() -> None:
    candidates = _form_submit_candidates(
        {
            "action": "http://example.test/checkout",
            "fields": [
                {
                    "tag": "input",
                    "type": "email",
                    "selector": "[data-testid=\"buyer-email\"]",
                    "testid": "buyer-email",
                    "name": "buyer_email",
                    "label": "Buyer email",
                    "required": True,
                },
                {
                    "tag": "select",
                    "selector": "[data-testid=\"plan\"]",
                    "testid": "plan",
                    "name": "plan",
                    "label": "Plan",
                    "options": [
                        {"value": "", "text": "Choose a plan", "selected": False},
                        {"value": "standard", "text": "Standard pass", "selected": True},
                        {"value": "workshop", "text": "Workshop pass", "selected": False},
                        {"value": "team", "text": "Team pass", "selected": False},
                        {"value": "vip", "text": "VIP pass", "selected": False},
                    ],
                },
            ],
            "submit": {
                "selector": "[data-testid=\"complete-registration\"]",
                "testid": "complete-registration",
                "text": "Complete registration",
            },
        },
        max_variants_per_control=3,
    )

    assert [candidate.variant_value for candidate in candidates] == [
        "standard",
        "workshop",
        "team",
    ]
    assert [candidate.variant_label for candidate in candidates] == [
        "Standard pass",
        "Workshop pass",
        "Team pass",
    ]
    workshop_candidate = candidates[1]
    assert workshop_candidate.name == "complete_registration_workshop_pass"
    assert "plan = 'workshop'" in workshop_candidate.code
    assert ".select_option(plan, timeout=timeout_ms)" in workshop_candidate.code
    assert "vip" not in "\n".join(candidate.code for candidate in candidates)


def test_generic_aria_button_uses_unique_visible_text_filter() -> None:
    candidate = _click_candidate(
        {
            "selector": "body > div:nth-of-type(1) > button:nth-of-type(2)",
            "selectorKind": "css_path",
            "tag": "button",
            "aria": "Button",
            "text": "Sign up",
            "textUnique": True,
            "visible": True,
        },
        kind="button",
    )

    assert candidate is not None
    assert "page.locator('button').filter(has_text='Sign up').click(timeout=timeout_ms)" in candidate.code
    assert "aria-label" not in candidate.code


def test_non_unique_selector_requires_unique_visible_text_repair() -> None:
    candidate = _click_candidate(
        {
            "selector": "[data-testid=\"reused-button\"]",
            "selectorKind": "testid",
            "selectorUnique": False,
            "tag": "button",
            "testid": "reused-button",
            "text": "Continue",
            "textUnique": True,
            "visible": True,
        },
        kind="button",
    )

    assert candidate is not None
    assert "page.locator('button').filter(has_text='Continue').click(timeout=timeout_ms)" in candidate.code

    assert (
        _click_candidate(
            {
                "selector": "[data-testid=\"reused-button\"]",
                "selectorKind": "testid",
                "selectorUnique": False,
                "tag": "button",
                "testid": "reused-button",
                "text": "Continue",
                "textUnique": False,
                "visible": True,
            },
            kind="button",
        )
        is None
    )


def test_disabled_and_anonymous_controls_do_not_become_candidates() -> None:
    assert (
        _click_candidate(
            {
                "selector": "[data-testid=\"send-message-button\"]",
                "selectorKind": "testid",
                "testid": "send-message-button",
                "text": "Send",
                "disabled": True,
            },
            kind="button",
        )
        is None
    )
    assert (
        _click_candidate(
            {
                "selector": "[data-testid=\"send-message-button\"]",
                "selectorKind": "testid",
                "testid": "send-message-button",
                "text": "Send",
                "ariaDisabled": "true",
            },
            kind="button",
        )
        is None
    )
    assert (
        _click_candidate(
            {
                "selector": "body > button:nth-of-type(1)",
                "selectorKind": "css_path",
                "aria": "Button",
                "text": "",
                "visible": True,
            },
            kind="button",
        )
        is None
    )


def test_candidate_dedupe_uses_normalized_action_intent() -> None:
    candidates = _dedupe_candidates(
        [
            CandidateAction("Sign up", "first", "page.locator('button').nth(0).click(timeout=timeout_ms)"),
            CandidateAction("sign_up", "second", "page.locator('button').nth(1).click(timeout=timeout_ms)"),
            CandidateAction("Log in", "third", "page.locator('button').nth(2).click(timeout=timeout_ms)"),
        ]
    )

    assert [candidate.name for candidate in candidates] == ["Sign up", "Log in"]


class _FakeConsentLocator:
    def __init__(self, page: "_FakeConsentPage", matches: bool) -> None:
        self.page = page
        self.matches = matches

    def count(self) -> int:
        return 1 if self.matches else 0

    def nth(self, index: int) -> "_FakeConsentLocator":
        assert index == 0
        return self

    def is_visible(self, *, timeout: int) -> bool:
        assert timeout == 250
        return True

    def click(self, *, timeout: int) -> None:
        assert timeout == 1000
        self.page.clicked = True


class _FakeConsentPage:
    def __init__(self, *, button_name: str = "Accept all cookies") -> None:
        self.button_name = button_name
        self.clicked = False

    def evaluate(self, script: str) -> bool:
        assert "cookie" in script.lower()
        return True

    def get_by_role(self, role: str, *, name: object) -> _FakeConsentLocator:
        assert role == "button"
        assert name is not None
        return _FakeConsentLocator(self, bool(name.search(self.button_name)))

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        assert state in {"load", "networkidle"}


def test_cookie_consent_overlay_is_dismissed() -> None:
    page = _FakeConsentPage()

    assert _dismiss_cookie_consent(page) is True
    assert page.clicked is True


def test_cookie_consent_overlay_accepts_cookie_suffix_controls() -> None:
    page = _FakeConsentPage(button_name="Decline all cookies")

    assert _dismiss_cookie_consent(page) is True
    assert page.clicked is True


def test_cookie_consent_is_dismissed_before_candidate_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        dismissed = False

    page = FakePage()

    def fake_dismiss(target: object) -> bool:
        assert target is page
        page.dismissed = True
        return True

    def fake_extract(target: object) -> dict[str, object]:
        assert target is page
        assert page.dismissed is True
        return {
            "forms": [],
            "links": [],
            "buttons": [
                {
                    "selector": "[data-testid=\"sign-up\"]",
                    "selectorKind": "testid",
                    "testid": "sign-up",
                    "text": "Sign up",
                    "visible": True,
                }
            ],
        }

    monkeypatch.setattr(discover_module, "_dismiss_cookie_consent", fake_dismiss)
    monkeypatch.setattr(discover_module, "_extract_affordances", fake_extract)

    candidates = _deterministic_candidates_for_page(page)  # type: ignore[arg-type]

    assert [candidate.name for candidate in candidates] == ["sign_up"]


def test_default_discovery_timeout_is_short_but_generated_replay_stays_conservative() -> None:
    normalized = _normalize_options(DiscoverOptions(urls=("http://example.test",)))
    root = DiscoveredNode(
        node_id="root",
        start_url="http://example.test/",
        snapshot=_snapshot("http://example.test/", "Home", "Home"),
        depth=0,
    )

    source = render_journey_source((root,), journey_name="discovered_demo")

    assert normalized.action_timeout_seconds == 8.0
    assert "timeout_ms = 30000" in source


def test_omission_reason_categorization_and_summary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _omission_reason_for_exception(RuntimeError("strict mode violation")) == "non_unique_selector"
    assert _omission_reason_for_exception(RuntimeError("element is not enabled")) == "disabled_control"
    assert (
        _omission_reason_for_exception(RuntimeError("Cookie dialog intercepts pointer events"))
        == "overlay_blocked"
    )
    assert _omission_reason_for_exception(TimeoutError("Timeout 8000ms exceeded")) == "timeout"

    configure_logging("info")
    _emit_discover_omission_summary({"timeout": 2, "non_unique_selector": 1})
    output = capsys.readouterr().out
    configure_logging("info")

    assert "omitted actions:" in output
    assert "non_unique_selector=1" in output
    assert "timeout=2" in output


def test_probe_helpers_match_identifiers_and_json_state_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _first_stable_identifier("Registration confirmed MC-ABC1234") == "MC-ABC1234"
    assert _first_stable_identifier("Provisioning configures seeded back-office state") is None
    assert _json_contains_value(
        {"registration": {"confirmation_code": "MC-ABC1234", "ticket_type": "workshop"}},
        "workshop",
    )

    fetched_urls: list[str] = []

    def fake_fetch(url: str, *, timeout: float) -> object | None:
        del timeout
        fetched_urls.append(url)
        if url.endswith("/api/state?confirmation_code=MC-ABC1234"):
            return {"confirmation_code": "MC-ABC1234"}
        return None

    monkeypatch.setattr(discover_module, "_fetch_json_if_ok", fake_fetch)

    assert (
        _discover_json_state_url("http://example.test/confirmation", "MC-ABC1234")
        == "http://example.test/api/state?confirmation_code={identifier}"
    )
    assert fetched_urls[0] == "http://example.test/api/state?confirmation_code=MC-ABC1234"


def test_generated_source_includes_discovered_probe_assertions() -> None:
    root = DiscoveredNode(
        node_id="root",
        start_url="http://example.test/",
        snapshot=_snapshot("http://example.test/", "Home", "Start"),
        depth=0,
    )
    confirmation = DiscoveredEdge(
        edge_id="confirmation",
        parent=root,
        action=CandidateAction(
            name="Complete checkout",
            description="Complete checkout and verify discovered evidence.",
            code="ticket_type = 'workshop'\npage.get_by_text('Complete').click(timeout=timeout_ms)",
            captures=(
                ActionCapture(
                    variable="ticket_type",
                    value="workshop",
                    kind="option",
                    label="Workshop pass",
                ),
            ),
        ),
        snapshot=_snapshot(
            "http://example.test/confirmation",
            "Confirmed",
            "Registration confirmed\nConfirmation code: MC-ABC1234",
        ),
        probes=(
            ProbeSpec(
                kind="json_state",
                url="http://example.test/api/state?confirmation_code={identifier}",
                description="same-origin JSON state contains the visible identifier",
            ),
            ProbeSpec(
                kind="email_evidence",
                url="http://127.0.0.1:18025",
                description="local email evidence contains the visible identifier",
            ),
            ProbeSpec(
                kind="webhook_evidence",
                url="http://127.0.0.1:19000",
                description="local webhook evidence contains the visible identifier",
            ),
        ),
    )
    root.edges.append(confirmation)

    source = render_journey_source((root,), journey_name="discovered_demo")

    assert "from journeysdk.touchpoints.http import http_request" in source
    assert "_first_visible_identifier(page, timeout_ms=timeout_ms)" in source
    assert "_wait_for_http_json(" in source
    assert "_wait_for_email_evidence(" in source
    assert "_wait_for_webhook_evidence(" in source
    assert "_journey_machine_expected_values" in source
    assert "ticket_type = 'workshop'" in source
    validate_generated_source(source, journey_name="discovered_demo")


def test_identifier_sanitization_and_candidate_dedupe() -> None:
    candidates = _dedupe_candidates(
        [
            CandidateAction("Open Admin", "first", "page.goto('http://example.test/admin')"),
            CandidateAction("Open Admin", "dupe", "page.goto('http://example.test/admin')"),
            CandidateAction("Open Checkout", "second", "page.goto('http://example.test/checkout')"),
        ]
    )

    assert [candidate.name for candidate in candidates] == ["Open Admin", "Open Checkout"]


def test_max_model_calls_validation() -> None:
    with pytest.raises(ValueError, match="--max-model-calls"):
        _normalize_options(
            DiscoverOptions(
                urls=("http://example.test",),
                max_model_calls=-1,
            )
        )


def test_normalize_options_accepts_page_state_start_without_urls() -> None:
    normalized = _normalize_options(
        DiscoverOptions(
            start_page_state=BrowserStartState(
                url="http://example.test/main",
                cookies=({"name": "session", "value": "abc", "url": "http://example.test"},),
                local_storage=(("feature", "enabled"),),
            ),
            anchor_step="open_main_page",
        )
    )

    assert normalized.urls == ()
    assert normalized.start_page_state is not None
    assert normalized.start_page_state.url == "http://example.test/main"
    assert normalized.start_page_state.local_storage == (("feature", "enabled"),)
    assert normalized.anchor_step == "open_main_page"


def test_discover_variant_and_probe_option_validation() -> None:
    with pytest.raises(ValueError, match="--max-variants-per-control"):
        _normalize_options(
            DiscoverOptions(
                urls=("http://example.test",),
                max_variants_per_control=0,
            )
        )

    with pytest.raises(ValueError, match="--side-effect-probes"):
        _normalize_options(
            DiscoverOptions(
                urls=("http://example.test",),
                side_effect_probes="invalid",  # type: ignore[arg-type]
            )
        )
