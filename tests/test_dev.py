from __future__ import annotations

from pathlib import Path

from journeysdk.dev import DevInspectionContext, inspect_dev_page, render_dev_pretty


class _FakeBody:
    def inner_text(self, *, timeout: int) -> str:
        assert timeout == 1000
        return "Start chat\nUpload attachment"


class _FakePage:
    url = "http://example.test/chat"

    def title(self) -> str:
        return "Chat"

    def content(self) -> str:
        return "<button id='start-chat' onclick='start()'>Start chat</button>"

    def locator(self, selector: str) -> _FakeBody:
        assert selector == "body"
        return _FakeBody()

    def screenshot(self, *, path: str, full_page: bool) -> None:
        assert full_page is True
        Path(path).write_bytes(b"fake-png")

    def evaluate(self, script: str) -> list[dict[str, object]]:
        assert "actionableEvents" in script
        return [
            {
                "tag": "button",
                "role": "button",
                "label": "Start chat",
                "text": "Start chat",
                "selector": "#start-chat",
                "eventTypes": ["click"],
                "detection": "inline_handler",
                "enabled": True,
                "visible": True,
                "boundingBox": {"x": 1, "y": 2, "width": 100, "height": 30},
            },
            {
                "tag": "input",
                "role": "",
                "label": "Upload attachment",
                "text": "",
                "selector": "input[name=\"attachment\"]",
                "eventTypes": ["change"],
                "detection": "semantic_control",
                "enabled": True,
                "visible": True,
                "boundingBox": {"x": 3, "y": 4, "width": 10, "height": 10},
            },
        ]


def test_dev_page_inspection_writes_artifacts_and_actions(tmp_path: Path) -> None:
    result = inspect_dev_page(
        _FakePage(),
        context=DevInspectionContext(
            file="journeys/app.py",
            journey="app_journey",
            case_id="case_1",
            paused_step="open_chat",
            paused_step_result_name="open_chat_page",
        ),
        artifact_root=tmp_path / ".journey" / "dev",
    )

    assert result.rendered_page.url == "http://example.test/chat"
    assert Path(result.rendered_page.html_path).read_text(encoding="utf-8").startswith("<button")
    assert Path(result.rendered_page.text_path).read_text(encoding="utf-8") == "Start chat\nUpload attachment"
    assert result.rendered_page.screenshot_path is not None
    assert Path(result.rendered_page.screenshot_path).read_bytes() == b"fake-png"
    assert result.dev_result_path is not None
    assert Path(result.dev_result_path).exists()
    assert [element.label for element in result.actionable_elements] == [
        "Start chat",
        "Upload attachment",
    ]
    assert result.actionable_elements[0].event_types == ("click",)
    assert result.actionable_elements[0].locator_hint == "page.get_by_role('button', name='Start chat')"
    assert result.candidate_flows[0].title == "Start a new chat"
    assert "branch(replay_from=open_chat_page)" in result.extension_instructions.journey_insertion_template
    assert "journey loop" in result.extension_instructions.verification_commands[0]

    pretty = "\n".join(line.text for line in render_dev_pretty(result))
    assert "Rendered page artifacts" in pretty
    assert "Structured result" in pretty
    assert "Candidate flows" in pretty
    assert "Actionable controls" in pretty
    assert "Start chat" in pretty
    assert "Extend this journey" in pretty


class _PayloadPage(_FakePage):
    def __init__(self, payload: list[dict[str, object]]) -> None:
        self._payload = payload

    def evaluate(self, script: str) -> list[dict[str, object]]:
        assert "actionableEvents" in script
        return self._payload


def _inspect_payload(tmp_path: Path, payload: list[dict[str, object]]):
    return inspect_dev_page(
        _PayloadPage(payload),
        context=DevInspectionContext(
            file="journeys/app.py",
            journey="app_journey",
            case_id="case_1",
            paused_step="open_chat",
            paused_step_result_name="open_chat_page",
        ),
        artifact_root=tmp_path / ".journey" / "dev",
    )


def test_actionable_controls_drop_noisy_delegated_descendants(tmp_path: Path) -> None:
    result = _inspect_payload(
        tmp_path,
        [
            {
                "tag": "button",
                "role": "button",
                "label": "Start a new chat",
                "text": "Start a new chat",
                "selector": "[data-testid=\"new-chat-button\"]",
                "eventTypes": ["click"],
                "detection": "inline_handler",
                "enabled": True,
                "visible": True,
                "actionType": "click",
                "boundingBox": {"x": 1, "y": 2, "width": 100, "height": 30},
            },
            {
                "tag": "svg",
                "role": "",
                "label": "",
                "text": "",
                "selector": "body > button > svg",
                "eventTypes": [],
                "detection": "delegated_ancestor",
                "enabled": True,
                "visible": True,
                "actionType": "click",
                "boundingBox": {"x": 1, "y": 2, "width": 10, "height": 10},
            },
            {
                "tag": "path",
                "role": "",
                "label": "",
                "text": "",
                "selector": "body > button > svg > path",
                "eventTypes": [],
                "detection": "delegated_ancestor",
                "enabled": True,
                "visible": True,
                "actionType": "click",
                "boundingBox": {"x": 1, "y": 2, "width": 10, "height": 10},
            },
        ],
    )

    assert [element.label for element in result.actionable_elements] == ["Start a new chat"]
    assert result.actionable_elements[0].locator_hint == "page.get_by_test_id('new-chat-button')"
    assert result.candidate_flows[0].title == "Start a new chat"


def test_generic_button_label_uses_visible_text_and_role_locator(tmp_path: Path) -> None:
    result = _inspect_payload(
        tmp_path,
        [
            {
                "tag": "button",
                "role": "button",
                "label": "Button",
                "text": "Sign up",
                "selector": "body > aside > button:nth-of-type(1)",
                "eventTypes": [],
                "detection": "semantic_control",
                "enabled": True,
                "visible": True,
                "actionType": "click",
                "boundingBox": {"x": 1, "y": 2, "width": 100, "height": 30},
            },
        ],
    )

    assert result.actionable_elements[0].label == "Sign up"
    assert result.actionable_elements[0].locator_hint == "page.get_by_role('button', name='Sign up')"
    assert result.candidate_flows[0].title == "Sign up"


def test_composer_and_disabled_send_create_composite_flow(tmp_path: Path) -> None:
    result = _inspect_payload(
        tmp_path,
        [
            {
                "tag": "div",
                "role": "textbox",
                "label": "Type or tap the mic to speak",
                "text": "",
                "selector": "[data-testid=\"composer-input\"]",
                "eventTypes": [],
                "detection": "semantic_control",
                "enabled": True,
                "visible": True,
                "actionType": "fill",
                "boundingBox": {"x": 1, "y": 2, "width": 100, "height": 30},
            },
            {
                "tag": "button",
                "role": "button",
                "label": "Send message",
                "text": "",
                "selector": "[data-testid=\"send-message-button\"]",
                "eventTypes": [],
                "detection": "semantic_control",
                "enabled": False,
                "visible": True,
                "actionType": "click",
                "stateNote": "disabled; requires prerequisite input or state before clicking",
                "boundingBox": {"x": 1, "y": 2, "width": 100, "height": 30},
            },
        ],
    )

    flow = result.candidate_flows[0]
    assert flow.title == "Send message through composer"
    assert flow.action_type == "compose_and_submit"
    assert "fill the composer" in flow.precondition or "disabled" in flow.precondition
    assert "page.get_by_test_id('composer-input').fill" in flow.action_hints[0]
    assert "page.get_by_test_id('send-message-button').click" in flow.action_hints[1]
    assert "send_message_through_composer" in result.extension_instructions.step_function_template


def test_hidden_file_input_creates_upload_flow(tmp_path: Path) -> None:
    result = _inspect_payload(
        tmp_path,
        [
            {
                "tag": "input",
                "role": "",
                "label": "file input",
                "text": "",
                "selector": "[data-testid=\"file-input\"]",
                "eventTypes": ["change"],
                "detection": "semantic_control",
                "enabled": True,
                "visible": False,
                "actionType": "upload",
                "stateNote": "hidden file input; use set_input_files directly or click the visible attachment control first",
                "boundingBox": {"x": 0, "y": 0, "width": 0, "height": 0},
            },
            {
                "tag": "button",
                "role": "button",
                "label": "Attach file",
                "text": "",
                "selector": "[data-testid=\"attach-file-button\"]",
                "eventTypes": [],
                "detection": "semantic_control",
                "enabled": True,
                "visible": True,
                "actionType": "click",
                "boundingBox": {"x": 1, "y": 2, "width": 100, "height": 30},
            },
        ],
    )

    flow = result.candidate_flows[0]
    assert flow.title == "Upload attachment"
    assert flow.action_type == "upload"
    assert "page.get_by_test_id('file-input').set_input_files" in flow.action_hints[0]
