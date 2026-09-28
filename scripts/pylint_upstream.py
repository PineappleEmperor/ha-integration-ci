"""Report core pylint plugin changes against our copy in pylint_plugins/ha_custom_pylint.

UPSTREAM.json records the core tag the copy came from, every message core's
plugin defined at that tag (carried here, or skipped with a reason), the core
file each carried message lives in with its sha256, and the sha256 of every
other core file the copy carries. Given a newer core tag, this reports:

- carried messages whose core file changed, moved or vanished;
- carried support files (helpers, constants) that changed or vanished;
- message ids core added or removed across the whole plugin.

Exit 1 when anything changed, 0 when nothing did. ``--write`` records the tag
and its hashes once the changes are ported; it refuses while an added or removed
id is still untriaged in UPSTREAM.json, or a recorded support file has vanished,
and says what to do about each.
"""

import argparse
import ast
import hashlib
import http.client
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
import time
from typing import IO, Any
import urllib.error
import urllib.request

PLUGIN_PATH = "pylint/plugins/pylint_home_assistant"
TARBALL_URL = "https://codeload.github.com/home-assistant/core/tar.gz/refs/tags/{tag}"
ATTEMPTS = 3
# A reset or timeout mid-read raises the bare OSError, not a URLError around it.
RETRIED = (
    http.client.IncompleteRead,
    tarfile.ReadError,
    urllib.error.URLError,
    ConnectionError,
    TimeoutError,
)
_sleep = time.sleep  # a seam for the tests
# What --write refuses to record over, by the report line's kind, and the fix.
UNTRIAGED = {
    "added in core, untriaged": (
        "give the id an entry under carried, or under skipped with a reason"
    ),
    "removed from core": "port core's removal and drop the id from carried or skipped",
    "vanished": (
        "port core's removal: delete the file from the copy and its entry "
        "from support_files"
    ),
}
DEFAULT_UPSTREAM = (
    Path(__file__).resolve().parents[1]
    / "pylint_plugins"
    / "ha_custom_pylint"
    / "UPSTREAM.json"
)


def _open_url(url: str) -> IO[bytes]:
    """Open *url* for streaming; a seam for the tests."""
    return urllib.request.urlopen(url, timeout=120)


def plugin_files_from_dir(core_dir: Path) -> dict[str, bytes]:
    """Return the plugin's Python files under a core checkout, by relative path."""
    plugin_dir = core_dir / PLUGIN_PATH
    if not plugin_dir.is_dir():
        msg = f"{plugin_dir} is not a directory"
        raise FileNotFoundError(msg)
    return {
        path.relative_to(plugin_dir).as_posix(): path.read_bytes()
        for path in sorted(plugin_dir.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def plugin_files_from_tarball(stream: IO[bytes]) -> dict[str, bytes]:
    """Return the plugin's Python files from a streamed core tarball."""
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            # parts[0] is the archive's top directory, core-<tag>.
            inner = PurePosixPath(*parts[1:]) if len(parts) > 1 else None
            if (
                inner is None
                or not member.isfile()
                or inner.suffix != ".py"
                or not inner.is_relative_to(PLUGIN_PATH)
            ):
                continue
            extracted = archive.extractfile(member)
            if extracted is not None:
                files[inner.relative_to(PLUGIN_PATH).as_posix()] = extracted.read()
    if not files:
        msg = f"no {PLUGIN_PATH}/*.py in the tarball"
        raise FileNotFoundError(msg)
    return files


class DownloadError(Exception):
    """Core's tarball could not be read: codeload refused it, or every attempt failed."""


def download_plugin_files(tag: str) -> dict[str, bytes]:
    """Download core's tarball at *tag* and return the plugin's Python files.

    codeload drops a stream now and then, so a dropped or truncated read is
    retried, with a short backoff, up to ATTEMPTS times in all. An HTTP error
    status is not retried, since codeload would answer the same again.
    """
    url = TARBALL_URL.format(tag=tag)
    error: Exception | None = None
    for attempt in range(ATTEMPTS):
        if attempt:
            _sleep(2 * attempt)
        try:
            with _open_url(url) as response:
                return plugin_files_from_tarball(response)
        except urllib.error.HTTPError as err:
            # codeload answered; asking again gets the same answer.
            msg = (
                f"codeload answered HTTP {err.code} for core {tag}; the tag {tag} "
                "may not exist, so check it against core's releases."
            )
            raise DownloadError(msg) from err
        except RETRIED as err:
            error = err
    msg = (
        f"Could not read core {tag} from codeload after {ATTEMPTS} attempts "
        f"({type(error).__name__}); try again, or pass --core-dir."
    )
    raise DownloadError(msg) from error


def messages_in(files: dict[str, bytes]) -> dict[str, tuple[str, str]]:
    """Map every message id a checker's ``msgs`` dict defines to (symbol, file)."""
    found: dict[str, tuple[str, str]] = {}
    for path, source in sorted(files.items()):
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            match node:
                case (
                    ast.Assign(targets=[ast.Name(id="msgs")], value=ast.Dict() as msgs)
                    | ast.AnnAssign(
                        target=ast.Name(id="msgs"), value=ast.Dict() as msgs
                    )
                ):
                    for key, value in zip(msgs.keys, msgs.values, strict=True):
                        match key, value:
                            case (
                                ast.Constant(value=str(msg_id)),
                                ast.Tuple(
                                    elts=[_, ast.Constant(value=str(symbol)), *_]
                                ),
                            ):
                                found[msg_id] = (symbol, path)
    return found


def sha256(data: bytes) -> str:
    """Return the hex sha256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def compare(upstream: dict[str, Any], files: dict[str, bytes]) -> list[str]:
    """Return one line per difference between UPSTREAM.json and core's *files*."""
    problems: list[str] = []
    core_messages = messages_in(files)
    carried: dict[str, dict[str, str]] = upstream["carried"]
    skipped: dict[str, dict[str, str]] = upstream["skipped"]

    changed: dict[str, list[str]] = {}
    for msg_id, record in sorted(carried.items()):
        if msg_id not in core_messages:
            continue  # reported below as removed
        _, core_file = core_messages[msg_id]
        if "file" not in record:
            problems.append(f"unrecorded: {msg_id} ({core_file}), run --write")
        elif core_file != record["file"]:
            problems.append(f"moved: {msg_id} {record['file']} -> {core_file}")
        elif sha256(files[core_file]) != record["sha256"]:
            changed.setdefault(core_file, []).append(msg_id)
    problems.extend(
        f"changed: {path} ({', '.join(ids)})" for path, ids in sorted(changed.items())
    )

    for path, digest in sorted(upstream["support_files"].items()):
        if path not in files:
            problems.append(f"vanished: {path}")
        elif sha256(files[path]) != digest:
            problems.append(f"changed: {path}")

    known = set(carried) | set(skipped)
    problems.extend(
        f"removed from core: {msg_id} {(carried | skipped)[msg_id].get('symbol', '')}"
        for msg_id in sorted(known - set(core_messages))
    )
    problems.extend(
        f"added in core, untriaged: {msg_id} {core_messages[msg_id][0]} "
        f"({core_messages[msg_id][1]})"
        for msg_id in sorted(set(core_messages) - known)
    )
    return problems


def updated(upstream: dict[str, Any], files: dict[str, bytes], tag: str) -> dict:
    """Return UPSTREAM.json recorded against *tag*'s files."""
    core_messages = messages_in(files)
    message_files = {path for _, path in core_messages.values()}
    carried = {
        msg_id: {
            "symbol": core_messages[msg_id][0],
            "file": core_messages[msg_id][1],
            "sha256": sha256(files[core_messages[msg_id][1]]),
        }
        for msg_id in sorted(upstream["carried"])
    }
    skipped = {
        msg_id: {"symbol": core_messages[msg_id][0], "reason": record["reason"]}
        for msg_id, record in sorted(upstream["skipped"].items())
    }
    support_files = {
        path: sha256(files[path])
        for path in sorted(upstream["support_files"])
        if path not in message_files
    }
    return {
        "core_tag": tag,
        "source": f"https://github.com/home-assistant/core/tree/{tag}/{PLUGIN_PATH}",
        "carried": carried,
        "skipped": skipped,
        "support_files": support_files,
    }


def main(argv: list[str] | None = None) -> int:
    """Compare UPSTREAM.json with core's plugin at ``--tag``; 1 on any change."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="core release tag, e.g. 2026.10.0")
    parser.add_argument(
        "--core-dir",
        type=Path,
        help="a local core checkout at the tag, instead of downloading it",
    )
    parser.add_argument("--upstream", type=Path, default=DEFAULT_UPSTREAM)
    parser.add_argument(
        "--write",
        action="store_true",
        help="record the tag and its hashes once the changes are ported",
    )
    args = parser.parse_args(argv)

    upstream = json.loads(args.upstream.read_text())
    try:
        files = (
            plugin_files_from_dir(args.core_dir)
            if args.core_dir
            else download_plugin_files(args.tag)
        )
    except DownloadError as err:
        print(err, file=sys.stderr)
        return 1
    problems = compare(upstream, files)

    if args.write:
        untriaged = [line for line in problems if line.split(":")[0] in UNTRIAGED]
        if untriaged:
            print("Resolve these before --write:")
            print(
                "\n".join(
                    f"  {line}\n    {UNTRIAGED[line.split(':')[0]]}"
                    for line in untriaged
                )
            )
            return 1
        record = updated(upstream, files, args.tag)
        args.upstream.write_text(json.dumps(record, indent=2) + "\n")
        print(f"{args.upstream} now records core {args.tag}.")
        return 0

    print(f"Core {args.tag} against our copy of {upstream['core_tag']}:")
    if not problems:
        print("  nothing changed.")
        return 0
    print("\n".join(f"  {line}" for line in problems))
    return 1


if __name__ == "__main__":
    sys.exit(main())
