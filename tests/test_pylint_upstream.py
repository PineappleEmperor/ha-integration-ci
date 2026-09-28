"""Unit tests for scripts/pylint_upstream.py, against a fake core tree. No network."""

import http.client
import importlib.util
import io
import json
import pathlib
import tarfile
import urllib.error

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "pylint_upstream", _SCRIPTS / "pylint_upstream.py"
)
upstream = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(upstream)

_CHECKER_A = '''"""Checker A."""


class A:
    msgs = {
        "C0001": ("one", "rule-one", "The first rule."),
        "W0002": ("two", "rule-two", "The second rule."),
    }
'''
_CHECKER_B = '''"""Checker B."""


class B:
    msgs: dict = {"C0003": ("three", "rule-three", "The third rule.")}
'''
_HELPER = '"""A helper."""\n\nVALUE = 1\n'


def _core(tmp_path: pathlib.Path, **files: str | None) -> pathlib.Path:
    """A core checkout; each keyword replaces a plugin file, ``a__b`` for ``a/b.py``."""
    root = tmp_path / "core"
    plugin = root / upstream.PLUGIN_PATH
    contents: dict[str, str | None] = {
        "checkers/a.py": _CHECKER_A,
        "checkers/b.py": _CHECKER_B,
        "helpers/h.py": _HELPER,
    }
    contents.update(
        {f"{name.replace('__', '/')}.py": text for name, text in files.items()}
    )
    for rel, text in contents.items():
        if text is None:
            continue
        (plugin / rel).parent.mkdir(parents=True, exist_ok=True)
        (plugin / rel).write_text(text)
    return root


def _recorded(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> pathlib.Path:
    """UPSTREAM.json recorded against the unchanged fake core at tag 1.0.0."""
    path = tmp_path / "UPSTREAM.json"
    path.write_text(
        json.dumps(
            {
                "core_tag": "bootstrap",
                "carried": {"C0001": {}, "W0002": {}},
                "skipped": {"C0003": {"reason": "covered elsewhere"}},
                "support_files": {"helpers/h.py": ""},
            }
        )
    )
    core = _core(tmp_path / "old")
    argv = ["--tag", "1.0.0", "--core-dir", str(core), "--upstream", str(path)]
    assert upstream.main([*argv, "--write"]) == 0
    capsys.readouterr()
    return path


def _run(upstream_path: pathlib.Path, core: pathlib.Path, *extra: str) -> int:
    return upstream.main(
        [
            "--tag",
            "2.0.0",
            "--core-dir",
            str(core),
            "--upstream",
            str(upstream_path),
            *extra,
        ]
    )


def test_write_records_symbols_files_and_hashes(tmp_path, capsys) -> None:
    """--write fills in each message's symbol, core file and that file's sha256."""
    record = json.loads(_recorded(tmp_path, capsys).read_text())
    assert record["core_tag"] == "1.0.0"
    assert record["carried"]["C0001"] == {
        "symbol": "rule-one",
        "file": "checkers/a.py",
        "sha256": upstream.sha256(_CHECKER_A.encode()),
    }
    assert record["skipped"]["C0003"] == {
        "symbol": "rule-three",
        "reason": "covered elsewhere",
    }
    assert record["support_files"] == {
        "helpers/h.py": upstream.sha256(_HELPER.encode())
    }


def test_unchanged_core_exits_zero(tmp_path, capsys) -> None:
    """The same files at a new tag: nothing to report."""
    path = _recorded(tmp_path, capsys)
    assert _run(path, _core(tmp_path / "new")) == 0
    assert "nothing changed" in capsys.readouterr().out


def test_a_changed_checker_names_its_file_and_carried_ids(tmp_path, capsys) -> None:
    """A carried message's core file changed: the file and the ids it holds."""
    path = _recorded(tmp_path, capsys)
    core = _core(tmp_path / "new", checkers__a=_CHECKER_A + "\nEXTRA = 1\n")
    assert _run(path, core) == 1
    assert "changed: checkers/a.py (C0001, W0002)" in capsys.readouterr().out


def test_a_skipped_message_file_is_not_tracked(tmp_path, capsys) -> None:
    """Only carried messages' files are hashed; a skipped one's may change freely."""
    path = _recorded(tmp_path, capsys)
    core = _core(tmp_path / "new", checkers__b=_CHECKER_B + "\nEXTRA = 1\n")
    assert _run(path, core) == 0


def test_a_changed_or_vanished_support_file_is_reported(tmp_path, capsys) -> None:
    """Helpers the copy carries count too, since every checker leans on them."""
    path = _recorded(tmp_path, capsys)
    assert _run(path, _core(tmp_path / "new", helpers__h=_HELPER + "X = 2\n")) == 1
    assert "changed: helpers/h.py" in capsys.readouterr().out
    assert _run(path, _core(tmp_path / "gone", helpers__h=None)) == 1
    assert "vanished: helpers/h.py" in capsys.readouterr().out


def test_a_moved_message_names_both_files(tmp_path, capsys) -> None:
    """A carried id now defined in another file is a move, not a removal."""
    path = _recorded(tmp_path, capsys)
    core = _core(
        tmp_path / "new",
        checkers__a=_CHECKER_A.replace(
            '"W0002": ("two", "rule-two", "The second rule."),\n', ""
        ),
        checkers__c='class C:\n    msgs = {"W0002": ("two", "rule-two", "Moved.")}\n',
    )
    assert _run(path, core) == 1
    assert "moved: W0002 checkers/a.py -> checkers/c.py" in capsys.readouterr().out


def test_added_and_removed_ids_are_reported_across_the_plugin(tmp_path, capsys) -> None:
    """A new core rule is seen for triage; a dropped one, carried or skipped, too."""
    path = _recorded(tmp_path, capsys)
    core = _core(
        tmp_path / "new",
        checkers__b='class B:\n    msgs = {"E0004": ("four", "rule-four", "New.")}\n',
    )
    assert _run(path, core) == 1
    out = capsys.readouterr().out
    assert "added in core, untriaged: E0004 rule-four (checkers/b.py)" in out
    assert "removed from core: C0003 rule-three" in out


def test_write_refuses_while_an_id_is_untriaged(tmp_path, capsys) -> None:
    """--write cannot quietly adopt a rule nobody decided on."""
    path = _recorded(tmp_path, capsys)
    before = path.read_text()
    core = _core(
        tmp_path / "new",
        checkers__b=_CHECKER_B.replace("}", ', "E0004": ("f", "rule-four", "New.")}'),
    )
    assert _run(path, core, "--write") == 1
    assert "E0004" in capsys.readouterr().out
    assert path.read_text() == before


def test_write_refuses_while_a_support_file_vanished(tmp_path, capsys) -> None:
    """A helper core deleted cannot be hashed; --write says what to do instead."""
    path = _recorded(tmp_path, capsys)
    before = path.read_text()
    assert _run(path, _core(tmp_path / "gone", helpers__h=None), "--write") == 1
    out = capsys.readouterr().out
    assert "vanished: helpers/h.py" in out
    assert "delete the file from the copy and its entry from support_files" in out
    assert path.read_text() == before


def test_a_triaged_new_id_is_recorded_by_write(tmp_path, capsys) -> None:
    """Added to carried as {}, a new id is unrecorded until --write fills it in."""
    path = _recorded(tmp_path, capsys)
    record = json.loads(path.read_text())
    record["carried"]["E0004"] = {}
    path.write_text(json.dumps(record))
    core = _core(
        tmp_path / "new",
        checkers__b=_CHECKER_B.replace("}", ', "E0004": ("f", "rule-four", "New.")}'),
    )
    assert _run(path, core) == 1
    assert "unrecorded: E0004 (checkers/b.py), run --write" in capsys.readouterr().out
    assert _run(path, core, "--write") == 0
    assert json.loads(path.read_text())["carried"]["E0004"]["file"] == "checkers/b.py"
    assert _run(path, core) == 0


def _tarball(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_the_download_keeps_only_the_plugin_python_files(monkeypatch) -> None:
    """The tag goes into codeload's URL, and only the plugin's .py files are kept."""
    prefix = f"core-9.9.9/{upstream.PLUGIN_PATH}"
    tarball = _tarball(
        {
            f"{prefix}/checkers/a.py": _CHECKER_A,
            f"{prefix}/README.md": "not python",
            "core-9.9.9/homeassistant/const.py": "OTHER = 1\n",
        }
    )
    opened: list[str] = []

    def fake_open(url: str) -> io.BytesIO:
        opened.append(url)
        return io.BytesIO(tarball)

    monkeypatch.setattr(upstream, "_open_url", fake_open)
    files = upstream.download_plugin_files("9.9.9")
    assert opened == [
        "https://codeload.github.com/home-assistant/core/tar.gz/refs/tags/9.9.9"
    ]
    assert files == {"checkers/a.py": _CHECKER_A.encode()}


def test_a_tarball_without_the_plugin_is_an_error() -> None:
    """A tag from before the plugin existed must not read as 'everything removed'."""
    with pytest.raises(FileNotFoundError):
        upstream.plugin_files_from_tarball(
            io.BytesIO(_tarball({"core-1/homeassistant/const.py": "X = 1\n"}))
        )


_GOOD_TARBALL = _tarball(
    {f"core-9.9.9/{upstream.PLUGIN_PATH}/checkers/a.py": _CHECKER_A}
)


def _incomplete_read() -> io.BytesIO:
    raise http.client.IncompleteRead(b"partial")


def _truncated_tarball() -> io.BytesIO:
    return io.BytesIO(_GOOD_TARBALL[: len(_GOOD_TARBALL) // 2])


class _FailingStream(io.BytesIO):
    """A response whose body read raises *error*, as a dropped socket does."""

    def __init__(self, error: OSError) -> None:
        super().__init__(_GOOD_TARBALL)
        self.error = error

    def read(self, size: int | None = -1) -> bytes:
        raise self.error


def _connection_reset() -> io.BytesIO:
    return _FailingStream(ConnectionResetError(104, "Connection reset by peer"))


def _read_timeout() -> io.BytesIO:
    return _FailingStream(TimeoutError("The read operation timed out"))


def _unreachable() -> io.BytesIO:
    raise urllib.error.URLError(OSError(101, "Network is unreachable"))


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(_incomplete_read, id="incomplete_read"),
        pytest.param(_truncated_tarball, id="truncated_tarball"),
        pytest.param(_connection_reset, id="connection_reset"),
        pytest.param(_read_timeout, id="read_timeout"),
        pytest.param(_unreachable, id="url_error"),
    ],
)
def test_a_failed_download_is_retried(monkeypatch, failure) -> None:
    """Two failed reads then a good one pass: codeload drops the stream now and then."""
    attempts: list[str] = []
    waits: list[float] = []

    def fake_open(url: str) -> io.BytesIO:
        attempts.append(url)
        return failure() if len(attempts) < 3 else io.BytesIO(_GOOD_TARBALL)

    monkeypatch.setattr(upstream, "_open_url", fake_open)
    monkeypatch.setattr(upstream, "_sleep", waits.append)
    assert upstream.download_plugin_files("9.9.9") == {
        "checkers/a.py": _CHECKER_A.encode()
    }
    assert len(attempts) == 3
    assert len(waits) == 2


def test_a_download_that_keeps_failing_ends_in_one_line(
    tmp_path, monkeypatch, capsys
) -> None:
    """After the last attempt: exit 1 and one line saying so, not a traceback."""
    attempts: list[str] = []

    def fake_open(url: str) -> io.BytesIO:
        attempts.append(url)
        return _connection_reset()

    monkeypatch.setattr(upstream, "_open_url", fake_open)
    monkeypatch.setattr(upstream, "_sleep", lambda _: None)
    path = tmp_path / "UPSTREAM.json"
    path.write_text(
        json.dumps(
            {"core_tag": "1.0.0", "carried": {}, "skipped": {}, "support_files": {}}
        )
    )
    assert upstream.main(["--tag", "9.9.9", "--upstream", str(path)]) == 1
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).strip().splitlines()
    assert len(attempts) == 3
    assert len(lines) == 1, lines
    assert "9.9.9" in lines[0] and "3 attempts" in lines[0]


def test_an_http_error_is_not_retried(tmp_path, monkeypatch, capsys) -> None:
    """A 404 for a mistyped tag answers the same every time: one try, and why."""
    attempts: list[str] = []

    def fake_open(url: str) -> io.BytesIO:
        attempts.append(url)
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(upstream, "_open_url", fake_open)
    monkeypatch.setattr(upstream, "_sleep", lambda _: None)
    path = tmp_path / "UPSTREAM.json"
    path.write_text(
        json.dumps(
            {"core_tag": "1.0.0", "carried": {}, "skipped": {}, "support_files": {}}
        )
    )
    assert upstream.main(["--tag", "2026.13.0", "--upstream", str(path)]) == 1
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).strip().splitlines()
    assert len(attempts) == 1
    assert len(lines) == 1, lines
    assert "HTTP 404" in lines[0]
    assert "2026.13.0 may not exist" in lines[0]
    assert "try again" not in lines[0]


def test_the_recorded_copy_matches_its_own_package() -> None:
    """UPSTREAM.json names every file our copy carries, and only those."""
    record = json.loads(upstream.DEFAULT_UPSTREAM.read_text())
    package = upstream.DEFAULT_UPSTREAM.parent
    ours = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    tracked = set(record["support_files"]) | {
        entry["file"] for entry in record["carried"].values()
    }
    assert tracked == ours
