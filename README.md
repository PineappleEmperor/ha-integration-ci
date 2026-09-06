# ha-integration-ci

The single home of the three [reusable workflows](https://docs.github.com/en/actions/using-workflows/reusing-workflows)
that define what a validated Home Assistant custom integration is, and of the audit
scripts they run. The sections below say what each workflow does, how a consumer calls
it, what the audit checks, and how a release of this repository reaches consumers.

## The three workflows

| Reusable workflow | Job name (the check-run) | What it does |
|---|---|---|
| `.github/workflows/python-validate.yml` | `Ruff, Pyright and Pytest` | `ruff check .` and `ruff format --check .` under the consumer's `pyproject.toml`, pyright on `custom_components/`, pytest on the Python floor. Warns when `tests/` is absent; fails when `tests/` exists without `requirements.test.txt`. |
| `.github/workflows/release.yml` | `Auto release zip` | On `release: published`: writes the tag into `manifest.json`, rebuilds the panel bundle when `frontend/` exists, zips `custom_components/<domain>` with the integration files at the zip root and attaches it to the release for HACS. The domain comes from the manifest. |
| `.github/workflows/quality-audit.yml` | `ha-integration conformance check` | Runs `scripts/skill_audit.py --root .` and `scripts/version_sync.py --root .` from this repository's checkout against the consumer. |

Every one of them is `on: workflow_call:` only. None declares a `secrets:` block: the caller's `GITHUB_TOKEN`
reaches a called workflow on its own, and `github.event` inside the called workflow is the
caller's event, so `release.yml` reads the release tag exactly as a copied body did. Every
step runs against the consumer's own checkout.

### Implementation notes

- **python-validate.yml**'s `python-version` is a scalar rather than a matrix: a matrix
  would rename the check-run to `lint-and-type (3.14)` and the ruleset's required context
  would never report. It installs with `uv`, which resolves the HA test stack —
  `homeassistant` plus `pytest-homeassistant-custom-component` pull several hundred
  packages — in seconds where pip takes minutes; the cache key covers
  `requirements.test.txt` and `pyproject.toml`, the files that actually decide the
  dependency set, so a key over the whole repo would re-resolve on any unrelated change
  and a narrower one would serve a stale environment after an HA bump. Ruff runs over the
  whole repository — Home Assistant core's own rule set, not `custom_components/` alone —
  so nothing beside the integration rots unseen, and the format check keeps the tree
  exactly as `ruff format` leaves it, so no file ever needs a formatter exclusion.
- **quality-audit.yml** sets up the Python floor before running the scripts because the
  runner's own `python3` predates their syntax and once rejected it; that interpreter has
  no `pyyaml` preinstalled the way the runner's system python did, so it installs it.
  `GH_TOKEN` lets three checks query GitHub — required contexts, the live ruleset and the
  dependency graph — instead of reporting NOT CHECKED; a required context with no
  producing job once blocked a PR here while CI stayed green.
- **release.yml** patches the manifest because HACS installs the asset built here, so the
  manifest inside the zip is what users get; `frenck/spook` patches the same way, from the
  same event, and the committed manifest value is a placeholder between releases —
  overriding a bump stays as simple as tagging what you want. It rebuilds the panel bundle
  immediately before packing rather than in its own workflow, because two workflows on the
  same `release: published` event cannot be ordered and a separate rebuild could finish
  after the zip was already packed, shipping the stale bundle it was meant to prevent. The
  stale-bundle notice is reported, not enforced,
  since the zip carries a fresh build either way, so a stale committed bundle is a
  repo-hygiene problem, not a release defect. The domain is read from the manifest rather
  than typed because an unsubstituted `<domain>` placeholder in a `run:` block is a bash
  redirect, and a published release once died on exactly that with no asset attached.

## Calling the workflows

An integration carries one caller workflow per reusable workflow in its own
`.github/workflows/`, under the same filename: the trigger and permissions around a
single job that `uses:` the workflow here. These three are the callers, complete:

```yaml
# .github/workflows/python-validate.yml
name: Python Validate

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  validate:
    uses: PineappleEmperor/ha-integration-ci/.github/workflows/python-validate.yml@{{sha}} # {{tag}}
```

```yaml
# .github/workflows/quality-audit.yml
name: Skill Audit

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  audit:
    uses: PineappleEmperor/ha-integration-ci/.github/workflows/quality-audit.yml@{{sha}} # {{tag}}
```

```yaml
# .github/workflows/release.yml
name: Create Release ZIP

on:
  release:
    types: [published]

permissions:
  contents: write

jobs:
  release:
    uses: PineappleEmperor/ha-integration-ci/.github/workflows/release.yml@{{sha}} # {{tag}}
```

`{{tag}}` and `{{sha}}` resolve as release-flow's README says under Calling the
workflows, against this repository:

```
TAG=$(gh api repos/PineappleEmperor/ha-integration-ci/releases/latest --jq .tag_name)
SHA=$(gh api "repos/PineappleEmperor/ha-integration-ci/commits/$TAG" --jq .sha)
```

The release caller grants `contents: write` because the called workflow uploads the zip
to the release; a called workflow can only narrow what its caller grants.

Named by GitHub's rule for called workflows (release-flow's README, Check names), the
job ids above give a consumer's ruleset these contexts:

| Caller job | Check-run name | Required context? |
|---|---|---|
| `validate` | `validate / Ruff, Pyright and Pytest` | yes |
| `audit` | `audit / ha-integration conformance check` | yes |
| `release` | `release / Auto release zip` | no, it runs on publish |

`check_required_contexts_have_producers` understands a caller job as the prefix it
produces.

## What the audit checks now

`python3 scripts/skill_audit.py --list` prints the registry; the list below is the shape
of it, not a substitute.

- **The callers.** `check_canonical_files` requires `pr-checks.yml`, `lint-pr.yml`,
  `python-validate.yml`, `quality-audit.yml`, `dependency-review.yml` and
  `release-drafter.yml` in every repo, with `.github/release-drafter.yml`,
  `.github/dependabot.yml` and `.gitignore` beside them; `hacs-validate.yml`,
  `hassfest-validate.yml` and `release.yml` in an integration; `panel-bundle.yml` once
  `frontend/package.json` exists; and refuses the superseded `frontend_build.yml`.
  `check_callers` requires each caller's `uses:` to name the repository and workflow
  path it stands for (`release-flow` for `pr-checks`, `lint-pr`, `release-drafter` and
  `auto-draft-pr`, this repository for `python-validate`, `quality-audit` and `release`,
  `ha-panel-ci` for `panel-bundle`) and to carry no body; a file whose only trigger is
  `workflow_call` is a body in the repository that owns it, not a copy, and is skipped.
  `check_action_pins` holds every `uses:` line, caller or step, to a 40-hex SHA with a
  version comment, so a `{{sha}}` copied unresolved from a README fails. `dependency-review`, HACS and hassfest are settings over a third-party
  action and stay plain files; nothing of theirs is versioned by a repository of ours.
- **Whatever workflow bodies the consumer still carries.** No `<placeholder>` in a `run:`,
  `with:` or `env:` value; no `${{ }}` inside a `run:`; a `setup-python` step before any
  step that runs Python, per job; one writer of the release body; `pull_request_target`
  on `pr-checks.yml`; no second labeler; no unsanctioned `gh pr create`. A check that
  reads a body skips a caller, because the body it would read lives in the repository
  the caller names and is judged there; the checks that read triggers still apply to
  a caller, because the triggers are the caller's own.
- **The integration itself.** `PLATFORMS` names with no module beside them, deprecated
  APIs, bare `# type: ignore`, multi-line docstrings on functions and classes, the
  canonical `quality_scale.yaml` rule set, manifest honesty (`integration_type`,
  `issue_tracker`, `config_flow` with a `config_flow.py`), a `done` rule with no test
  behind it, the root `conftest.py`, `asyncio_mode = "auto"`, the pinned test harness,
  brand assets at the sizes HACS expects.
- **The drafter config, the ruleset and the GitHub side.** Title-only autolabeler rules,
  v7 `when:`-shaped categories, every required context in `ruleset.json` and in the live
  branch rules produced by some job (on the base branch or in the working tree, and the
  verdict says which), required status checks present at all, the dependency graph on
  when `dependency-review.yml` is carried, `RELEASE_TOKEN` or the App pair present when
  the draft-PR opener is.
- **The commit hook.** Present, executable, enforcing the Conventional Commit subject
  shape and rejecting editorialising subjects, and `core.hooksPath` pointing at it.

`version_sync.py` compares the Python version across every workflow that sets one up in
the consumer's own `.github/workflows/`, the three reusable workflows in the
`.ha-integration-ci/` checkout beside it, ruff's `target-version` and
`pyrightconfig.json`, and requires the test harness to be pinned. A consumer's own
workflows are callers and declare nothing, so the comparison that matters is its floor
against the CI it runs; this repository's own `ci.yml` is read by its own CI, not by a
consumer's.

## What the audit deliberately no longer checks

Everything that existed only because files were copied, deleted rather than kept:

- `check_self_diff`, which compared a skill repo's `.github/` against its own templates.
- `check_template_scripts_match`, which compared shipped scripts byte-for-byte with the
  copies a repo ran.
- `check_template_pins`, which compared action pins in templates against Dependabot-bumped
  copies.
- `check_scripts_present` and `check_scripts_wired`, which required the copied
  `scripts/*.py` and a workflow step running each; the scripts are run from this
  repository's checkout now, and a consumer's `scripts/` is its own business.
- The template branches of `check_no_placeholders`, `check_python_steps_have_a_setup` and
  `check_required_contexts_have_producers`, which walked `plugins/*/skills/*/templates/`.
  The consumer-workflow branches stay.

There is no `templates/` directory to walk and no `_template_dir` helper.

## The version model

- **A tag is a version.** GitHub versions repositories, not files, so a release of this
  repository is a release of all three workflows and both scripts together, even when
  only one moved. Tags are `vX.Y.Z`.
- **Consumers pin a SHA and say which tag it is.** `@<sha> # vX.Y.Z`, the shape every
  pinned action already uses. A tag is mutable and a SHA is not; the comment is what a
  reader sees.
- **Dependabot moves the pin.** A consumer's existing weekly grouped `github-actions`
  update bumps the SHA and the version comment together, so a CI release arrives at every
  integration as the same PR it already receives for its other actions, and goes green
  with no hand edit. Nothing is copied in that PR.
- **A release is held for three days before it is offered.** Dependabot resolves the pinned
  SHA to its tag, sees the newer release, and then filters it: `Days since release : 0
  (cooldown days 3)`, `All versions are in cooldown period, returning current version`. That
  default is invisible under a weekly schedule and is the whole delay under a daily one, so
  a consumer that wants a release the day it lands sets `cooldown: {default-days: 0}` in its
  `dependabot.yml`. Read from the testbed's own update job after `v1.0.1` of release-flow.
- **The scripts ride the same pin.** `quality-audit.yml` checks this repository out at
  `github.job_workflow_sha`, the commit of the reusable workflow that is running, so a
  consumer can never run one release's workflow with another release's audit.
- **No repo runs integration workflows on itself.** This repository's own CI is Working
  on this repository, below. A release of the reusable workflows is proven on the testbed
  integration before it is tagged.

## This repository's own PR gate

This repository is a consumer of
[PineappleEmperor/release-flow](https://github.com/PineappleEmperor/release-flow) like
any other: its `.github/workflows/` carries release-flow's four callers, its
`.github/release-drafter.yml` and `.githooks/commit-msg` are the copies that README
lists, and `RELEASE_TOKEN` is set. `ci.yml` is its own.

## Working on this repository

```
ruff check .
ruff format --check .
python -m pytest tests/ -q
```

`ci.yml` runs the same three commands above, plus `python scripts/version_sync.py --root
.`. It tests the scripts under the same ruff tables and Python floor a consumer runs,
since the scripts execute inside every consumer's quality-audit job. It installs `pyyaml`
because `skill_audit.py` parses workflows and the tests import it — a local venv that
already has it installed would hide a missing dependency, so the job names every import
the suite reaches.

Every check in `scripts/skill_audit.py` is a function returning `(failures, warnings)`
and has a test in `tests/test_skill_audit.py`. A changed check gets its test changed
first and seen to fail before the check moves; a check you have not seen fail is not a
check.
