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
    """The pinned test harness decides the release when the repository has one."""
    (tmp_path / "requirements.test.txt").write_text(
        "pytest-homeassistant-custom-component==0.13.367\n"
    )
    result, calls = _run_step("Install", tmp_path, ["uv"])
    assert result.returncode == 0, result.stderr
    assert any("-r requirements.test.txt" in call for call in calls)
    assert not any("homeassistant==" in call for call in calls)
