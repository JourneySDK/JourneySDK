#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request


PACKAGE_NAME = "journey-sdk"
PYPI_PACKAGE_URL = "https://pypi.org/pypi/{package_name}/{version}/json"
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class ReleaseAssertionError(RuntimeError):
    """Raised when a release tag is not safe to publish."""


def read_project_version(project_root: Path) -> str:
    pyproject_path = project_root / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as pyproject_file:
            pyproject = tomllib.load(pyproject_file)
        version = pyproject["project"]["version"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseAssertionError(
            f"Could not read [project].version from {pyproject_path}."
        ) from exc

    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ReleaseAssertionError(
            f"Package version in {pyproject_path} is malformed: {version!r}."
        )
    return version


def normalize_release_tag(tag: str) -> str:
    if not tag or tag.strip() != tag:
        raise ReleaseAssertionError(
            "Release tag must not be blank or contain leading/trailing whitespace."
        )

    version = tag[1:] if tag[:1].lower() == "v" else tag
    if not VERSION_RE.fullmatch(version):
        raise ReleaseAssertionError(
            f"Release tag {tag!r} must be formatted as X.Y.Z or vX.Y.Z."
        )
    return version


def assert_tag_matches_project_version(tag: str, project_root: Path) -> str:
    tag_version = normalize_release_tag(tag)
    project_version = read_project_version(project_root)
    if tag_version != project_version:
        raise ReleaseAssertionError(
            f"Release tag {tag!r} resolves to version {tag_version}, "
            f"but pyproject.toml declares version {project_version}."
        )
    return tag_version


def assert_pypi_version_absent(
    package_name: str,
    version: str,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> None:
    package_path = urllib.parse.quote(package_name, safe="")
    version_path = urllib.parse.quote(version, safe="")
    url = PYPI_PACKAGE_URL.format(package_name=package_path, version=version_path)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "journey-sdk-release-check/1.0"},
    )

    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            if status == 200:
                raise ReleaseAssertionError(
                    f"{package_name} version {version} already exists on PyPI."
                )
            raise ReleaseAssertionError(
                f"Unexpected PyPI response while checking {package_name} {version}: "
                f"HTTP {status!r}."
            )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise ReleaseAssertionError(
            f"Unexpected PyPI response while checking {package_name} {version}: "
            f"HTTP {exc.code}."
        ) from exc
    except ReleaseAssertionError:
        raise
    except Exception as exc:
        raise ReleaseAssertionError(
            f"Could not confirm that {package_name} {version} is absent from PyPI."
        ) from exc


def assert_release_is_publishable(
    tag: str,
    project_root: Path,
    package_name: str = PACKAGE_NAME,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> str:
    version = assert_tag_matches_project_version(tag, project_root)
    assert_pypi_version_absent(package_name, version, opener=opener)
    return version


def _default_release_tag() -> str:
    return os.environ.get("JOURNEY_RELEASE_TAG") or os.environ.get("GITHUB_REF_NAME") or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify that the current release tag is safe to publish."
    )
    parser.add_argument("--tag", default=_default_release_tag())
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--package-name", default=PACKAGE_NAME)
    args = parser.parse_args(argv)

    try:
        version = assert_release_is_publishable(
            args.tag,
            args.project_root,
            package_name=args.package_name,
        )
    except ReleaseAssertionError as exc:
        print(f"Release assertion failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"Release tag {args.tag!r} matches {args.package_name} version {version}, "
        "and PyPI has no existing release for that version."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
