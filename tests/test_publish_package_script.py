from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
import urllib.error

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT_DIR / "scripts" / "assert_release_tag.py"


def _load_release_helper():
    spec = importlib.util.spec_from_file_location("assert_release_tag", HELPER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_helper = _load_release_helper()


def _write_pyproject(project_root: Path, version: str = "0.1.0") -> None:
    project_root.joinpath("pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n',
        encoding="utf-8",
    )


def _without_release_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("JOURNEY_RELEASE_TAG", None)
    env.pop("GITHUB_REF_NAME", None)
    return env


def test_publish_package_script_requires_publish_token() -> None:
    script = ROOT_DIR / "scripts" / "publish_package.sh"
    env = _without_release_env()
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
    env = _without_release_env()
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


def test_release_tag_may_start_with_v_or_version_number(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)

    assert release_helper.assert_tag_matches_project_version("v0.1.0", tmp_path) == "0.1.0"
    assert release_helper.assert_tag_matches_project_version("0.1.0", tmp_path) == "0.1.0"


def test_release_tag_must_match_project_version(tmp_path: Path) -> None:
    _write_pyproject(tmp_path)

    with pytest.raises(release_helper.ReleaseAssertionError, match="declares version 0.1.0"):
        release_helper.assert_tag_matches_project_version("v0.2.0", tmp_path)


def test_release_tag_rejects_malformed_and_empty_values() -> None:
    with pytest.raises(release_helper.ReleaseAssertionError, match="blank"):
        release_helper.normalize_release_tag("")

    with pytest.raises(release_helper.ReleaseAssertionError, match="X.Y.Z"):
        release_helper.normalize_release_tag("release-0.1.0")


def test_pypi_check_fails_when_version_already_exists() -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    seen: dict[str, object] = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return Response()

    with pytest.raises(release_helper.ReleaseAssertionError, match="already exists"):
        release_helper.assert_pypi_version_absent(
            "journey-sdk",
            "0.1.0",
            opener=opener,
        )

    assert seen == {
        "url": "https://pypi.org/pypi/journey-sdk/0.1.0/json",
        "timeout": 10.0,
    }


def test_pypi_check_allows_missing_version() -> None:
    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "Not Found",
            hdrs=None,
            fp=None,
        )

    release_helper.assert_pypi_version_absent(
        "journey-sdk",
        "0.1.0",
        opener=opener,
    )
