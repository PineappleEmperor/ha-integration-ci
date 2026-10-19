"""Hold the copy in pylint_plugins/ha_custom_pylint to core's files at the recorded tag.

UPSTREAM.json records the sha256 of every core file the copy carries, at its core_tag.
A file NOTICE does not name must hash to that value once the package name is put back;
a file NOTICE names must not, so NOTICE lists every change and no change that is not
there. No network: the hashes are the record of core's bytes.
"""

import hashlib
import json
import pathlib
import re

import pytest

_PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "pylint_plugins" / "ha_custom_pylint"
)
_UPSTREAM = json.loads((_PACKAGE / "UPSTREAM.json").read_text())
_RECORDED = {
    record["file"]: record["sha256"] for record in _UPSTREAM["carried"].values()
} | _UPSTREAM["support_files"]
_NOTICE_NAMES = set(
    re.findall(
        r"\b(?:checkers|helpers)/[\w/]+\.py\b", (_PACKAGE / "NOTICE").read_text()
    )
)


def _core_bytes(path: str) -> bytes:
    """Return our copy of *path* with core's package name put back."""
    return (
        (_PACKAGE / path)
        .read_bytes()
        .replace(b"ha_custom_pylint", b"pylint_home_assistant")
    )


def test_every_copied_file_is_recorded() -> None:
    """The copy carries exactly the files UPSTREAM.json records."""
    copied = {
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    assert copied == set(_RECORDED)


def test_notice_names_only_recorded_files() -> None:
    """Every file NOTICE names is one the copy carries."""
    assert _NOTICE_NAMES
    assert set(_RECORDED) >= _NOTICE_NAMES


@pytest.mark.parametrize("path", sorted(set(_RECORDED) - _NOTICE_NAMES))
def test_unlisted_file_is_core_at_the_tag(path: str) -> None:
    """A file NOTICE does not name is core's file at core_tag, but for the name."""
    digest = hashlib.sha256(_core_bytes(path)).hexdigest()
    assert digest == _RECORDED[path], (
        f"{path} differs from core {_UPSTREAM['core_tag']}: list the change in "
        "NOTICE or undo it"
    )


@pytest.mark.parametrize("path", sorted(_NOTICE_NAMES))
def test_listed_file_differs_from_core(path: str) -> None:
    """A file NOTICE names carries a change, so NOTICE is not stale."""
    digest = hashlib.sha256(_core_bytes(path)).hexdigest()
    assert digest != _RECORDED[path], (
        f"{path} is core {_UPSTREAM['core_tag']} unchanged: drop it from NOTICE"
    )
