"""Hold the copy in pylint_plugins/ha_custom_pylint to core's files at the recorded tag.

UPSTREAM.json records the sha256 of every core file the copy carries, at its core_tag,
and CHANGES.diff every edit the copy makes to them. Each copied file, with core's
package name put back and its CHANGES.diff hunks undone, must hash to the recorded
value, so an edit to any file that CHANGES.diff does not carry fails here. No network:
the hashes are the record of core's bytes.
"""

import hashlib
import importlib.util
import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "pylint_upstream", _ROOT / "scripts" / "pylint_upstream.py"
)
upstream = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(upstream)

_PACKAGE = upstream.DEFAULT_UPSTREAM.parent
_UPSTREAM = json.loads(upstream.DEFAULT_UPSTREAM.read_text())
_RECORDED = {
    record["file"]: record["sha256"] for record in _UPSTREAM["carried"].values()
} | _UPSTREAM["support_files"]
_CHANGES = upstream.parse_changes((_PACKAGE / upstream.CHANGES).read_text())
_NOTICE = (_PACKAGE / "NOTICE").read_text()


def test_every_copied_file_is_recorded() -> None:
    """The copy carries exactly the files UPSTREAM.json records."""
    copied = {
        path.relative_to(_PACKAGE).as_posix()
        for path in _PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    assert copied == set(_RECORDED)


def test_notice_names_exactly_the_files_changes_carries() -> None:
    """NOTICE explains every file CHANGES.diff edits, and names no unedited one."""
    assert _CHANGES
    assert set(_RECORDED) >= set(_CHANGES)
    named = {path for path in _RECORDED if path in _NOTICE}
    assert named == set(_CHANGES)


@pytest.mark.parametrize("path", sorted(_RECORDED))
def test_each_file_is_core_at_the_tag_but_for_its_changes(path: str) -> None:
    """Undo the name and the file's CHANGES.diff hunks, and core's bytes come back."""
    copy = upstream.as_core((_PACKAGE / path).read_text())
    core = upstream.revert(copy, _CHANGES.get(path, []))
    assert hashlib.sha256(core.encode()).hexdigest() == _RECORDED[path], (
        f"{path} differs from core {_UPSTREAM['core_tag']} beyond CHANGES.diff: "
        "undo the edit, or record it with pylint_upstream.py --write"
    )
