"""Unit tests for scripts/version_sync.py.

Load the standalone script by path; it is not an importable package.
"""

import importlib.util
import json
import pathlib

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


def _repo(tmp_path, *, workflow="3.14", ruff="py314", pyright="3.14", pin=True):
    """A repo declaring the python version in each of the places that carry it."""
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/python-validate.yml").write_text(
        _SETUP.format(workflow)
    )
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.ruff]\ntarget-version = "{ruff}"\n'
    )
    (tmp_path / "pyrightconfig.json").write_text(json.dumps({"pythonVersion": pyright}))
    (tmp_path / "requirements.test.txt").write_text(
        "pytest-homeassistant-custom-component==0.13.354\n"
        if pin
        else "pytest-homeassistant-custom-component\n"
    )
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
    found = vs.problems(_repo(tmp_path, pin=False))
    assert any("does not pin pytest-homeassistant-custom-component" in f for f in found)


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
    assert "pyrightconfig.json" in warns[0]


def test_the_thin_warning_names_only_files_a_consumer_carries(tmp_path) -> None:
    """A pointer-model consumer has no workflow that declares a version."""
    (tmp_path / "pyproject.toml").write_text('[tool.ruff]\ntarget-version = "py314"\n')
    warns = vs.thin(tmp_path)
    assert len(warns) == 1
    assert "pyrightconfig.json" in warns[0]
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
    (tmp_path / "pyrightconfig.json").write_text('{"pythonVersion": "3.13"}\n')
    assert vs.thin(tmp_path) == []
    assert any("disagrees" in p for p in vs.problems(tmp_path))
