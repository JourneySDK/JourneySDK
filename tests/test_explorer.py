from __future__ import annotations

import pytest

from journeysdk.explorer import (
    CandidateAction,
    ExploredEdge,
    ExploredNode,
    PageSnapshot,
    parse_candidate_actions,
    render_journey_source,
    validate_generated_source,
)


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


def test_rendered_journey_source_compiles_with_branches() -> None:
    root = ExploredNode(
        node_id="root",
        start_url="http://example.test/",
        snapshot=_snapshot("http://example.test/", "Home", "Create Admin"),
        depth=0,
    )
    checkout = ExploredEdge(
        edge_id="checkout",
        parent=root,
        action=CandidateAction(
            name="Complete checkout",
            description="Complete attendee checkout.",
            code='page.get_by_text("Checkout").click(timeout=timeout_ms)',
        ),
        snapshot=_snapshot("http://example.test/checkout", "Checkout"),
    )
    admin = ExploredEdge(
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

    source = render_journey_source((root,), journey_name="explored_demo")

    assert "if branch(replay_from=open_home_page):" in source
    assert "elif branch(replay_from=open_home_page):" in source
    assert "def complete_checkout" in source
    assert "def open_back_office" in source
    validate_generated_source(source, journey_name="explored_demo")
