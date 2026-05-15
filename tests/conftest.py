from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_default_test_state() -> None:
    state_root = Path(__file__).parent / ".journey"
    shutil.rmtree(state_root, ignore_errors=True)
    yield
    shutil.rmtree(state_root, ignore_errors=True)
