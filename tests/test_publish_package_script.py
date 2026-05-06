from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_publish_package_script_requires_publish_token() -> None:
    script = ROOT_DIR / "scripts" / "publish_package.sh"
    env = os.environ.copy()
    env.pop("UV_PUBLISH_TOKEN", None)

    result = subprocess.run(
        ["bash", str(script)],
        cwd=ROOT_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "UV_PUBLISH_TOKEN must be set" in result.stderr


def test_publish_package_script_reexecs_when_invoked_with_zsh() -> None:
    script = ROOT_DIR / "scripts" / "publish_package.sh"
    env = os.environ.copy()
    env.pop("UV_PUBLISH_TOKEN", None)

    result = subprocess.run(
        ["/bin/zsh", str(script)],
        cwd=ROOT_DIR.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "BASH_SOURCE" not in result.stderr
    assert "UV_PUBLISH_TOKEN must be set" in result.stderr
