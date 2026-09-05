#!/usr/bin/env python3
"""Skill-conformance audit: verify the ha-integration skill was actually followed.

Pointer workflows present and aimed at the reusable workflow each stands for, action
pins current, antipatterns absent, quality_scale honest. The mechanical subset of the
audit; the judgement items in the skill's reference/audit.md still need an agent with the
skill on disk. Exit 1 on any FAIL. Runs locally and in CI, where quality-audit.yml checks
this repository out beside the consumer's tree and runs it with `--root .`.

README.md says what it checks and what it no longer does.
"""

import argparse
import ast
import json
import os
import pathlib
import re
import struct
import subprocess
import sys

import yaml

Result = tuple[list[str], list[str]]  # (failures, warnings)

# Required in every repo built from this skill. All produce a context the consumer's
# ruleset requires, and a repo missing one leaves the ruleset waiting forever on a check
# that never reports. The integration-only workflows below produce required contexts too;
# they are separated because a non-integration repo has no manifest.
CANONICAL = (
    "pr-checks",
    "lint-pr",
    "python-validate",
    "quality-audit",
    "dependency-review",
    # Produces no required context; here because the release model depends on it.
    "release-drafter",
)
INTEGRATION_ONLY = ("hacs-validate", "hassfest-validate", "release")
# The reusable workflow each pointer file must call. auto-draft-pr is judged when
# carried, never required.
POINTERS = {
    "pr-checks.yml": "PineappleEmperor/release-flow/.github/workflows/pr-checks.yml",
    "lint-pr.yml": "PineappleEmperor/release-flow/.github/workflows/lint-pr.yml",
    "release-drafter.yml": (
        "PineappleEmperor/release-flow/.github/workflows/release-drafter.yml"
    ),
    "auto-draft-pr.yml": (
        "PineappleEmperor/release-flow/.github/workflows/auto-draft-pr.yml"
    ),
    "python-validate.yml": (
        "PineappleEmperor/ha-integration-ci/.github/workflows/python-validate.yml"
    ),
    "quality-audit.yml": (
        "PineappleEmperor/ha-integration-ci/.github/workflows/quality-audit.yml"
    ),
    "release.yml": "PineappleEmperor/ha-integration-ci/.github/workflows/release.yml",
    "panel-bundle.yml": (
        "PineappleEmperor/ha-panel-ci/.github/workflows/panel-bundle.yml"
    ),
}
PIN_EXEMPT = ("hacs/action", "home-assistant/actions")
CANON_RULES = {
    "action-setup",
    "appropriate-polling",
    "brands",
    "common-modules",
    "config-flow-test-coverage",
    "config-flow",
    "dependency-transparency",
    "docs-actions",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-removal-instructions",
    "entity-event-setup",
    "entity-unique-id",
    "has-entity-name",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "unique-config-entry",
    "config-entry-unloading",
    "log-when-unavailable",
    "entity-unavailable",
    "action-exceptions",
    "reauthentication-flow",
    "parallel-updates",
    "test-coverage",
    "integration-owner",
    "docs-installation-parameters",
    "docs-configuration-parameters",
    "entity-translations",
    "entity-device-class",
    "devices",
    "entity-category",
    "entity-disabled-by-default",
    "discovery",
    "stale-devices",
    "diagnostics",
    "exception-translations",
    "icon-translations",
    "reconfiguration-flow",
    "dynamic-devices",
    "discovery-update-info",
    "repair-issues",
    "docs-use-cases",
    "docs-supported-devices",
    "docs-supported-functions",
    "docs-data-update",
    "docs-known-limitations",
    "docs-troubleshooting",
    "docs-examples",
    "async-dependency",
    "inject-websession",
    "strict-typing",
}
ANTIPATTERNS = (
    (
        r"discovery\.async_load_platform",
        "deprecated discovery.async_load_platform (use NotifyEntity / platform forward)",
    ),
    (
        r"BaseNotificationService",
        "deprecated BaseNotificationService (use NotifyEntity)",
    ),
    (
        r"update_before_add=True",
        "update_before_add=True (populate via property or _handle_coordinator_update)",
    ),
    (r"OptionsFlowHandler", "deprecated OptionsFlowHandler name (use OptionsFlow)"),
    (
        r"PlatformNotReady",
        "PlatformNotReady in a config-entry integration (use ConfigEntryNotReady)",
    ),
    (
        r'_LOGGER\.[a-z]+\(\s*f"',
        "f-string in a logging call (use lazy % args — ruff G004)",
    ),
)


class Repo:
    """The repository under audit, and the small facts every check needs."""

    def __init__(self, root: pathlib.Path) -> None:
        """Locate the integration package and the workflow directory under root."""
        self.root = root
        components = sorted(root.glob("custom_components/*/"))
        self.cc = components[0] if components else None
        self.workflows = root / ".github/workflows"

    def text(self, rel: str) -> str:
        """The file's text, or empty when it does not exist."""
        p = self.root / rel
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""

    def yaml(self, rel: str) -> dict:
        """The file parsed as YAML, or empty when absent or unparseable."""
        try:
            return yaml.safe_load(self.text(rel)) or {}
        except OSError, yaml.YAMLError:
            return {}

    def exists(self, rel: str) -> bool:
        """Whether the path exists under root."""
        return (self.root / rel).exists()

    def workflow_files(self) -> list[pathlib.Path]:
        """Every workflow file, sorted, or none when the directory is absent."""
        return sorted(self.workflows.glob("*.y*ml")) if self.workflows.is_dir() else []

    def steps(self, path: pathlib.Path) -> list[tuple[str, dict]]:
        """Every (job name, step) pair in one workflow."""
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except OSError, yaml.YAMLError:
            return []
        return [
            (jn, s or {})
            for jn, job in (doc.get("jobs") or {}).items()
            for s in (job or {}).get("steps", []) or []
        ]


def _live(run: str) -> str:
    """A run: block with its shell comments dropped.

    A name mentioned only in a comment is documentation, not an invocation — matching
    the raw body counted pr-checks.yml's explanation of manifest_gate.py as wiring.
    """
    return "\n".join(
        line for line in run.splitlines() if not line.lstrip().startswith("#")
    )


def _is_pointer(doc: dict) -> bool:
    """Whether every job in a parsed workflow calls another workflow instead of running steps."""
    jobs = doc.get("jobs") or {}
    return bool(jobs) and all(
        "uses" in (job or {}) and "steps" not in (job or {}) for job in jobs.values()
    )


def _named(repo: Repo, stem: str) -> str | None:
    """The workflow path for `stem`, accepting the older underscore spelling.

    The pointer files are hyphenated; a repo mid-migration still carries the underscore
    names, and a check that only knew one spelling would judge nothing there.
    """
    for name in (stem, stem.replace("-", "_")):
        rel = f".github/workflows/{name}.yml"
        if repo.exists(rel):
            return rel
    return None


def check_canonical_files(repo: Repo) -> Result:
    """Every pointer and config the stack cannot run without."""
    fails, warns = [], []
    fails += [
        f"missing .github/workflows/{w}.yml"
        for w in CANONICAL
        if not repo.exists(f".github/workflows/{w}.yml")
    ]
    if repo.cc:
        fails += [
            f"missing .github/workflows/{w}.yml"
            for w in INTEGRATION_ONLY
            if not repo.exists(f".github/workflows/{w}.yml")
        ]
    # Panel repos only: on a repo with no frontend/ the workflow's own first run fails,
    # and nothing required it on the repos that have one, so both real panel repos sat
    # on the superseded frontend_build.yml with nothing objecting.
    if repo.exists("frontend/package.json") and not repo.exists(
        ".github/workflows/panel-bundle.yml"
    ):
        fails.append(
            "missing .github/workflows/panel-bundle.yml (frontend/ exists, so the panel's "
            "TypeScript is checked by nothing)"
        )
    if repo.exists(".github/workflows/frontend_build.yml"):
        fails.append(
            "frontend_build.yml is superseded by panel-bundle.yml (it gated merges on a "
            "build artefact; release.yml rebuilds the bundle before packing the zip)"
        )
    for f, why in (
        (".github/release-drafter.yml", ""),
        (".github/dependabot.yml", ""),
        (".gitignore", ""),
    ):
        if not repo.exists(f):
            fails.append(f"missing {f}{why}")
    return fails, warns


def check_pointers(repo: Repo) -> Result:
    """Each pointer must call the workflow it stands for, in the repository that owns it.

    The pin's shape is check_action_pins' concern, which reads the same line.
    """
    fails = []
    for name, target in POINTERS.items():
        rel = f".github/workflows/{name}"
        if not repo.exists(rel):
            continue  # absence is check_canonical_files' verdict, reported once
        doc = repo.yaml(rel)
        if set(doc.get(True) or doc.get("on") or {}) == {"workflow_call"}:
            continue
        jobs = doc.get("jobs") or {}
        if not jobs:
            fails.append(f"{name} defines no job; expected one calling {target}")
            continue
        for jid, job in jobs.items():
            uses = str((job or {}).get("uses") or "")
            if not uses:
                fails.append(
                    f"{name} job '{jid}' carries a workflow body instead of calling "
                    f"{target}: it is a copy, not a pointer, so no CI release reaches it"
                )
                continue
            path, _, _sha = uses.rpartition("@")
            if path != target:
                fails.append(
                    f"{name} job '{jid}' uses {path or uses}; expected {target}"
                )
    return fails, []


def check_no_tracked_artefacts(repo: Repo) -> Result:
    """A committed .pyc ships inside every release zip."""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return [], []
    tracked = [
        line for line in out.splitlines() if re.search(r"__pycache__|\.py[cod]$", line)
    ]
    if tracked:
        return [
            "compiled Python artefacts are tracked (git rm --cached, and add them "
            "to .gitignore): " + ", ".join(tracked[:5])
        ], []
    return [], []


def check_single_body_writer(repo: Repo) -> Result:
    """Two writers race, and the loser's output is what users read."""
    writers = []
    for wf in repo.workflow_files():
        for jn, step in repo.steps(wf):
            run = _live(str(step.get("run", "")))
            if re.search(r"gh release (edit|create)[^\n]*--notes", run):
                writers.append(f"{wf.name}:{jn} (gh release --notes)")
            with_ = step.get("with") or {}
            if str(with_.get("generate_release_notes", "")).lower() == "true":
                writers.append(f"{wf.name}:{jn} (generate_release_notes)")
            if "body" in with_ or "body_path" in with_:
                writers.append(f"{wf.name}:{jn} (body)")
    if len(writers) > 1:
        return [
            "more than one workflow step writes the release body; they race and the "
            "published notes end up containing both: " + "; ".join(writers)
        ], []
    return [], []


def check_previous_tag(repo: Repo) -> Result:
    """`--limit 1` on a release event returns the release being written."""
    rel = _named(repo, "release-drafter")
    if not rel:
        return [], []
    doc = repo.yaml(rel)
    on = doc.get(True) or doc.get("on") or {}
    if "release" not in on or _is_pointer(doc):
        return [], []
    for _, step in repo.steps(repo.root / rel):
        run = str(step.get("run", ""))
        if "release_notes.py" not in run:
            continue
        prev = next(
            (line for line in run.splitlines() if re.match(r"\s*PREV=", line)), ""
        )
        if "--limit 1 " in prev or prev.rstrip().endswith("--limit 1"):
            return [
                (
                    f"{pathlib.Path(rel).name} resolves the previous tag with `--limit 1` "
                    "while triggering on `release: published`; that returns the release "
                    "being written and the notes come out empty. Exclude the current tag."
                )
            ], []
    return [], []


def check_zip_release_patches_manifest(repo: Repo) -> Result:
    """An unpatched zip ships whatever version the last PR happened to commit."""
    if not (repo.exists("hacs.json") and repo.exists(".github/workflows/release.yml")):
        return [], []
    if not re.search(r'"zip_release"\s*:\s*true', repo.text("hacs.json")):
        return [], []
    # A pointer has no body to read; check_pointers holds it to the workflow that does the
    # patching, and that body is judged where it lives.
    if _is_pointer(repo.yaml(".github/workflows/release.yml")):
        return [], []
    if "manifest.json" not in repo.text(".github/workflows/release.yml"):
        return [
            (
                "release.yml builds a zip_release asset without setting the manifest "
                "version from the tag (point it at ha-integration-ci's release.yml)"
            )
        ], []
    return [], []


def check_no_placeholders(repo: Repo) -> Result:
    """A `<placeholder>` left in a workflow step."""
    fails = []
    for wf in repo.workflow_files():
        for jn, step in repo.steps(wf):
            texts = [_live(str(step.get("run", "")))]
            for key in ("with", "env"):
                texts += [str(v) for v in (step.get(key) or {}).values()]
            found = sorted(
                {m for t in texts for m in re.findall(r"<[a-z][a-z0-9_-]*>", t)}
            )
            if found:
                fails.append(
                    f"{wf.relative_to(repo.root)} step '{step.get('name') or jn}' "
                    f"carries the unsubstituted placeholder {', '.join(found)} — "
                    f"bash reads `<` as a redirect, so the step cannot run"
                )
    return fails, []


# `python` or `python3` as a shell word: the start of the block, after whitespace, or after
# a shell operator. `pipx`, `python-version` and paths like `bin/python3x` do not match.
_PYTHON_CALL = re.compile(r"(?:^|[\s;&|(])python3?(?=\s|$)", re.MULTILINE)


def check_python_steps_have_a_setup(repo: Repo) -> Result:
    """A step that runs Python with no setup-python before it runs on the runner's own."""
    fails = []
    for wf in repo.workflow_files():
        ready: set[str] = set()
        for jn, step in repo.steps(wf):
            if "actions/setup-python" in str(step.get("uses", "")):
                ready.add(jn)
                continue
            if jn not in ready and _PYTHON_CALL.search(_live(str(step.get("run", "")))):
                fails.append(
                    f"{wf.relative_to(repo.root)} job '{jn}' step "
                    f"'{step.get('name') or jn}' runs Python on the runner's own "
                    f"interpreter; put actions/setup-python before it so it runs on "
                    f"the declared floor"
                )
    return fails, []


def check_label_events(repo: Repo) -> Result:
    """`labeled` plus `cancel-in-progress` makes cancelled runs look like failures."""
    if not repo.exists(".github/workflows/pr-checks.yml"):
        return [], []
    doc = repo.yaml(".github/workflows/pr-checks.yml")
    on = doc.get(True) or doc.get("on") or {}
    types = set(
        (on.get("pull_request_target") or on.get("pull_request") or {}).get("types", [])
    )
    cancels = bool((doc.get("concurrency") or {}).get("cancel-in-progress"))
    hazard = types & {"labeled", "unlabeled"}
    if hazard and cancels:
        return [
            (
                f"pr-checks.yml triggers on {sorted(hazard)} with cancel-in-progress. A bot "
                f"applying several labels starts a run per label; the cancelled ones make "
                f"the status rollup FAILURE and the PR unmergeable."
            )
        ], []
    return [], []


def check_release_drafter_wiring(repo: Repo) -> Result:
    """A drafter body must run the notes generator, its checker, and clone deep enough."""
    rel = _named(repo, "release-drafter")
    if not rel or _is_pointer(repo.yaml(rel)):
        return [], []
    t = repo.text(rel)
    fails = []
    if "scripts/release_notes.py" not in t:
        fails.append(
            f"{rel} never runs scripts/release_notes.py (notes fall back to "
            f"release-drafter's $CHANGES, grouped by PR label)"
        )
    if "scripts/check_release_notes.py" not in t:
        fails.append(
            f"{rel} never runs scripts/check_release_notes.py (a malformed "
            f"release description would ship unnoticed)"
        )
    if "fetch-depth: 0" not in t:
        fails.append(
            f"{rel} checks out at depth 1; release_notes.py cannot resolve its "
            f"commit range without fetch-depth: 0"
        )
    return fails, []


def check_classifier_not_inlined(repo: Repo) -> Result:
    """An inline classifier cannot be unit-tested and corrupts notes silently."""
    if "MAINT = " in repo.text(".github/workflows/pr-checks.yml"):
        return [
            "pr-checks.yml inlines the commit classifier (call scripts/commit_summary.py instead)"
        ], []
    return [], []


def _quality_scale(repo: Repo) -> dict:
    if not repo.cc:
        return {}
    rel = str((repo.cc / "quality_scale.yaml").relative_to(repo.root))
    return (repo.yaml(rel) or {}).get("rules") or {}


def _status(value) -> str | None:
    return value if isinstance(value, str) else (value or {}).get("status")


def check_claims_have_tests(repo: Repo) -> Result:
    """A `done` with no test is a claim, not evidence."""
    rules = _quality_scale(repo)
    done = sum(1 for v in rules.values() if _status(v) == "done")
    fails, warns = [], []
    tests = repo.exists("tests")
    if _status(rules.get("test-coverage")) == "done" and repo.exists("frontend"):
        found = list((repo.root / "frontend").rglob("*.test.ts")) + list(
            (repo.root / "frontend").rglob("*.spec.ts")
        )
        if not found:
            fails.append(
                "quality_scale marks test-coverage done, but the panel has no "
                "frontend tests (its presentation logic is reachable from nothing else)"
            )
    if tests and repo.cc:
        if not repo.exists("requirements.test.txt"):
            fails.append(
                "tests/ exists but requirements.test.txt is missing (pytest step "
                "cannot install the suite)"
            )
        if repo.exists("conftest.py"):
            conftest = repo.text("conftest.py")
            if not re.search(r"^import custom_components", conftest, re.MULTILINE):
                fails.append(
                    "conftest.py does not import custom_components (HA will not "
                    "discover the integration)"
                )
            if "enable_custom_integrations" not in conftest:
                fails.append("conftest.py does not pull in enable_custom_integrations")
        else:
            fails.append(
                "missing root conftest.py (must be at the repo root, not tests/conftest.py)"
            )
        if not re.search(r'asyncio_mode\s*=\s*"auto"', repo.text("pyproject.toml")):
            fails.append(
                'pyproject.toml missing asyncio_mode = "auto" (async tests never run)'
            )
        # The pytest step lives in this repository's python-validate.yml; a pointer at it
        # is the proof the suite runs, and check_pointers holds the pointer to that target.
        # A repo still carrying a body is judged on what that body runs.
        validate = ".github/workflows/python-validate.yml"
        if "pytest" not in repo.text(validate) and not _is_pointer(repo.yaml(validate)):
            fails.append(
                "python-validate.yml has no pytest step and is not a pointer at one "
                "(quality_scale 'done' rules would go unproven)"
            )
    elif done:
        fails.append(
            f"quality_scale marks {done} rule(s) done but there is no tests/ "
            f"directory — a done without a test is a claim, not evidence"
        )
    if (
        repo.exists("requirements.test.txt")
        and repo.cc
        and not re.search(
            r"pytest-homeassistant-custom-component\s*==",
            repo.text("requirements.test.txt"),
        )
    ):
        warns.append(
            "pytest-homeassistant-custom-component is unpinned in requirements.test.txt"
        )
    return fails, warns


def check_action_pins(repo: Repo) -> Result:
    """Every `uses:` pinned to a 40-hex SHA with a version comment."""
    uses = re.compile(r"uses:\s*(?P<ref>[^\s#]+)\s*(?P<comment>#.*)?$")
    fails = []
    for wf in repo.workflow_files():
        for n, line in enumerate(wf.read_text(errors="replace").splitlines(), 1):
            m = uses.search(line)
            if not m:
                continue
            ref = m.group("ref")
            if ref.startswith("./") or any(ref.startswith(e) for e in PIN_EXEMPT):
                continue
            sha = ref.rsplit("@", 1)[-1] if "@" in ref else ""
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                fails.append(f"{wf.name}:{n} {ref} is not pinned to a commit SHA")
            elif not re.search(r"#\s*v?\d+\.\d+", m.group("comment") or ""):
                fails.append(
                    f"{wf.name}:{n} {ref} has no version comment (nothing says "
                    f"what this SHA is)"
                )
    return fails, []


def check_pr_checks_shape(repo: Repo) -> Result:
    """Ordering and pull_request_target safety, which no other workflow can provide."""
    rel = ".github/workflows/pr-checks.yml"
    if not repo.exists(rel):
        return [], []
    t = repo.text(rel)
    fails, warns = [], []
    if "pull_request_target" not in t:
        fails.append(
            "pr-checks.yml must use pull_request_target (fork PRs get a read-only "
            "token otherwise)"
        )
    if _is_pointer(repo.yaml(rel)):
        return fails, warns  # the jobs live in release-flow and are judged there
    if "Remove superseded" not in t:
        fails.append("pr-checks.yml missing the removal-only superseded-label step")
    # The label decides the release category and the version bump, so it has to be the
    # RIGHT label, not merely a label. Checking presence alone passed a `fix:`-titled PR
    # carrying a `feat!:` commit, which released a breaking change as a patch.
    if "--mode label" not in t:
        fails.append(
            "pr-checks.yml does not compare the PR's label against the one its "
            "commits entitle it to (scripts/commit_summary.py --mode label)"
        )
    if "needs: label" not in t:
        fails.append(
            "pr-checks.yml: label-reading jobs must declare 'needs: label' (else "
            "they race the autolabeler)"
        )
    if "user.type != 'Bot'" not in t:
        fails.append("pr-checks.yml does not skip bot-authored PRs")
    if "actions/checkout" in t:
        if "ref: ${{ github.event.pull_request.base.ref }}" not in t:
            fails.append(
                "pr-checks.yml must check out base.ref (never run PR code under "
                "pull_request_target; not base.sha either — that is frozen at PR "
                "creation while the workflow runs from the base branch head, so a "
                "scripts/ change merged mid-PR ran the new workflow against the "
                "old script)"
            )
        if re.search(r"actions/checkout[^\n]*\n(?:[^\n]*\n){0,2}?[^\n]*head\.sha", t):
            fails.append(
                "pr-checks.yml checks out the PR head under pull_request_target"
            )
    # checkout clears the workspace, so a job that writes a file first loses it
    late = [
        jn
        for jn, steps in _jobs_steps(repo, rel).items()
        if any("actions/checkout" in str(s.get("uses", "")) for s in steps)
        and "actions/checkout" not in str((steps[0] or {}).get("uses", ""))
    ]
    fails += [
        f"pr-checks.yml: actions/checkout must be the FIRST step of job '{jn}' "
        f"(it clears the workspace)"
        for jn in late
    ]
    return fails, warns


def check_no_run_interpolation(repo: Repo) -> Result:
    """`${{ }}` inside a `run:` is substituted as text before the shell sees it."""
    fails = []
    for wf in repo.workflow_files():
        for jn, steps in _jobs_steps(repo, f".github/workflows/{wf.name}").items():
            for s in steps:
                fails += [
                    f"{wf.name} interpolates ${{{{ {expr} }}}} inside a run: block "
                    f"in '{s.get('name') or jn}' (pass it through env:)"
                    for expr in re.findall(
                        r"\$\{\{\s*([^}]+?)\s*\}\}", str(s.get("run", ""))
                    )
                ]
    return fails, []


def _jobs_steps(repo: Repo, rel: str) -> dict[str, list[dict]]:
    doc = repo.yaml(rel)
    return {
        jn: (job or {}).get("steps", []) or []
        for jn, job in (doc.get("jobs") or {}).items()
    }


def check_no_ignored_validations(repo: Repo) -> Result:
    """`ignore:` disqualifies the repo from the HACS default store."""
    if not repo.cc:
        return [], []
    fails = []
    for w in ("hacs-validate", "hassfest-validate"):
        rel = _named(repo, w)
        if rel and re.search(r"^\s*ignore:", repo.text(rel), re.MULTILINE):
            fails.append(
                f"{pathlib.Path(rel).name} sets ignore: — ignoring any check "
                f"disqualifies the repo from the HACS default store"
            )
    return fails, []


def check_sole_labeler(repo: Repo) -> Result:
    """A second labeler makes labels flap and breaks `needs: label` ordering."""
    rel = _named(repo, "release-drafter")
    if not rel:
        return [], []
    doc = repo.yaml(rel)
    triggers = set(doc.get(True) or doc.get("on") or {})
    bad = []
    if triggers - {"push", "workflow_dispatch", "release"}:
        bad.append(f"triggers {sorted(triggers)} (expected push and release only)")
    bad += [
        f"job '{n}' looks like a second labeler"
        for n in (doc.get("jobs") or {})
        if "label" in n.lower()
    ]
    if bad:
        return [
            f"{pathlib.Path(rel).name} may trigger only on push, workflow_dispatch or "
            "release with no autolabeler job (pr-checks.yml is the sole labeler): "
            + "; ".join(bad)
        ], []
    return [], []


def check_pr_openers(repo: Repo) -> Result:
    """Only draft-only, actor-gated openers may exist."""
    fails = []
    if repo.exists(".github/workflows/create-dev-pr.yml"):
        fails.append(
            "create-dev-pr.yml is superseded (use auto-draft-pr.yml, which is "
            "draft-only and actor-gated)"
        )
    # The sanctioned openers are the ones a human cannot open for themselves: a bot that
    # must propose its own change, or the draft opener that exists so a title is never
    # typed. Anything else opening a PR is a workflow acting as an author. A repo with a
    # different delivery model declares its own with a marker rather than being named
    # here — this audit runs in every scaffolded integration and should not carry the
    # filenames of repos it never runs in.
    sanctioned = ("auto-draft-pr.yml", "auto_draft_pr.yml")
    for wf in repo.workflow_files():
        text = wf.read_text(errors="replace")
        if (
            "gh pr create" in text
            and wf.name not in sanctioned
            and "# skill-audit: sanctioned-opener" not in text
        ):
            fails.append(
                f"{wf.name} opens PRs with 'gh pr create' (only "
                + ", ".join(sanctioned)
                + " may, or mark it "
                "'# skill-audit: sanctioned-opener' with a reason)"
            )
    rel = _named(repo, "auto-draft-pr")
    # A pointer has no actor gate or --draft to read; that body lives in release-flow.
    if rel and not _is_pointer(repo.yaml(rel)):
        opener = repo.text(rel)
        if "github.actor == github.repository_owner" not in opener:
            fails.append(
                f"{pathlib.Path(rel).name} must gate on the actor being the repo "
                "owner, or it opens PRs that impersonate the token owner"
            )
        if "--draft" not in opener:
            fails.append(f"{pathlib.Path(rel).name} must open the PR as a draft")
    return fails, []


def check_platforms_have_modules(repo: Repo) -> Result:
    """Every name in PLATFORMS must have a module beside it.

    `async_forward_entry_setups` imports `<domain>/<platform>.py` for each name, so a name
    with no module raises ModuleNotFoundError and the entry never reaches LOADED. Observed
    on a live repo: `PLATFORMS = ["sensor"]` with no `sensor.py`, inert until someone wired
    the forward, at which point setup died. Mechanical, so the skill states the build rule
    and the gate catches the mismatch.
    """
    if not repo.cc:
        return [], []
    fails = []
    for pkg in [repo.cc]:  # repo.cc IS the integration package
        names: list[str] = []
        for py in (pkg / "const.py", pkg / "__init__.py"):
            if not py.is_file():
                continue
            m = re.search(
                r"PLATFORMS[^=]*=\s*\[(?P<body>[^\]]*)\]",
                py.read_text(errors="replace"),
            )
            if m:
                names += re.findall(r"[\"\']([a-z_]+)[\"\']", m.group("body"))
                names += [
                    p.split(".")[-1].lower()
                    for p in re.findall(r"Platform\.([A-Z_]+)", m.group("body"))
                ]
        fails += [
            f"{pkg.name}: PLATFORMS names {name!r} but {pkg.name}/{name}.py "
            "does not exist — the entry will fail to set up"
            for name in dict.fromkeys(names)
            if not (pkg / f"{name}.py").is_file()
        ]
    return fails, []


def check_antipatterns(repo: Repo) -> Result:
    """Deprecated APIs that still import cleanly and fail at runtime."""
    if not repo.cc:
        return [], []
    fails, warns = [], []
    sources = list(repo.cc.rglob("*.py"))
    blob = {p: p.read_text(errors="replace") for p in sources}
    for pattern, message in ANTIPATTERNS:
        if any(re.search(pattern, t) for t in blob.values()):
            fails.append(message)
    bare = [
        f"{p}"
        for p, t in blob.items()
        if any(
            "# type: ignore" in line and "import-untyped" not in line
            for line in t.splitlines()
        )
    ]
    if bare:
        fails.append(
            "bare # type: ignore (Platinum: only [import-untyped] with a reason): "
            + ", ".join(str(p.name) for p in map(pathlib.Path, bare[:3]))
        )
    # `from __future__ import annotations` is deliberately not demanded: Python 3.14
    # defers annotation evaluation natively, core bans the import, and the consumer's
    # pyproject.toml enforces that ban through ruff.
    return fails, warns


def check_quality_scale_and_manifest(repo: Repo) -> Result:
    """Honesty of the claims a consumer reads before installing."""
    if not repo.cc:
        return [], []
    fails, warns = [], []
    if repo.exists(str((repo.cc / "quality_scale.yaml").relative_to(repo.root))):
        missing = sorted(CANON_RULES - set(_quality_scale(repo)))
        if missing:
            fails.append(
                f"quality_scale.yaml does not enumerate the canonical rule set: "
                f"{len(missing)} absent, e.g. {missing[:6]}"
            )
    else:
        fails.append("missing quality_scale.yaml")
    manifest = repo.cc / "manifest.json"
    m = manifest.read_text(errors="replace") if manifest.is_file() else ""
    if '"integration_type"' not in m:
        fails.append("manifest.json missing integration_type")
    if '"issue_tracker"' not in m:
        fails.append("manifest.json missing issue_tracker (HACS requires it)")
    if (
        re.search(r'"config_flow"\s*:\s*true', m)
        and not (repo.cc / "config_flow.py").is_file()
    ):
        fails.append(
            f"manifest declares config_flow: true but {repo.cc.name}/config_flow.py is missing"
        )
    if repo.exists("frontend/package.json") and not re.search(
        r'"test"\s*:', repo.text("frontend/package.json")
    ):
        warns.append(
            "frontend/package.json has no test script; the panel's presentation "
            "logic is unproven"
        )
    if re.search(r'"(frontend|panel_custom)"', m) and not re.search(
        r"^\s*home-assistant-frontend==",
        repo.text("requirements.test.txt"),
        re.MULTILINE,
    ):
        fails.append(
            "manifest depends on frontend/panel_custom but requirements.test.txt has "
            "no home-assistant-frontend pin (every setup test will fail in CI with: "
            "No module named 'hass_frontend')"
        )
    for f, why in (
        (("CLAUDE.md"), "the skill's per-repo enforcement"),
        (("README.md"), "HACS 'information' and 'images' checks both need it"),
    ):
        if not repo.exists(f):
            fails.append(f"missing {f} ({why})")
    if not repo.exists("pyrightconfig.json"):
        warns.append("missing pyrightconfig.json")
    return fails, warns


def check_autolabeler_title_only(repo: Repo) -> Result:
    """A branch rule flaps whenever the branch name disagrees with the commits."""
    if not repo.exists(".github/release-drafter.yml"):
        return [], []
    cfg = repo.yaml(".github/release-drafter.yml")
    bad = [
        r.get("label")
        for r in cfg.get("autolabeler", []) or []
        if set(r) - {"label", "title"}
    ]
    if bad:
        return [
            (
                f"release-drafter.yml autolabeler has non-title rules (title-only, or labels "
                f"flap): {bad}"
            )
        ], []
    return [], []


def check_drafter_categories(repo: Repo) -> Result:
    """v7 matches under `when:`; the v6 shape parses and matches nothing."""
    if not repo.exists(".github/release-drafter.yml"):
        return [], []
    cfg = repo.yaml(".github/release-drafter.yml")
    bad = [
        c.get("title") or c.get("type")
        for c in cfg.get("categories") or []
        if "labels" in c or "label" in c
    ]
    if bad:
        return [
            (
                f"release-drafter categories use the v6 top-level `labels:`; v7 matches under "
                f"`when:` and these never match, so the version resolves to a patch bump: {bad}"
            )
        ], []
    return [], []


def check_docstrings(repo: Repo) -> Result:
    """Single-line docstrings on functions and classes; modules are exempt."""
    if not repo.cc:
        return [], []
    bad = []
    for f in sorted(repo.cc.rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            doc = ast.get_docstring(node, clean=False)
            if doc and "\n" in doc.strip():
                bad.append(f"{f}:{node.lineno} {node.name}")
    if bad:
        return [
            "multi-line docstring on a function or class in custom_components/ "
            "(single-line required; module docstrings are exempt): "
            + "; ".join(bad[:3])
        ], []
    return [], []


def check_commit_hook(repo: Repo) -> Result:
    """Shipping the hook is not enabling it."""
    hook = repo.root / ".githooks/commit-msg"
    if not hook.is_file():
        return [], ["no .githooks/commit-msg (terse-subject + AI-trailer rejection)"]
    fails, warns = [], []
    if not os.access(hook, os.X_OK):
        fails.append(".githooks/commit-msg is not executable (chmod +x)")
    text = hook.read_text()
    # A hook that only measures length passes a well-formed subject that says nothing.
    # Both guards were added after a subject that passed every rule and named nothing:
    # the shape rule keeps the type mapped for the release notes, the word list
    # catches a subject that editorialises instead of saying what changed.
    if "feat|fix|docs" not in text:
        fails.append(
            ".githooks/commit-msg does not enforce the Conventional Commit subject shape"
        )
    if "editorialising" not in text:
        fails.append(".githooks/commit-msg does not reject editorialising subjects")
    try:
        configured = subprocess.run(
            ["git", "config", "core.hooksPath"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if configured != ".githooks":
            warns.append(
                "core.hooksPath is not .githooks — run: git config core.hooksPath .githooks"
            )
    except OSError:
        pass
    return fails, warns


def check_brand_assets(repo: Repo) -> Result:
    """A present icon.png with no @2x is the classic 'icon shows only sometimes' bug."""
    if not repo.cc:
        return [], []
    brand = repo.cc / "brand"
    if not brand.is_dir():
        return [
            f"missing {brand.relative_to(repo.root)}/ (HACS check-brands fails without icon.png)"
        ], []

    def size(p: pathlib.Path):
        b = p.read_bytes()
        return struct.unpack(">II", b[16:24]) if b[:8] == b"\x89PNG\r\n\x1a\n" else None

    bad = []
    for name, expected in (("icon.png", (256, 256)), ("icon@2x.png", (512, 512))):
        f = brand / name
        if not f.is_file():
            bad.append(f"missing {f.relative_to(repo.root)}")
        elif size(f) != expected:
            bad.append(f"{f.relative_to(repo.root)} is {size(f)}, expected {expected}")
    bad += [
        f"missing {(brand / n).relative_to(repo.root)}"
        for n in ("logo.png", "logo@2x.png")
        if not (brand / n).is_file()
    ]
    return (
        [f"brand assets missing or wrongly sized: {'; '.join(bad)}"] if bad else []
    ), []


def check_release_token(repo: Repo) -> Result:
    """The opener needs a token whose PRs trigger workflows."""
    if not _named(repo, "auto-draft-pr"):
        return [], []
    try:
        out = subprocess.run(
            ["gh", "secret", "list", "--json", "name", "--jq", ".[].name"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return [], [
            "cannot list secrets here — verify RELEASE_TOKEN exists, or draft PRs will not open"
        ]
    if out.returncode != 0:
        return [], [
            "cannot list secrets here — verify RELEASE_TOKEN exists, or draft PRs will not open"
        ]
    names = set(out.stdout.split())
    # Either sanctioned source: the PAT, or the GitHub App pair the App path mints from.
    if "RELEASE_TOKEN" in names or {"APP_ID", "APP_PRIVATE_KEY"} <= names:
        return [], []
    return [
        (
            "the draft-PR opener is present but neither RELEASE_TOKEN nor the "
            "APP_ID/APP_PRIVATE_KEY pair is set (see SKILL.md, RELEASE_TOKEN)"
        )
    ], []


def check_required_status_checks(repo: Repo) -> Result:
    """Every workflow is advisory until the default branch requires it."""
    try:
        name = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return [], [
            "gh is not available — required status checks NOT CHECKED, not passed"
        ]
    slug = name.stdout.strip()
    if not slug:
        return [], [
            "no GitHub remote resolved — required status checks NOT CHECKED, not passed"
        ]
    branch = subprocess.run(
        ["gh", "api", f"repos/{slug}", "--jq", ".default_branch"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not branch:
        return [], [
            (
                f"could not read {slug} (token lacks permission?) — required status checks "
                "NOT CHECKED, not passed"
            )
        ]
    rules = subprocess.run(
        ["gh", "api", f"repos/{slug}/rules/branches/{branch}", "--jq", "[.[].type]"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not rules:
        return [], [
            (
                f"could not read branch rules for {branch} (token lacks permission?) — "
                f"verify required status checks by hand"
            )
        ]
    fails, warns = [], []
    if "required_status_checks" not in rules:
        fails.append(
            f"no required status checks on {branch} — every workflow in this stack is "
            f"advisory and a red PR can be merged"
        )
    if "non_fast_forward" not in rules:
        warns.append(f"force-pushes to {branch} are not blocked")
    return fails, warns


def _contexts(doc: dict, source: str) -> dict[str, str]:
    """Check-run name -> source, pointer jobs recorded as the prefix they produce."""
    out: dict[str, str] = {}
    for jid, job in (doc.get("jobs") or {}).items():
        job = job or {}
        name = str(job.get("name") or jid)
        out[f"{name} / " if "uses" in job else name] = source
    return out


def _produced(produced: dict[str, str], context: str) -> bool:
    """Whether a required context is a produced name, or falls under a pointer's prefix."""
    return context in produced or any(
        k.endswith(" / ") and context.startswith(k) for k in produced
    )


def _job_names(wf_dir: pathlib.Path) -> dict[str, str]:
    """Check-run name (or pointer prefix) -> the workflow that defines it."""
    out: dict[str, str] = {}
    for wf in sorted(wf_dir.glob("*.y*ml")):
        try:
            doc = yaml.safe_load(wf.read_text(encoding="utf-8")) or {}
        except OSError, yaml.YAMLError:
            continue
        out.update(_contexts(doc, wf.name))
    return out


def _required_contexts(ruleset: pathlib.Path) -> list[str]:
    """The status-check contexts a ruleset JSON makes required."""
    try:
        doc = json.loads(ruleset.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return []
    return [
        c["context"]
        for r in doc.get("rules") or []
        if r.get("type") == "required_status_checks"
        for c in (r.get("parameters") or {}).get("required_status_checks") or []
        if c.get("context")
    ]


def check_required_contexts_have_producers(repo: Repo) -> Result:
    """A required context that no job in the workflows produces."""
    if not (repo.exists("ruleset.json") and repo.workflows.is_dir()):
        return [], []
    produced = _job_names(repo.workflows)
    # Under pull_request_target the producing workflow is the BASE branch's, so a branch
    # that deletes a job the base still defines has not orphaned the context — that
    # misreading removed a working gate once. Count both trees, and say which were
    # consulted.
    base = _base_ref(repo)
    on_base = _job_names_at(repo, base) if base else None
    if on_base is not None:
        produced = {**on_base, **produced}
        judged = f".github/workflows/ on {base} or in the working tree"
    else:
        judged = ".github/workflows/ in the working tree (no base branch to consult)"
    return [
        f"ruleset.json requires the status check {c!r}, but no job in "
        f"{judged} is named that — it can never report and "
        f"every PR stays blocked"
        for c in _required_contexts(repo.root / "ruleset.json")
        if not _produced(produced, c)
    ], []


def _base_ref(repo: Repo) -> str | None:
    """The remote default branch as a ref, when the clone can see it."""
    candidates: list[str] = []
    try:
        head = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=False,
        )
        if head.returncode == 0 and head.stdout.strip():
            candidates.append(head.stdout.strip())
        candidates += ["origin/main", "origin/master"]
        for ref in candidates:
            ok = subprocess.run(
                ["git", "rev-parse", "--verify", "-q", ref + "^{commit}"],
                cwd=repo.root,
                capture_output=True,
                text=True,
                check=False,
            )
            if ok.returncode == 0:
                return ref
    except OSError:
        return None
    return None


def _job_names_at(repo: Repo, ref: str) -> dict[str, str] | None:
    """Check-run name -> workflow, read from `ref` rather than the working tree."""
    try:
        ls = subprocess.run(
            ["git", "ls-tree", "--name-only", ref, ".github/workflows/"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if ls.returncode != 0:
        return None
    out: dict[str, str] = {}
    for path in ls.stdout.split():
        if not path.endswith((".yml", ".yaml")):
            continue
        show = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=False,
        )
        if show.returncode != 0:
            continue
        try:
            doc = yaml.safe_load(show.stdout) or {}
        except yaml.YAMLError:
            continue
        out.update(_contexts(doc, pathlib.Path(path).name))
    return out


def check_live_required_contexts(repo: Repo) -> Result:
    """The ruleset in force, not the one committed beside it; unverifiable is NOT CHECKED."""
    if not repo.workflows.is_dir():
        return [], []
    try:
        slug = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        return [], [
            "gh is not available — live required contexts NOT CHECKED, not passed"
        ]
    if not slug:
        return [], [
            "no GitHub remote resolved — live required contexts NOT CHECKED, not passed"
        ]
    branch = subprocess.run(
        ["gh", "api", f"repos/{slug}", "--jq", ".default_branch"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not branch:
        return [], [
            (
                f"could not read {slug} (token lacks permission?) — live required contexts "
                "NOT CHECKED, not passed"
            )
        ]
    out = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{slug}/rules/branches/{branch}",
            "--jq",
            (
                '[.[] | select(.type == "required_status_checks") '
                "| .parameters.required_status_checks[].context]"
            ),
        ],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return [], [
            (
                f"could not read branch rules for {branch} (token lacks permission?) — "
                "live required contexts NOT CHECKED, not passed"
            )
        ]
    try:
        contexts = json.loads(out.stdout)
    except ValueError:
        return [], [
            (
                f"unexpected branch-rules response for {branch} — live required contexts "
                "NOT CHECKED, not passed"
            )
        ]
    produced = _job_names(repo.workflows)
    on_base = _job_names_at(repo, f"origin/{branch}")
    judged = ".github/workflows/ in the working tree (no base branch to consult)"
    if on_base is not None:
        produced = {**on_base, **produced}
        judged = f".github/workflows/ on origin/{branch} or in the working tree"
    return [
        f"{branch} requires the status check {c!r}, but no job in {judged} is "
        f"named that — it can never report and every PR stays blocked"
        for c in contexts
        if not _produced(produced, c)
    ], []


def check_dependency_graph(repo: Repo) -> Result:
    """`dependency-review.yml` fails, rather than skips, when the graph is disabled.

    Observed on a live test repo: seven workflows green and Dependency review red alone,
    because the repo had been created private and made public, which leaves the graph off.
    A required check that can never pass blocks every PR, so this reports the state rather
    than leaving it to be discovered on the first PR.
    """
    if not _named(repo, "dependency-review"):
        return [], []
    try:
        slug = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        return [], ["gh is not available — dependency graph NOT CHECKED, not passed"]
    if not slug:
        return [], [
            "no GitHub remote resolved — dependency graph NOT CHECKED, not passed"
        ]
    probe = subprocess.run(
        ["gh", "api", f"repos/{slug}/dependency-graph/sbom", "--jq", ".sbom.name"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip():
        return [], []
    return [
        (
            f"{slug} ships dependency-review.yml but its dependency graph is off, so that "
            "check fails on every PR — enable it at Settings -> Advanced Security"
        )
    ], []


CHECKS = (
    check_canonical_files,
    check_pointers,
    check_no_tracked_artefacts,
    check_single_body_writer,
    check_previous_tag,
    check_zip_release_patches_manifest,
    check_no_placeholders,
    check_python_steps_have_a_setup,
    check_label_events,
    check_release_drafter_wiring,
    check_classifier_not_inlined,
    check_claims_have_tests,
    check_action_pins,
    check_pr_checks_shape,
    check_no_run_interpolation,
    check_no_ignored_validations,
    check_sole_labeler,
    check_pr_openers,
    check_platforms_have_modules,
    check_antipatterns,
    check_quality_scale_and_manifest,
    check_autolabeler_title_only,
    check_drafter_categories,
    check_docstrings,
    check_commit_hook,
    check_brand_assets,
    check_release_token,
    check_required_status_checks,
    check_required_contexts_have_producers,
    check_live_required_contexts,
    check_dependency_graph,
)


def audit(root: pathlib.Path) -> Result:
    """Run every check against one repository."""
    repo = Repo(root)
    fails: list[str] = []
    warns: list[str] = []
    for check in CHECKS:
        f, w = check(repo)
        fails += f
        warns += w
    return fails, warns


def main(argv: list[str] | None = None) -> int:
    """Run the audit against --root, or list the checks; exit 1 on any failure."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="repository to audit")
    # The skill used to enumerate these rules in prose, which went stale the moment the
    # pin check moved from version floors to commit SHAs. The registry is the list.
    ap.add_argument("--list", action="store_true", help="print the checks and exit")
    args = ap.parse_args(argv)

    if args.list:
        for check in CHECKS:
            summary = (check.__doc__ or "").strip().splitlines()[0]
            print(f"{check.__name__[len('check_') :]:28} {summary}")
        return 0

    root = pathlib.Path(args.root)
    repo = Repo(root)
    if not repo.cc:
        print(
            "ℹ️  no custom_components/ — skipping integration-only checks "
            "(HACS, hassfest, zip release, HA test harness)"
        )
    fails, warns = audit(root)
    for w in warns:
        print(f"⚠️  WARN: {w}")
    for f in fails:
        print(f"❌ FAIL: {f}")
    print("skill audit FAILED" if fails else "✅ skill audit passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
