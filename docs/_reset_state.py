"""Shared tutorial reset helpers."""

from __future__ import annotations

import shutil
from pathlib import Path


def reset_default_state(source_file: str) -> None:
    shutil.rmtree(
        Path(source_file).resolve().parent / ".journey",
        ignore_errors=True,
    )
