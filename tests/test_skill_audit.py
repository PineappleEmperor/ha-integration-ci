"""Unit tests for scripts/skill_audit.py."""

import importlib.util
import json
import pathlib
import subprocess

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
_SPEC = importlib.util.spec_from_file_location(
    "skill_audit", _SCRIPTS / "skill_audit.py"
)
audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(audit)

_SHA = "0" * 40
_CI = "PineappleEmperor/ha-integration-ci/.github/workflows"


@pytest.fixture
def repo(tmp_path):
    """A repo with the workflow directory present and nothing else."""
    (tmp_path / ".github/workflows").mkdir(parents=True)
    return tmp_path


def _wf(repo, name, body):
    (repo / ".github/workflows" / name).write_text(body)


def _pointer(target: str, job: str = "validate", on: str = "pull_request:") -> str:
    """A pointer workflow: one job whose `uses:` calls `target` at the placeholder SHA."""
    return f"on:\n  {on}\njobs:\n  {job}:\n    uses: {target}@{_SHA} # v1.0.0\n"


def test_missing_canonical_workflows_are_listed(repo) -> None:
    """Every absent canonical file is named, not just the first."""
    fails, _ = audit.check_canonical_files(audit.Repo(repo))
    assert any("pr-checks.yml" in f for f in fails)
    assert any("python-validate.yml" in f for f in fails)
    assert any("dependency-review.yml" in f for f in fails)
    assert any(".gitignore" in f for f in fails)


def test_a_panel_repo_must_carry_the_panel_workflow(repo) -> None:
    """`frontend/` without `panel-bundle.yml` leaves the panel's TypeScript unchecked."""
    (repo / "frontend").mkdir()
    (repo / "frontend/package.json").write_text("{}")
    fails, _ = audit.check_canonical_files(audit.Repo(repo))
    assert any("panel-bundle.yml" in f and "frontend/" in f for f in fails)


def test_a_repo_without_a_panel_is_not_asked_for_the_panel_workflow(repo) -> None:
    """On a repo with no `frontend/` the workflow's own first run fails; never demand it."""
    fails, _ = audit.check_canonical_files(audit.Repo(repo))
    assert not any("panel-bundle.yml" in f for f in fails)


def test_the_superseded_frontend_workflow_is_refused(repo) -> None:
    """`frontend_build.yml` gated merges on a build artefact; `panel-bundle.yml` replaced it."""
    (repo / ".github/workflows/frontend_build.yml").write_text("name: old\n")
    fails, _ = audit.check_canonical_files(audit.Repo(repo))
    assert any("frontend_build.yml" in f and "panel-bundle.yml" in f for f in fails)


def test_a_pointer_at_the_expected_workflow_passes(repo) -> None:
    """The healthy state: each pointer calls the workflow it stands for, by SHA."""
    _wf(repo, "python-validate.yml", _pointer(f"{_CI}/python-validate.yml"))
    _wf(repo, "quality-audit.yml", _pointer(f"{_CI}/quality-audit.yml", "audit"))
    _wf(repo, "release.yml", _pointer(f"{_CI}/release.yml", "release", "release:"))
    assert audit.check_pointers(audit.Repo(repo)) == ([], [])
    assert audit.check_action_pins(audit.Repo(repo)) == ([], [])


def test_a_pointer_at_the_wrong_repository_fails(repo) -> None:
    """A pointer at someone else's workflow runs someone else's CI under this name."""
    _wf(
        repo,
        "python-validate.yml",
        _pointer("someone/else/.github/workflows/python-validate.yml"),
    )
    fails, _ = audit.check_pointers(audit.Repo(repo))
    assert len(fails) == 1
    assert "someone/else" in fails[0] and f"{_CI}/python-validate.yml" in fails[0]


def test_a_pointer_at_the_wrong_path_fails(repo) -> None:
    """The right repository but another of its workflows is still the wrong check."""
    _wf(repo, "quality-audit.yml", _pointer(f"{_CI}/python-validate.yml", "audit"))
    fails, _ = audit.check_pointers(audit.Repo(repo))
    assert len(fails) == 1 and f"{_CI}/quality-audit.yml" in fails[0]


def test_a_pointer_carrying_a_body_fails(repo) -> None:
    """A pointer job carrying a body."""
    _wf(
        repo,
        "release.yml",
        "on:\n  release:\njobs:\n  build:\n    steps:\n      - run: zip -r out.zip .\n",
    )
    fails, _ = audit.check_pointers(audit.Repo(repo))
    assert len(fails) == 1 and "release.yml" in fails[0] and "body" in fails[0]


def test_a_pointer_is_held_to_the_pin_shape(repo) -> None:
    """A pointer's `uses:` is an action ref like any other: SHA plus a version comment."""
    _wf(
        repo,
        "python-validate.yml",
        f"jobs:\n  validate:\n    uses: {_CI}/python-validate.yml@v1\n",
    )
    assert audit.check_pointers(audit.Repo(repo)) == ([], [])
    fails, _ = audit.check_action_pins(audit.Repo(repo))
    assert len(fails) == 1 and "not pinned to a commit SHA" in fails[0]


def test_an_absent_pointer_is_the_canonical_check_s_concern(repo) -> None:
    """check_pointers judges the pointers a repo carries; absence is reported once, elsewhere."""
    assert audit.check_pointers(audit.Repo(repo)) == ([], [])


def test_a_reusable_workflow_body_is_a_definition_not_a_copy(repo) -> None:
    """The repository that owns a body carries it under `workflow_call` alone."""
    _wf(
        repo,
        "release.yml",
        "on:\n  workflow_call:\njobs:\n  build:\n    steps:\n      - run: zip -r out.zip .\n",
    )
    assert audit.check_pointers(audit.Repo(repo)) == ([], [])

    _wf(
        repo,
        "release.yml",
        "on:\n  workflow_call:\n  release:\njobs:\n  build:\n    steps:\n      - run: zip -r out.zip .\n",
    )
    fails, _ = audit.check_pointers(audit.Repo(repo))
    assert len(fails) == 1 and "body" in fails[0]


def test_every_release_flow_pointer_is_judged(repo) -> None:
    """The drafter and the draft opener are release-flow's too; a body in their place is a copy."""
    _rf = "PineappleEmperor/release-flow/.github/workflows"
    for name in ("release-drafter.yml", "auto-draft-pr.yml"):
        _wf(
            repo,
            name,
            "on:\n  push:\njobs:\n  x:\n    steps:\n      - run: gh pr create\n",
        )
    fails, _ = audit.check_pointers(audit.Repo(repo))
    assert len(fails) == 2
    assert any(f"{_rf}/release-drafter.yml" in f for f in fails)
    assert any(f"{_rf}/auto-draft-pr.yml" in f for f in fails)


def test_the_drafter_pointer_is_canonical(repo) -> None:
    """The release model depends on the drafter, so a repo without its pointer is incomplete."""
    fails, _ = audit.check_canonical_files(audit.Repo(repo))
    assert any("workflows/release-drafter.yml" in f for f in fails)


def test_bare_tag_pins_fail(repo) -> None:
    """A tag can be repointed at new code that runs with the workflow's token."""
    _wf(repo, "a.yml", "jobs:\n  x:\n    steps:\n      - uses: actions/checkout@v7\n")
    fails, _ = audit.check_action_pins(audit.Repo(repo))
    assert len(fails) == 1 and "not pinned to a commit SHA" in fails[0]


def test_sha_without_a_version_comment_fails(repo) -> None:
    """A 40-character hex string tells a reader nothing on its own."""
    _wf(
        repo,
        "a.yml",
        f"jobs:\n  x:\n    steps:\n      - uses: actions/checkout@{'a' * 40}\n",
    )
    fails, _ = audit.check_action_pins(audit.Repo(repo))
    assert len(fails) == 1 and "no version comment" in fails[0]


def test_documented_mutable_refs_are_exempt(repo) -> None:
    """HACS and hassfest each document a mutable ref; pinning stops tracking them."""
    _wf(
        repo,
        "a.yml",
        "jobs:\n  x:\n    steps:\n      - uses: hacs/action@main\n"
        "      - uses: home-assistant/actions/hassfest@master\n",
    )
    assert audit.check_action_pins(audit.Repo(repo)) == ([], [])


def test_two_release_body_writers_fail(repo) -> None:
    """Two writers race, and the loser's output is what users read."""
    _wf(
        repo,
        "a.yml",
        "jobs:\n  x:\n    steps:\n      - run: gh release edit v1 --notes-file n.md\n",
    )
    _wf(
        repo,
        "b.yml",
        "jobs:\n  y:\n    steps:\n      - uses: softprops/action-gh-release@v3\n"
        "        with:\n          generate_release_notes: true\n",
    )
    fails, _ = audit.check_single_body_writer(audit.Repo(repo))
    assert (
        len(fails) == 1
        and "more than one workflow step writes the release body" in fails[0]
    )


def test_a_zip_release_pointer_is_trusted_to_patch_the_manifest(repo) -> None:
    """The step that writes the tag into the manifest lives in the workflow pointed at."""
    (repo / "hacs.json").write_text('{"zip_release": true}')
    _wf(repo, "release.yml", _pointer(f"{_CI}/release.yml", "release", "release:"))
    assert audit.check_zip_release_patches_manifest(audit.Repo(repo)) == ([], [])


def test_a_zip_release_body_that_never_patches_the_manifest_fails(repo) -> None:
    """A repo still carrying a body is judged on what that body does."""
    (repo / "hacs.json").write_text('{"zip_release": true}')
    _wf(
        repo,
        "release.yml",
        "jobs:\n  build:\n    steps:\n      - run: zip -r out.zip custom_components\n",
    )
    fails, _ = audit.check_zip_release_patches_manifest(audit.Repo(repo))
    assert len(fails) == 1 and "manifest" in fails[0]


def test_v6_drafter_categories_fail(repo) -> None:
    """The v6 shape parses, matches nothing, and resolves every release as a patch."""
    (repo / ".github/release-drafter.yml").write_text(
        "categories:\n  - title: Features\n    semver-increment: minor\n    labels:\n      - feature\n"
    )
    fails, _ = audit.check_drafter_categories(audit.Repo(repo))
    assert len(fails) == 1 and "v6 top-level" in fails[0]


def test_when_shaped_categories_pass(repo) -> None:
    """The v7 `when:` shape is the one that matches."""
    (repo / ".github/release-drafter.yml").write_text(
        "categories:\n  - title: Features\n    semver-increment: minor\n    when:\n"
        "      labels:\n        - feature\n"
    )
    assert audit.check_drafter_categories(audit.Repo(repo)) == ([], [])


def test_pr_opener_must_be_draft_and_actor_gated(repo) -> None:
    """A PR opened with a shared token otherwise appears to be written by its owner."""
    _wf(
        repo,
        "auto-draft-pr.yml",
        "jobs:\n  draft:\n    steps:\n      - run: gh pr create --title x\n",
    )
    fails, _ = audit.check_pr_openers(audit.Repo(repo))
    assert any("gate on the actor" in f for f in fails)
    assert any("must open the PR as a draft" in f for f in fails)


def test_the_underscore_spelling_is_still_found(repo) -> None:
    """A repo mid-migration carries the old names; a check that knew one spelling judged nothing."""
    _wf(
        repo,
        "auto_draft_pr.yml",
        "jobs:\n  draft:\n    steps:\n      - run: gh pr create --title x\n",
    )
    fails, _ = audit.check_pr_openers(audit.Repo(repo))
    assert any("auto_draft_pr.yml must gate on the actor" in f for f in fails)


def test_an_opener_pointer_is_judged_where_its_body_lives(repo) -> None:
    """A pointer at the opener has no actor gate or --draft to read; its body is elsewhere."""
    _wf(
        repo,
        "auto-draft-pr.yml",
        _pointer(
            "PineappleEmperor/release-flow/.github/workflows/auto-draft-pr.yml",
            "draft",
            "push:",
        ),
    )
    assert audit.check_pr_openers(audit.Repo(repo)) == ([], [])


def test_multiline_docstrings_in_integration_code_fail(tmp_path) -> None:
    """Module docstrings are exempt; functions and classes are not."""
    cc = tmp_path / "custom_components/demo"
    cc.mkdir(parents=True)
    (cc / "__init__.py").write_text(
        '"""Module docstring.\n\nStill fine, multiple lines.\n"""\n\n\n'
        'def f():\n    """One line."""\n\n\n'
        'def g():\n    """First.\n\n    Second.\n    """\n'
    )
    fails, _ = audit.check_docstrings(audit.Repo(tmp_path))
    assert len(fails) == 1 and "g" in fails[0]


def test_done_rules_without_tests_fail(tmp_path) -> None:
    """A `done` with no test is a claim, not evidence."""
    cc = tmp_path / "custom_components/demo"
    cc.mkdir(parents=True)
    (cc / "quality_scale.yaml").write_text(
        "rules:\n  config-flow: done\n  diagnostics: todo\n"
    )
    fails, _ = audit.check_claims_have_tests(audit.Repo(tmp_path))
    assert any("no tests/ directory" in f for f in fails)


def _tested_integration(tmp_path) -> pathlib.Path:
    """An integration with a test suite and every file the pytest path expects."""
    cc = tmp_path / "custom_components/demo"
    cc.mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "requirements.test.txt").write_text(
        "pytest-homeassistant-custom-component==0.13.354\n"
    )
    (tmp_path / "conftest.py").write_text(
        "import custom_components\n\ndef enable_custom_integrations(): ...\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n'
    )
    return tmp_path


def test_a_pytest_pointer_proves_the_suite_runs(tmp_path) -> None:
    """The pytest step lives in python-validate.yml here; the pointer at it is the proof."""
    root = _tested_integration(tmp_path)
    _wf(root, "python-validate.yml", _pointer(f"{_CI}/python-validate.yml"))
    assert audit.check_claims_have_tests(audit.Repo(root)) == ([], [])


def test_a_validate_body_without_pytest_fails(tmp_path) -> None:
    """A repo carrying its own body must run pytest in it, as before."""
    root = _tested_integration(tmp_path)
    _wf(
        root,
        "python-validate.yml",
        "jobs:\n  lint:\n    steps:\n      - run: ruff check .\n",
    )
    fails, _ = audit.check_claims_have_tests(audit.Repo(root))
    assert len(fails) == 1 and "pytest" in fails[0]


def test_hook_without_the_subject_guards_fails(tmp_path) -> None:
    """A hook that only measures length lets a well-formed empty subject through."""
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    hook = hooks / "commit-msg"
    hook.write_text("#!/usr/bin/env bash\n[ ${#1} -gt 72 ] && exit 1\nexit 0\n")
    hook.chmod(0o755)

    fails, _ = audit.check_commit_hook(audit.Repo(tmp_path))
    assert any("Conventional Commit subject shape" in f for f in fails)
    assert any("editorialising" in f for f in fails)

    hook.write_text(
        "case x in feat|fix|docs) ;; esac\n# editorialising subjects rejected\n"
    )
    hook.chmod(0o755)
    fails, _ = audit.check_commit_hook(audit.Repo(tmp_path))
    assert fails == []


def test_list_mode_names_every_check(capsys) -> None:
    """The skill points readers at --list instead of enumerating rules that go stale."""
    assert audit.main(["--list"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == len(audit.CHECKS)
    assert all(line.split()[0] for line in out)


def test_the_copy_model_checks_are_gone() -> None:
    """Nothing is copied any more, so nothing compares a copy; the checks went with them."""
    gone = {
        "check_self_diff",
        "check_template_scripts_match",
        "check_template_pins",
        "check_scripts_present",
        "check_scripts_wired",
        "_template_dir",
    }
    assert not gone & set(dir(audit))
    assert "check_pointers" in {c.__name__ for c in audit.CHECKS}


def test_a_third_pr_opener_is_still_refused(repo) -> None:
    """The sanctioned list is short on purpose: a workflow opening a PR acts as an author."""
    _wf(
        repo,
        "helpful.yml",
        "jobs:\n  x:\n    steps:\n      - run: gh pr create --title hi\n",
    )
    fails, _ = audit.check_pr_openers(audit.Repo(repo))
    assert any("helpful.yml opens PRs" in f for f in fails)


def test_a_marked_opener_states_its_own_reason(repo) -> None:
    """A repo with a different delivery model declares its exception in its own file."""
    _wf(
        repo,
        "sync_plugin_version.yml",
        "# skill-audit: sanctioned-opener — the version lives in a committed file\n"
        "jobs:\n  x:\n    steps:\n      - run: gh pr create --title v\n",
    )
    fails, _ = audit.check_pr_openers(audit.Repo(repo))
    assert fails == []


def test_unverifiable_checks_warn_rather_than_passing(repo, monkeypatch) -> None:
    """A check that cannot run must say NOT CHECKED, not stay silent."""

    class _Missing:
        def __call__(self, *a, **k):
            raise OSError("gh not found")

    monkeypatch.setattr(audit.subprocess, "run", _Missing())

    _, warns = audit.check_required_status_checks(audit.Repo(repo))
    assert any("NOT CHECKED" in w for w in warns)

    _wf(repo, "dependency-review.yml", "jobs:\n  review:\n    steps: []\n")
    _, warns = audit.check_dependency_graph(audit.Repo(repo))
    assert any("NOT CHECKED" in w for w in warns)


def test_dependency_graph_off_is_a_failure(repo, monkeypatch) -> None:
    """Seven workflows green and Dependency review red alone — the observed failure."""
    _wf(repo, "dependency-review.yml", "jobs:\n  review:\n    steps: []\n")

    class _Fake:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode = out, rc

    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        if "repo" in cmd and "view" in cmd:
            return _Fake("owner/repo\n")
        return _Fake("", 1)  # sbom probe fails: graph disabled

    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    fails, _ = audit.check_dependency_graph(audit.Repo(repo))
    assert any("dependency graph is off" in f for f in fails)


def test_no_dependency_review_workflow_means_nothing_to_check(
    repo, monkeypatch
) -> None:
    """A repo that does not ship the workflow has no prerequisite to satisfy."""
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not query")),
    )
    assert audit.check_dependency_graph(audit.Repo(repo)) == ([], [])


def test_an_ignored_hacs_check_fails(repo) -> None:
    """`ignore:` on the HACS action disqualifies the repo from the default store."""
    pkg = repo / "custom_components/acmedev"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json").write_text('{"domain": "acmedev"}')
    _wf(
        repo,
        "hacs-validate.yml",
        "jobs:\n  hacs:\n    steps:\n      - uses: hacs/action@main\n"
        "        with:\n          ignore: brands\n",
    )
    fails, _ = audit.check_no_ignored_validations(audit.Repo(repo))
    assert len(fails) == 1 and "hacs-validate.yml" in fails[0]


def test_a_drafter_pointer_is_judged_on_its_triggers_only(repo) -> None:
    """The drafter's wiring lives in release-flow; the pointer owns just the triggers."""
    target = "PineappleEmperor/release-flow/.github/workflows/release-drafter.yml"
    _wf(
        repo,
        "release-drafter.yml",
        f"on:\n  push:\n    branches: [main]\n  release:\n    types: [published]\n"
        f"jobs:\n  draft:\n    uses: {target}@{_SHA} # v1.0.0\n",
    )
    assert audit.check_release_drafter_wiring(audit.Repo(repo)) == ([], [])
    assert audit.check_previous_tag(audit.Repo(repo)) == ([], [])
    assert audit.check_sole_labeler(audit.Repo(repo)) == ([], [])


def test_a_drafter_pointer_on_pull_request_is_a_second_labeler(repo) -> None:
    """The trigger set is the pointer's own, so the sole-labeler rule still bites there."""
    target = "PineappleEmperor/release-flow/.github/workflows/release-drafter.yml"
    _wf(
        repo,
        "release-drafter.yml",
        f"on:\n  pull_request:\njobs:\n  draft:\n    uses: {target}@{_SHA} # v1.0.0\n",
    )
    fails, _ = audit.check_sole_labeler(audit.Repo(repo))
    assert len(fails) == 1 and "pull_request" in fails[0]


def test_platforms_naming_a_missing_module_fails(repo) -> None:
    """The live defect: PLATFORMS = ["sensor"] with no sensor.py, inert until forwarded."""
    pkg = repo / "custom_components/acmedev"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json").write_text('{"domain": "acmedev"}')
    (pkg / "const.py").write_text(
        'DOMAIN = "acmedev"\nPLATFORMS = ["sensor", "notify"]\n'
    )
    (pkg / "notify.py").write_text("")
    fails, _ = audit.check_platforms_have_modules(audit.Repo(repo))
    assert len(fails) == 1 and "sensor" in fails[0] and "notify" not in fails[0]


def test_platform_enum_form_is_understood(repo) -> None:
    """Both spellings appear in real integrations."""
    pkg = repo / "custom_components/acmedev"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json").write_text('{"domain": "acmedev"}')
    (pkg / "const.py").write_text("PLATFORMS = [Platform.SENSOR, Platform.NOTIFY]\n")
    (pkg / "sensor.py").write_text("")
    fails, _ = audit.check_platforms_have_modules(audit.Repo(repo))
    assert len(fails) == 1 and "notify" in fails[0]


def test_matching_platforms_pass(repo) -> None:
    """A module beside every PLATFORMS entry is the wired state."""
    pkg = repo / "custom_components/acmedev"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json").write_text('{"domain": "acmedev"}')
    (pkg / "const.py").write_text('PLATFORMS = ["notify"]\n')
    (pkg / "notify.py").write_text("")
    assert audit.check_platforms_have_modules(audit.Repo(repo)) == ([], [])


def _ruleset(repo, *contexts) -> None:
    (repo / "ruleset.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "required_status_checks": [{"context": c} for c in contexts]
                        },
                    }
                ]
            }
        )
    )


def test_a_required_context_no_job_produces_fails(repo) -> None:
    """The observed defect, twice: a ruleset requiring a check nothing reports."""
    _wf(
        repo,
        "pr-checks.yml",
        "jobs:\n  label:\n    name: CC labelling\n    steps: []\n",
    )
    _ruleset(repo, "CC labelling", "Version validation")
    fails, _ = audit.check_required_contexts_have_producers(audit.Repo(repo))
    assert len(fails) == 1 and "Version validation" in fails[0]


def test_every_required_context_produced_passes(repo) -> None:
    """A ruleset whose every context has a producing job is the healthy state."""
    _wf(
        repo,
        "pr-checks.yml",
        "jobs:\n  label:\n    name: CC labelling\n    steps: []\n",
    )
    _ruleset(repo, "CC labelling")
    assert audit.check_required_contexts_have_producers(audit.Repo(repo)) == ([], [])


def test_a_job_without_a_name_is_known_by_its_id(repo) -> None:
    """GitHub names the check-run for the job id when the job declares no name."""
    _wf(repo, "a.yml", "jobs:\n  review:\n    steps: []\n")
    _ruleset(repo, "review")
    assert audit.check_required_contexts_have_producers(audit.Repo(repo)) == ([], [])


def test_a_pointer_job_produces_the_prefixed_context(repo) -> None:
    """A job calling a reusable workflow."""
    _wf(repo, "python-validate.yml", _pointer(f"{_CI}/python-validate.yml"))
    _ruleset(repo, "validate / Ruff, Pyright and Pytest")
    assert audit.check_required_contexts_have_producers(audit.Repo(repo)) == ([], [])


def test_a_pointer_job_never_reports_under_its_bare_id(repo) -> None:
    """A ruleset naming the pointer job alone waits on a check-run GitHub never creates."""
    _wf(repo, "python-validate.yml", _pointer(f"{_CI}/python-validate.yml"))
    _ruleset(repo, "validate")
    fails, _ = audit.check_required_contexts_have_producers(audit.Repo(repo))
    assert len(fails) == 1 and "'validate'" in fails[0]


def test_live_required_contexts_warn_when_gh_is_missing(repo, monkeypatch) -> None:
    """Unverifiable must say NOT CHECKED; a silent pass is how this survived before."""

    class _Missing:
        def __call__(self, *a, **k):
            raise OSError("gh not found")

    monkeypatch.setattr(audit.subprocess, "run", _Missing())
    _, warns = audit.check_live_required_contexts(audit.Repo(repo))
    assert any("NOT CHECKED" in w for w in warns)


def test_live_ruleset_orphan_fails(repo, monkeypatch) -> None:
    """A repo protected from the GitHub UI has no ruleset.json to compare against."""
    _wf(
        repo,
        "pr-checks.yml",
        "jobs:\n  label:\n    name: CC labelling\n    steps: []\n",
    )

    class _Fake:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode = out, rc

    def fake_run(cmd, **k):
        if cmd[0] == "git":
            return _Fake(
                "", 128
            )  # no clone to consult: the working tree is the verdict
        if "view" in cmd:
            return _Fake("owner/repo\n")
        if cmd[-1] == ".default_branch":
            return _Fake("main\n")
        return _Fake('["CC labelling","Version validation"]')

    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    fails, _ = audit.check_live_required_contexts(audit.Repo(repo))
    assert (
        len(fails) == 1
        and "Version validation" in fails[0]
        and "working tree" in fails[0]
    )


def test_live_check_knows_a_pointer_by_its_prefix(repo, monkeypatch) -> None:
    """The live ruleset names `audit / ha-integration conformance check`; the pointer produces it."""
    _wf(repo, "quality-audit.yml", _pointer(f"{_CI}/quality-audit.yml", "audit"))

    class _Fake:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode = out, rc

    def fake_run(cmd, **k):
        if cmd[0] == "git":
            return _Fake("", 128)
        if "view" in cmd:
            return _Fake("owner/repo\n")
        if cmd[-1] == ".default_branch":
            return _Fake("main\n")
        return _Fake('["audit / ha-integration conformance check"]')

    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    assert audit.check_live_required_contexts(audit.Repo(repo)) == ([], [])


def test_a_placeholder_left_in_a_copied_workflow_fails(repo) -> None:
    """A workflow that never had its `<domain>` substituted dies on a bash redirect."""
    _wf(
        repo,
        "release.yml",
        "jobs:\n  build:\n    steps:\n      - run: gh release upload v1 <domain>.zip\n",
    )
    fails, _ = audit.check_no_placeholders(audit.Repo(repo))
    assert len(fails) == 1 and "<domain>" in fails[0]


def test_a_placeholder_in_a_comment_is_documentation(repo) -> None:
    """A comment saying what `<domain>` means is never seen by bash."""
    _wf(
        repo,
        "release.yml",
        "# zips custom_components/<domain>\njobs:\n  build:\n    steps:\n      - run: |\n"
        "          # the package is custom_components/<domain>\n          zip -r out.zip .\n",
    )
    assert audit.check_no_placeholders(audit.Repo(repo)) == ([], [])


def test_a_placeholder_in_a_with_value_fails(repo) -> None:
    """An action input is not shell, but a placeholder there is just as unsubstituted."""
    _wf(
        repo,
        "a.yml",
        "jobs:\n  x:\n    steps:\n      - uses: actions/setup-node@abc # v1\n"
        "        with:\n          cache-dependency-path: <domain>/package-lock.json\n",
    )
    fails, _ = audit.check_no_placeholders(audit.Repo(repo))
    assert len(fails) == 1 and "<domain>" in fails[0]


def test_python_run_without_a_setup_step_fails(repo) -> None:
    """A step that runs Python before any setup-python step runs on the runner's own."""
    _wf(
        repo,
        "a.yml",
        "jobs:\n  x:\n    steps:\n      - uses: actions/checkout@abc # v1\n"
        "      - name: Gate\n        run: python3 scripts/manifest_gate.py --suggest\n",
    )
    fails, _ = audit.check_python_steps_have_a_setup(audit.Repo(repo))
    assert len(fails) == 1
    assert "a.yml" in fails[0] and "'Gate'" in fails[0] and "setup-python" in fails[0]


def test_python_run_after_a_setup_step_passes(repo) -> None:
    """The ordinary shape: setup-python, then the script."""
    _wf(
        repo,
        "a.yml",
        "jobs:\n  x:\n    steps:\n      - uses: actions/checkout@abc # v1\n"
        "      - uses: actions/setup-python@def # v7\n        with:\n"
        "          python-version: '3.14'\n"
        "      - run: |\n          python3 - <<'PY'\n          print(1)\n          PY\n",
    )
    assert audit.check_python_steps_have_a_setup(audit.Repo(repo)) == ([], [])


def test_a_setup_step_in_another_job_does_not_count(repo) -> None:
    """Jobs run on separate runners; a setup in one job leaves the other on the default."""
    _wf(
        repo,
        "a.yml",
        "jobs:\n  x:\n    steps:\n      - uses: actions/setup-python@def # v7\n"
        "  y:\n    steps:\n      - run: python -m pytest\n",
    )
    fails, _ = audit.check_python_steps_have_a_setup(audit.Repo(repo))
    assert len(fails) == 1 and "job 'y'" in fails[0]


def _pr_checks(ref: str) -> str:
    """A pr-checks.yml carrying every string the shape check requires, checking out `ref`."""
    return (
        "on:\n  pull_request_target:\n    types: [opened]\n"
        "jobs:\n  label:\n    steps:\n      - run: echo 'Remove superseded'\n"
        "  title-check:\n    needs: label\n    if: github.event.pull_request.user.type != 'Bot'\n"
        "    steps:\n      - uses: actions/checkout@abc # v1\n"
        f"        with:\n          ref: ${{{{ github.event.pull_request.{ref} }}}}\n"
        "      - run: python3 scripts/commit_summary.py --mode label\n"
    )


def test_title_check_pinned_to_the_base_sha_fails(repo) -> None:
    """base.sha is frozen at PR creation while the workflow runs from the base branch head."""
    _wf(repo, "pr-checks.yml", _pr_checks("base.sha"))
    fails, _ = audit.check_pr_checks_shape(audit.Repo(repo))
    assert len(fails) == 1 and "base.ref" in fails[0] and "frozen" in fails[0]


def test_title_check_on_the_base_ref_passes(repo) -> None:
    """base.ref is the base branch head, the same ref the workflow itself is loaded from."""
    _wf(repo, "pr-checks.yml", _pr_checks("base.ref"))
    assert audit.check_pr_checks_shape(audit.Repo(repo)) == ([], [])


def test_a_pr_checks_pointer_owns_only_its_trigger(repo) -> None:
    """The label and title-check jobs live in release-flow; the pointer owns the event."""
    target = "PineappleEmperor/release-flow/.github/workflows/pr-checks.yml"
    _wf(repo, "pr-checks.yml", _pointer(target, "checks", "pull_request_target:"))
    assert audit.check_pr_checks_shape(audit.Repo(repo)) == ([], [])

    _wf(repo, "pr-checks.yml", _pointer(target, "checks", "pull_request:"))
    fails, _ = audit.check_pr_checks_shape(audit.Repo(repo))
    assert len(fails) == 1 and "pull_request_target" in fails[0]


def _cloned_repo(tmp_path, workflow: str) -> pathlib.Path:
    """A working clone whose origin/main carries `workflow` as pr-checks.yml."""
    src = tmp_path / "src"
    (src / ".github/workflows").mkdir(parents=True)
    (src / ".github/workflows/pr-checks.yml").write_text(workflow)
    git = [
        "git",
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=src, check=True)
    subprocess.run([*git, "add", "."], cwd=src, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "chore: init"], cwd=src, check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(src), str(work)], check=True)
    return work


def test_a_job_the_base_branch_defines_is_not_an_orphan(tmp_path) -> None:
    """The misreading that cost a working gate."""
    work = _cloned_repo(
        tmp_path, "jobs:\n  gate:\n    name: Version validation\n    steps: []\n"
    )
    (work / ".github/workflows/pr-checks.yml").write_text(
        "jobs:\n  label:\n    name: CC labelling\n    steps: []\n"
    )
    _ruleset(work, "CC labelling", "Version validation")
    assert audit.check_required_contexts_have_producers(audit.Repo(work)) == ([], [])


def test_a_pointer_the_base_branch_defines_is_not_an_orphan(tmp_path) -> None:
    """The base branch's pointer jobs count by prefix, the same as the working tree's."""
    work = _cloned_repo(
        tmp_path,
        _pointer(
            "PineappleEmperor/release-flow/.github/workflows/pr-checks.yml",
            "checks",
            "pull_request_target:",
        ),
    )
    (work / ".github/workflows/pr-checks.yml").write_text(
        "jobs:\n  x:\n    steps: []\n"
    )
    _ruleset(work, "checks / CC labelling", "checks / CC label validation")
    assert audit.check_required_contexts_have_producers(audit.Repo(work)) == ([], [])


def test_an_orphan_report_names_the_refs_it_judged(tmp_path) -> None:
    """A verdict without its evidence is what got misread; say which ref was consulted."""
    work = _cloned_repo(
        tmp_path, "jobs:\n  label:\n    name: CC labelling\n    steps: []\n"
    )
    _ruleset(work, "CC labelling", "Version validation")
    fails, _ = audit.check_required_contexts_have_producers(audit.Repo(work))
    assert (
        len(fails) == 1
        and "Version validation" in fails[0]
        and "origin/main" in fails[0]
    )


def test_without_a_remote_the_verdict_says_working_tree(repo) -> None:
    """No base branch to consult is a weaker verdict, and it must say so."""
    _wf(
        repo,
        "pr-checks.yml",
        "jobs:\n  label:\n    name: CC labelling\n    steps: []\n",
    )
    _ruleset(repo, "CC labelling", "Version validation")
    fails, _ = audit.check_required_contexts_have_producers(audit.Repo(repo))
    assert len(fails) == 1 and "working tree" in fails[0]


def test_live_check_counts_jobs_the_base_branch_defines(tmp_path, monkeypatch) -> None:
    """The live ruleset is judged the same way: producers on the base branch count."""
    work = _cloned_repo(
        tmp_path, "jobs:\n  gate:\n    name: Version validation\n    steps: []\n"
    )
    (work / ".github/workflows/pr-checks.yml").write_text(
        "jobs:\n  label:\n    name: CC labelling\n    steps: []\n"
    )
    real_run = audit.subprocess.run

    class _Fake:
        def __init__(self, out, rc=0):
            self.stdout, self.returncode = out, rc

    def fake_run(cmd, **k):
        if cmd[0] == "git":
            return real_run(cmd, **k)
        if "view" in cmd:
            return _Fake("owner/repo\n")
        if cmd[-1] == ".default_branch":
            return _Fake("main\n")
        return _Fake('["CC labelling","Version validation"]')

    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    assert audit.check_live_required_contexts(audit.Repo(work)) == ([], [])


def test_the_future_import_is_not_demanded(repo) -> None:
    """Python 3.14 defers annotations itself; the consumer's ruff config bans the import."""
    pkg = repo / "custom_components/acmedev"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json").write_text('{"domain": "acmedev"}')
    (pkg / "__init__.py").write_text('DOMAIN = "acmedev"\n')
    assert audit.check_antipatterns(audit.Repo(repo)) == ([], [])


def _integration(repo, **files: str) -> pathlib.Path:
    """A minimal integration package, plus any extra files by name."""
    pkg = repo / "custom_components/acmedev"
    pkg.mkdir(parents=True)
    (pkg / "manifest.json").write_text('{"domain": "acmedev"}')
    for name, text in files.items():
        (pkg / name).write_text(text)
    return pkg


def _fake_gh(answers: dict[str, str]):
    """A subprocess.run stand-in answering `gh` by the first matching argument."""

    class _Done:
        def __init__(self, out: str, rc: int = 0) -> None:
            self.stdout, self.returncode = out, rc

    def run(cmd, **k):
        for key, out in answers.items():
            if key in cmd:
                return _Done(out)
        return _Done("", 1)

    return run


def test_a_tracked_compiled_artefact_fails(tmp_path) -> None:
    """A committed .pyc ships inside every release zip."""
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "x.pyc").write_bytes(b"\x00")
    subprocess.run([*git, "add", "x.pyc"], cwd=tmp_path, check=True)
    fails, _ = audit.check_no_tracked_artefacts(audit.Repo(tmp_path))
    assert len(fails) == 1 and "x.pyc" in fails[0]


def test_label_events_with_cancellation_fail(repo) -> None:
    """Dependabot's three labels at once started five runs; the cancelled ones read as red."""
    _wf(
        repo,
        "pr-checks.yml",
        "on:\n  pull_request_target:\n    types: [opened, labeled]\n"
        "concurrency:\n  group: x\n  cancel-in-progress: true\njobs: {}\n",
    )
    fails, _ = audit.check_label_events(audit.Repo(repo))
    assert len(fails) == 1 and "labeled" in fails[0]


def test_an_inlined_classifier_fails(repo) -> None:
    """A classifier in a heredoc cannot be unit-tested; the script can."""
    _wf(
        repo,
        "pr-checks.yml",
        "jobs:\n  x:\n    steps:\n      - run: |\n          MAINT = 1\n",
    )
    fails, _ = audit.check_classifier_not_inlined(audit.Repo(repo))
    assert len(fails) == 1 and "commit_summary.py" in fails[0]


def test_an_expression_inside_a_run_block_fails(repo) -> None:
    """An expression inside a run: block."""
    _wf(
        repo,
        "a.yml",
        "jobs:\n  x:\n    steps:\n      - name: Say\n"
        "        run: echo ${{ github.event.pull_request.title }}\n",
    )
    fails, _ = audit.check_no_run_interpolation(audit.Repo(repo))
    assert len(fails) == 1
    assert "github.event.pull_request.title" in fails[0] and "'Say'" in fails[0]


def test_a_dishonest_manifest_fails_on_each_claim(repo) -> None:
    """The claims a user reads before installing: each missing one is named."""
    _integration(repo)
    (repo / "custom_components/acmedev/manifest.json").write_text(
        '{"domain": "acmedev", "config_flow": true}'
    )
    fails, _ = audit.check_quality_scale_and_manifest(audit.Repo(repo))
    assert any("missing quality_scale.yaml" in f for f in fails)
    assert any("integration_type" in f for f in fails)
    assert any("issue_tracker" in f for f in fails)
    assert any("config_flow.py is missing" in f for f in fails)
    assert any("missing CLAUDE.md" in f for f in fails)
    assert any("missing README.md" in f for f in fails)


def test_a_branch_rule_in_the_autolabeler_fails(repo) -> None:
    """A branch rule flaps whenever the branch name disagrees with the commits."""
    (repo / ".github/release-drafter.yml").write_text(
        "autolabeler:\n  - label: fix\n    branch:\n      - '/fix\\//'\n"
    )
    fails, _ = audit.check_autolabeler_title_only(audit.Repo(repo))
    assert len(fails) == 1 and "fix" in fails[0]


def _png(width: int, height: int) -> bytes:
    """Just enough of a PNG for the size to be read from its IHDR chunk."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + width.to_bytes(4) + height.to_bytes(4)
    )


def test_missing_and_mis_sized_brand_assets_fail(repo) -> None:
    """A present icon.png with no @2x is the 'icon shows only sometimes' bug."""
    _integration(repo)
    fails, _ = audit.check_brand_assets(audit.Repo(repo))
    assert len(fails) == 1 and "brand/" in fails[0]

    brand = repo / "custom_components/acmedev/brand"
    brand.mkdir()
    (brand / "icon.png").write_bytes(_png(384, 384))
    fails, _ = audit.check_brand_assets(audit.Repo(repo))
    assert len(fails) == 1
    assert "icon.png is (384, 384)" in fails[0] and "missing" in fails[0]
    assert "icon@2x.png" in fails[0] and "logo@2x.png" in fails[0]


def test_an_opener_with_no_token_fails(repo, monkeypatch) -> None:
    """A PR opened with the default token fires no checks and is unmergeable forever."""
    _wf(repo, "auto-draft-pr.yml", "jobs:\n  draft:\n    steps: []\n")
    monkeypatch.setattr(audit.subprocess, "run", _fake_gh({"secret": "OTHER\n"}))
    fails, _ = audit.check_release_token(audit.Repo(repo))
    assert len(fails) == 1 and "RELEASE_TOKEN" in fails[0]

    monkeypatch.setattr(
        audit.subprocess, "run", _fake_gh({"secret": "APP_ID\nAPP_PRIVATE_KEY\n"})
    )
    assert audit.check_release_token(audit.Repo(repo)) == ([], [])


def test_a_drafter_body_reading_the_current_release_as_previous_fails(repo) -> None:
    """`--limit 1` on a release event returns the release being written; notes come out empty."""
    _wf(
        repo,
        "release-drafter.yml",
        "on:\n  release:\n    types: [published]\njobs:\n  notes:\n    steps:\n"
        "      - run: |\n          PREV=$(gh release list --limit 1 --json tagName)\n"
        '          python3 scripts/release_notes.py --since "$PREV"\n',
    )
    fails, _ = audit.check_previous_tag(audit.Repo(repo))
    assert len(fails) == 1 and "--limit 1" in fails[0]


def test_a_drafter_body_missing_its_wiring_fails_on_each_piece(repo) -> None:
    """A body that never runs the generator, its checker, or clones deep enough."""
    _wf(
        repo,
        "release-drafter.yml",
        "on:\n  push:\njobs:\n  draft:\n    steps:\n      - uses: actions/checkout@abc # v1\n",
    )
    fails, _ = audit.check_release_drafter_wiring(audit.Repo(repo))
    assert len(fails) == 3
    assert any("release_notes.py" in f for f in fails)
    assert any("check_release_notes.py" in f for f in fails)
    assert any("fetch-depth: 0" in f for f in fails)


def test_a_deprecated_api_and_a_bare_ignore_fail(repo) -> None:
    """Deprecated APIs still import cleanly and fail at runtime."""
    _integration(
        repo,
        **{
            "notify.py": "class S(BaseNotificationService):\n    x = 1  # type: ignore\n"
        },
    )
    fails, _ = audit.check_antipatterns(audit.Repo(repo))
    assert len(fails) == 2
    assert any("BaseNotificationService" in f for f in fails)
    assert any("bare # type: ignore" in f and "notify.py" in f for f in fails)


def test_a_default_branch_with_no_required_checks_fails(repo, monkeypatch) -> None:
    """Every workflow is advisory until the default branch requires it."""
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        _fake_gh(
            {
                "view": "owner/repo\n",
                ".default_branch": "main\n",
                "[.[].type]": '["non_fast_forward"]\n',
            }
        ),
    )
    fails, warns = audit.check_required_status_checks(audit.Repo(repo))
    assert len(fails) == 1 and "no required status checks on main" in fails[0]
    assert warns == []
