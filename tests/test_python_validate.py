"""Tests for .github/workflows/python-validate.yml, one step at a time.

Each test lifts a step's ``run:`` script out of the workflow and runs it in bash
with the runner's flags, in a throwaway repository, with the tools it calls
replaced by stubs that record their arguments.
"""

import json
import os
import pathlib
import subprocess
import sys

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = yaml.safe_load(
    (_ROOT / ".github" / "workflows" / "python-validate.yml").read_text()
)
_UPSTREAM = json.loads(
    (_ROOT / "pylint_plugins" / "ha_custom_pylint" / "UPSTREAM.json").read_text()
)


def _step(name: str) -> str:
    """Return the ``run:`` script of the step called *name*."""
    (step,) = (
        step
        for step in _WORKFLOW["jobs"]["lint-and-type"]["steps"]
        if step.get("name") == name
    )
    return step["run"]


def _run_step(
    name: str, repo: pathlib.Path, tools: list[str]
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run step *name* in *repo* with *tools* stubbed; return the result and calls."""
    stubs = repo / ".stubs"
    stubs.mkdir()
    calls = repo / ".calls"
    calls.touch()
    for tool in tools:
        stub = stubs / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\n')
        stub.chmod(0o755)
    (stubs / "python").symlink_to(sys.executable)
    (repo / ".ha-integration-ci").symlink_to(_ROOT)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _step(name)],
        cwd=repo,
        env={**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text().splitlines()


def test_install_pins_homeassistant_without_test_requirements(
    tmp_path: pathlib.Path,
) -> None:
    """With no requirements.test.txt, pylint infers against the rules' own release."""
    result, calls = _run_step("Install", tmp_path, ["uv"])
    assert result.returncode == 0, result.stderr
    installed = " ".join(calls).split()
    assert f"homeassistant=={_UPSTREAM['core_tag']}" in installed
    assert "homeassistant" not in installed


def test_install_takes_homeassistant_from_test_requirements(
    tmp_path: pathlib.Path,
) -> None:
    """The test harness decides the release when the repository has one.

    The harness pins the homeassistant it was built against and brings it, so
    no separate homeassistant is asked for beside it.
    """
    (tmp_path / "requirements.test.txt").write_text(
        "pytest-homeassistant-custom-component==0.13.367\n"
    )
    result, calls = _run_step("Install", tmp_path, ["uv"])
    assert result.returncode == 0, result.stderr
    assert any("-r requirements.test.txt" in call for call in calls)
    installed = " ".join(calls).split()
    assert not any(arg.startswith("homeassistant") for arg in installed)


def _repo(root: pathlib.Path, *files: str) -> pathlib.Path:
    """A repository holding one integration module and *files*."""
    for rel in ("custom_components/sample/__init__.py", *files):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text('"""A module."""\n')
    return root


def test_pylint_lints_a_tests_package(tmp_path: pathlib.Path) -> None:
    """tests/__init__.py makes the modules tests.*, the names the test rules key on."""
    repo = _repo(tmp_path, "tests/__init__.py", "tests/test_init.py")
    result, calls = _run_step("Pylint", repo, ["pylint"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert [call.split()[-1] for call in calls] == ["custom_components/", "tests/"]


def test_pylint_fails_on_tests_that_are_not_a_package(tmp_path: pathlib.Path) -> None:
    """Without tests/__init__.py every test rule would pass unseen, so the step fails."""
    repo = _repo(tmp_path, "tests/test_init.py")
    result, calls = _run_step("Pylint", repo, ["pylint"])
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "tests/__init__.py" in result.stdout
    assert [call.split()[-1] for call in calls] == ["custom_components/"]


def test_pylint_names_every_test_directory_that_is_not_a_package(
    tmp_path: pathlib.Path,
) -> None:
    """A sub-directory without __init__.py would be skipped, so each one is named."""
    repo = _repo(
        tmp_path,
        "tests/__init__.py",
        "tests/test_init.py",
        "tests/helpers/__init__.py",
        "tests/helpers/test_a.py",
        "tests/sub/test_b.py",
        "tests/other/deep/test_c.py",
        "tests/fixtures/data.json",
    )
    result, calls = _run_step("Pylint", repo, ["pylint"])
    assert result.returncode == 1
    (error,) = (line for line in result.stdout.splitlines() if "::error::" in line)
    for missing in ("sub", "other", "other/deep"):
        assert f"tests/{missing}/__init__.py" in error
    for present in ("tests/__init__.py", "helpers", "fixtures"):
        assert present not in error
    assert [call.split()[-1] for call in calls] == ["custom_components/"]
