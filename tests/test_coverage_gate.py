"""Unit tests for scripts/coverage_gate.py."""

import importlib.util
import json
import pathlib

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "coverage_gate", _SCRIPTS / "coverage_gate.py"
)
gate = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(gate)


def _repo(tmp_path, *modules: str) -> pathlib.Path:
    """An integration package holding the named modules."""
    pkg = tmp_path / "custom_components/acme"
    pkg.mkdir(parents=True)
    for name in modules:
        (pkg / name).write_text("")
    return tmp_path


def _report(**missing: list[int]) -> dict:
    """A coverage.py JSON report; each keyword is a module and its missed lines."""
    return {
        "files": {
            f"custom_components/acme/{name}.py": {"missing_lines": lines}
            for name, lines in missing.items()
        }
    }


def test_fully_covered_modules_pass(tmp_path) -> None:
    """Every line of the flow and of diagnostics ran: nothing to report."""
    root = _repo(tmp_path, "config_flow.py", "diagnostics.py")
    assert gate.problems(root, _report(config_flow=[], diagnostics=[])) == []


def test_a_missed_line_fails_and_names_it(tmp_path) -> None:
    """Core's bar is every line, so one miss is a failure that says where."""
    root = _repo(tmp_path, "config_flow.py")
    found = gate.problems(root, _report(config_flow=[42, 57]))
    assert len(found) == 1
    assert "custom_components/acme/config_flow.py" in found[0]
    assert "42, 57" in found[0]


def test_a_module_no_test_imported_fails(tmp_path) -> None:
    """A module missing from the report never ran at all."""
    root = _repo(tmp_path, "diagnostics.py")
    found = gate.problems(root, _report())
    assert len(found) == 1 and "diagnostics.py" in found[0] and "never" in found[0]


@pytest.mark.parametrize(
    "module",
    [
        "backup.py",
        "config_flow.py",
        "device_action.py",
        "device_condition.py",
        "device_trigger.py",
        "diagnostics.py",
        "group.py",
        "intent.py",
        "logbook.py",
        "media_source.py",
        "recorder.py",
        "scene.py",
    ],
)
def test_each_module_kind_cores_codecov_names_is_held(tmp_path, module) -> None:
    """Core's codecov.yml holds these twelve kinds to 100%; a miss in any one fails."""
    root = _repo(tmp_path, module)
    found = gate.problems(root, _report(**{module.removesuffix(".py"): [7]}))
    assert len(found) == 1 and module in found[0]


def test_other_modules_are_not_held_to_the_bar(tmp_path) -> None:
    """Core holds only these modules to 100%; the rest are not this gate's concern."""
    root = _repo(tmp_path, "sensor.py")
    assert gate.problems(root, _report(sensor=[1, 2, 3])) == []


def test_main_reads_the_report_and_exits_on_a_miss(tmp_path, capsys) -> None:
    """The workflow runs the script on the report pytest-cov wrote."""
    root = _repo(tmp_path, "config_flow.py")
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(_report(config_flow=[3])))
    assert gate.main(["--root", str(root), "--report", str(report)]) == 1
    assert "config_flow.py" in capsys.readouterr().out

    report.write_text(json.dumps(_report(config_flow=[])))
    assert gate.main(["--root", str(root), "--report", str(report)]) == 0
