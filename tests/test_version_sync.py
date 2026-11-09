"""Unit tests for scripts/version_sync.py.

Load the standalone script by path; it is not an importable package.
"""

import importlib.util
import pathlib

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "version_sync", _SCRIPTS / "version_sync.py"
)
vs = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(vs)

_SETUP = (
    "jobs:\n  t:\n    steps:\n      - uses: actions/setup-python@v7\n"
    "        with:\n          python-version: '{}'\n"
)


def _mypy(version: str) -> str:
    """A mypy.ini with global settings under [mypy], then a per-module section."""
    return (
        f"[mypy]\npython_version = {version}\nstrict_equality = true\n\n"
        "[mypy-acmedev.*]\ndisallow_untyped_defs = true\n"
    )


def _repo(
    tmp_path,
    *,
    workflow="3.14",
    ruff="py314",
    mypy="3.14",
    reqs="pytest-homeassistant-custom-component==0.13.354\n",
):
    """A repo declaring the python version in each of the places that carry it."""
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/python-validate.yml").write_text(
        _SETUP.format(workflow)
    )
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.ruff]\ntarget-version = "{ruff}"\n'
    )
    (tmp_path / "mypy.ini").write_text(_mypy(mypy))
    (tmp_path / "requirements.test.txt").write_text(reqs)
    return tmp_path


def test_agreeing_versions_pass(tmp_path) -> None:
    """The ordinary case: one version, written four times, all the same."""
    assert vs.problems(_repo(tmp_path)) == []


def test_a_bump_left_behind_is_caught(tmp_path) -> None:
    """The failure this exists for: the workflow moved, the linters did not."""
    found = vs.problems(_repo(tmp_path, workflow="3.15"))
    assert len(found) == 1
    assert "python version disagrees" in found[0]
    assert "python-validate.yml=3.15" in found[0]


def test_unpinned_test_harness_is_caught(tmp_path) -> None:
    """An unpinned test harness."""
    found = vs.problems(_repo(tmp_path, reqs="pytest-homeassistant-custom-component\n"))
    assert len(found) == 1
    assert "pins no test harness" in found[0]


def test_requirements_without_any_harness_are_caught(tmp_path) -> None:
    """A requirements file naming neither harness pins no HA release at all."""
    found = vs.problems(_repo(tmp_path, reqs="pytest-cov==7.1.0\n"))
    assert len(found) == 1
    assert "pins no test harness" in found[0]


@pytest.mark.parametrize(
    "line",
    [
        "pytest-homeassistant==2026.11.0",
        "pytest-homeassistant[bluetooth]==2026.11.0",
        "pytest-homeassistant == 2026.11.0b4  # the release under test",
        "pytest_homeassistant==2026.11.1",
    ],
)
def test_core_s_harness_pinned_exactly_passes(tmp_path, line: str) -> None:
    """Core's own harness, pinned to one Home Assistant release, is a pin."""
    assert vs.problems(_repo(tmp_path, reqs=f"{line}\n")) == []


@pytest.mark.parametrize(
    "line",
    [
        "pytest-homeassistant",
        "pytest-homeassistant>=2026.11.0",
        "pytest-homeassistant~=2026.11.0",
        "pytest-homeassistant==2026.11.*",
        "pytest-homeassistant==2026.11.0,<2026.12",
        "pytest-homeassistant==0.1.3",
    ],
)
def test_core_s_harness_not_pinned_to_a_release_fails(tmp_path, line: str) -> None:
    """Anything but one exact HA release can resolve to another author's 0.1.x."""
    found = vs.problems(_repo(tmp_path, reqs=f"{line}\n"))
    assert len(found) == 1
    assert "pytest-homeassistant==<Home Assistant release>" in found[0]
    assert line in found[0]


def test_both_harnesses_pinned_fail(tmp_path) -> None:
    """Core's package and the community one supply the same fixtures; never both."""
    found = vs.problems(
        _repo(
            tmp_path,
            reqs=(
                "pytest-homeassistant==2026.11.0\n"
                "pytest-homeassistant-custom-component==0.13.375\n"
            ),
        )
    )
    assert len(found) == 1
    assert "both" in found[0]


def test_absent_files_are_not_a_disagreement(tmp_path) -> None:
    """A repo that declares the version once has nothing to disagree with."""
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/python-validate.yml").write_text(
        _SETUP.format("3.14")
    )
    assert vs.problems(tmp_path) == []


def test_a_single_declaration_warns_rather_than_passing_silently(tmp_path) -> None:
    """A single declared version."""
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/python-validate.yml").write_text(
        _SETUP.format("3.14")
    )
    assert vs.problems(tmp_path) == []
    warns = vs.thin(tmp_path)
    assert len(warns) == 1 and "nothing to compare" in warns[0]
    assert "mypy.ini" in warns[0]


def test_the_thin_warning_names_only_files_a_consumer_carries(tmp_path) -> None:
    """A consumer's own workflows are callers and declare no version."""
    (tmp_path / "pyproject.toml").write_text('[tool.ruff]\ntarget-version = "py314"\n')
    warns = vs.thin(tmp_path)
    assert len(warns) == 1
    assert "mypy.ini" in warns[0]
    assert "python_validate" not in warns[0] and "python-validate" not in warns[0]


def test_the_checked_out_ci_repo_s_workflows_are_compared(tmp_path) -> None:
    """A consumer's floor is compared against the CI it actually runs."""
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[tool.ruff]\ntarget-version = "py314"\n')
    ci = tmp_path / ".ha-integration-ci/.github/workflows"
    ci.mkdir(parents=True)
    (ci / "python-validate.yml").write_text(_SETUP.format("3.15"))
    found = vs.problems(tmp_path)
    assert len(found) == 1
    assert "ha-integration-ci/python-validate.yml=3.15" in found[0]
    assert vs.thin(tmp_path) == []


def test_the_ci_repo_s_own_workflow_is_not_the_consumer_s_concern(tmp_path) -> None:
    """Only the three reusable workflows a consumer runs are read from the checkout."""
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[tool.ruff]\ntarget-version = "py314"\n')
    ci = tmp_path / ".ha-integration-ci/.github/workflows"
    ci.mkdir(parents=True)
    (ci / "python-validate.yml").write_text(_SETUP.format("3.14"))
    (ci / "ci.yml").write_text(_SETUP.format("3.12"))
    assert vs.problems(tmp_path) == []
    assert "ha-integration-ci/ci.yml" not in vs.collect(tmp_path)


def test_every_workflow_that_sets_up_python_is_compared(tmp_path) -> None:
    """A second workflow on a different Python is a disagreement, named by file."""
    _repo(tmp_path)
    (tmp_path / ".github/workflows/quality-audit.yml").write_text(_SETUP.format("3.12"))
    found = vs.problems(tmp_path)
    assert len(found) == 1
    assert "quality-audit.yml=3.12" in found[0]
    assert "python-validate.yml=3.14" in found[0]


def test_a_workflow_without_python_is_not_a_declaration(tmp_path) -> None:
    """A workflow that never sets up Python neither agrees nor disagrees."""
    _repo(tmp_path)
    (tmp_path / ".github/workflows/lint-pr.yml").write_text(
        "jobs:\n  lint:\n    steps:\n      - uses: amannn/action-semantic-pull-request@v6\n"
    )
    assert vs.problems(tmp_path) == []
    assert "lint-pr.yml" not in vs.collect(tmp_path)


def test_two_declarations_are_compared_not_warned(tmp_path) -> None:
    """Two values are enough to compare, so the thin warning must not fire."""
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/python-validate.yml").write_text(
        _SETUP.format("3.14")
    )
    (tmp_path / "mypy.ini").write_text(_mypy("3.13"))
    assert vs.thin(tmp_path) == []
    assert any("disagrees" in p for p in vs.problems(tmp_path))


def test_mypy_left_behind_is_caught(tmp_path) -> None:
    """The type checker's floor moves with the rest, or the gate checks the wrong Python."""
    found = vs.problems(_repo(tmp_path, mypy="3.13"))
    assert len(found) == 1
    assert "mypy.ini=3.13" in found[0]


def test_a_python_version_outside_the_mypy_section_is_not_read(tmp_path) -> None:
    """A per-module section never sets the global floor, so only [mypy] counts."""
    _repo(tmp_path)
    (tmp_path / "mypy.ini").write_text(
        "[mypy]\nstrict_equality = true\n\n[mypy-tests.*]\npython_version = 3.12\n"
    )
    assert vs.problems(tmp_path) == []
    assert vs.collect(tmp_path)["mypy.ini"] is None


def test_a_leftover_pyrightconfig_is_not_a_declaration(tmp_path) -> None:
    """Pyright is no longer the gate, so its config neither agrees nor disagrees."""
    _repo(tmp_path)
    (tmp_path / "pyrightconfig.json").write_text('{"pythonVersion": "3.12"}\n')
    assert vs.problems(tmp_path) == []
    assert "pyrightconfig.json" not in vs.collect(tmp_path)
