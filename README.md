# ha-integration-ci

The single home of the three [reusable workflows](https://docs.github.com/en/actions/using-workflows/reusing-workflows)
that define what a validated Home Assistant custom integration is, and of the audit
scripts they run. The sections below say what each workflow does, how a consumer calls
it, what the audit checks, and how a release of this repository reaches consumers.

## The three workflows

| Reusable workflow | Job name (the check-run) | What it does |
|---|---|---|
| `.github/workflows/python-validate.yml` | `Python validation` | `ruff check .` and `ruff format --check .` under the consumer's `pyproject.toml`, `mypy --config-file mypy.ini custom_components/`, the consumer's `.pre-commit-config.yaml` hooks, core's pylint rules on `custom_components/` and on `tests/` when it exists, pytest with a 9-second per-test timeout and core's translation check on the Python floor, then `scripts/coverage_gate.py` on the coverage it measured. Warns when `tests/` is absent; fails when `tests/` exists without `requirements.test.txt`. |
| `.github/workflows/release.yml` | `Auto release zip` | On `release: published`: writes the tag into `manifest.json`, rebuilds the panel bundle when `frontend/` exists, zips `custom_components/<domain>` with the integration files at the zip root and attaches it to the release as `<domain>.zip`, the name a consumer's `hacs.json` must carry as `filename` for HACS to download it. The domain comes from the manifest. |
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
  Ruff, mypy and pylint are pinned to 0.16.3, 2.3.1 and 4.0.7, with pylint's astroid at
  4.0.4, the versions Home Assistant core 2026.9.0 pins, because the consumer's rule set,
  `mypy.ini` and pylint rules are derived from core's and a newer tool reports errors
  core has not met yet. Dependabot does not read a
  `run:` line, so the pins move by hand when core's `requirements_test.txt` and
  `requirements_test_pre_commit.txt` move them. Pytest's `--timeout=9` is core's per-test
  limit, so a hung test fails in seconds rather than at the job timeout; the flag comes
  from `pytest-timeout`, which the pinned test harness brings. The job
  name says what the job is for rather than which tools it runs, because it is half of
  every consumer's required context: swapping a tool must not rename the check.
- **The translation check** is `pytest_plugins/ha_translations.py`, a port of the autouse
  `check_translations` fixture in core's `tests/components/conftest.py` at 2026.9.0.
  pytest-homeassistant-custom-component copies core's root `tests/conftest.py` but not that
  file, so without the port a flow error, abort, repair issue, action or action exception
  whose text is missing from `translations/en.json` passes here and fails in core; a user
  then sees the raw key. The port changes three things only: the quality scale is read
  beside the loaded integration rather than at a core path, the message names
  `translations/en.json`, and a service a test registers is recognised by the consumer's
  own `tests/` directory. It keeps core's `ignore_missing_translations` and
  `ignore_translations_for_mock_domains` fixtures for a test to override. python-validate
  checks this repository out at `github.job_workflow_sha`, as quality-audit does, and loads
  the plugin with `-p ha_translations`; the checkout comes after ruff and mypy so neither
  lints it. A local run gets the same check with
  `PYTHONPATH=<a clone of this repository>/pytest_plugins pytest -p ha_translations`.
- **The coverage gate** is `scripts/coverage_gate.py`, run on the JSON report pytest-cov
  writes. It fails any line of `config_flow.py` or `diagnostics.py` that no test ran, and
  either module no test imported: those are the modules core's `codecov.yml` holds to 100%,
  since the setup screens and the support dump are what a user meets when something is
  wrong. Other modules carry no threshold here, as core's patch target does not bind them.
  pytest-cov arrives with the pinned test harness.
- **The pylint rules** are `pylint_plugins/ha_custom_pylint`, our own copy of the plugin
  in core's `pylint/plugins`, taken at 2026.9.0 and synced to 2026.9.4, the release
  consumers test against, under Apache-2.0 with its `NOTICE`. Core's
  plugin decides what an integration is by the module name `homeassistant.components.<domain>`,
  which a custom integration never has, so run as published it stays silent on most of
  one, and its README says it is not for external use. The copy keeps core's checkers,
  message ids and symbols, so a diff against core stays readable, and changes only what
  `NOTICE` lists file by file: the name gate also accepts `custom_components.<domain>` and
  the bare `<domain>` pylint uses when it lints `custom_components/<domain>` from the
  repository root, where a `manifest.json` beside it confirms the integration; a
  repo-root `tests/` owns the one integration beside it; W7418, W7420, W7421, W7422 and
  W7426 match a called name on the import that binds it, so an alias no longer evades
  them; and R7403 reads
  `tests/conftest.py` and the root `conftest.py`. python-validate runs it after the
  checkout, with every other pylint check off, and fails on any hit. The ids it enables
  come from `UPSTREAM.json`, which records for each one the core file it came from and
  that file's sha256 at the tag. R7402 is on although core's own config disables it while
  core clears old violations, since a new repository has none. On each core release,
  `python scripts/pylint_upstream.py --tag <core tag>` downloads core's plugin at that tag
  and names every carried message whose core file changed, moved or vanished, every other
  core file the copy carries that changed or vanished, and every message id core added
  or removed; it exits 1 when anything did. Port each named file's change into the copy,
  give each new id an entry under `carried` or `skipped`, with a reason, in
  `UPSTREAM.json`, then run it again with `--write` to record the tag and its hashes;
  `--write` refuses while an id is untriaged. The set is every message core defines at
  2026.9.4 but one:

  | Id | Symbol | Status |
  |---|---|---|
  | C7401 | `home-assistant-logger-period` | carried |
  | C7402 | `home-assistant-logger-capital` | carried |
  | C7403 | `home-assistant-relative-import` | carried |
  | C7404 | `home-assistant-absolute-import` | skipped: ruff's TID252 already bans a relative import that climbs out of the integration |
  | C7405 | `home-assistant-component-root-import` | carried |
  | C7406 | `home-assistant-helper-namespace-import` | carried |
  | C7407 | `home-assistant-import-constant-alias` | carried |
  | C7408 | `home-assistant-import-constant-unnecessary-alias` | carried |
  | C7409 | `home-assistant-enforce-sorted-platforms` | carried |
  | C7410 | `home-assistant-enforce-greek-micro-char` | carried |
  | C7411 | `home-assistant-enforce-class-module` | carried |
  | C7412 | `home-assistant-entity-description-redundant-default` | carried |
  | C7413 | `home-assistant-duplicate-const` | carried |
  | C7414 | `home-assistant-enforce-utcnow` | carried |
  | C7415 | `home-assistant-domain-argument` | carried |
  | C7425 | `home-assistant-enforce-now` | carried |
  | C7427 | `home-assistant-enforce-naive-now` | carried |
  | E7401 | `home-assistant-invalid-inheritance` | carried |
  | E7402 | `home-assistant-argument-type` | carried |
  | E7403 | `home-assistant-return-type` | carried |
  | E7404 | `home-assistant-missing-super-call` | carried |
  | E7405 | `home-assistant-action-swallowed-exception` | carried |
  | E7406 | `home-assistant-exception-translation-key-missing` | carried |
  | E7408 | `home-assistant-exception-translation-key-domain-mismatch` | carried |
  | E7409 | `home-assistant-mdi-icon-not-found` | carried |
  | E7410 | `home-assistant-mdi-icon-json-not-found` | carried |
  | E7418 | `home-assistant-exception-placeholder-mismatch` | carried |
  | R7401 | `home-assistant-consider-usefixtures-decorator` | carried |
  | R7402 | `home-assistant-unused-test-fixture-argument` | carried |
  | R7403 | `home-assistant-tests-redundant-usefixtures` | carried |
  | R7404 | `home-assistant-tests-registry-fixtures` | carried |
  | W7401 | `home-assistant-deprecated-import` | carried |
  | W7402 | `home-assistant-async-callback-decorator` | carried |
  | W7403 | `home-assistant-pytest-fixture-decorator` | carried |
  | W7404 | `home-assistant-async-load-fixtures` | carried |
  | W7405 | `home-assistant-use-runtime-data` | carried |
  | W7406 | `home-assistant-unique-id-ip-based` | carried |
  | W7407 | `home-assistant-config-flow-polling-field` | carried |
  | W7408 | `home-assistant-config-flow-name-field` | carried |
  | W7409 | `home-assistant-test-non-deterministic` | carried |
  | W7410 | `home-assistant-missing-reauthentication-flow` | carried |
  | W7411 | `home-assistant-missing-parallel-updates` | carried |
  | W7412 | `home-assistant-missing-diagnostics` | carried |
  | W7413 | `home-assistant-missing-config-entry-unloading` | carried |
  | W7414 | `home-assistant-service-registered-in-setup-entry` | carried |
  | W7415 | `home-assistant-sequential-executor-jobs` | carried |
  | W7416 | `home-assistant-missing-has-entity-name` | carried |
  | W7417 | `home-assistant-exception-not-translated` | carried |
  | W7418 | `home-assistant-tests-direct-async-setup-entry` | carried |
  | W7419 | `home-assistant-exception-message-with-translation` | carried |
  | W7420 | `home-assistant-tests-direct-platform-async-setup-entry` | carried |
  | W7421 | `home-assistant-tests-direct-async-migrate-entry` | carried |
  | W7422 | `home-assistant-tests-direct-async-setup` | carried |
  | W7423 | `home-assistant-missing-entity-unique-id` | carried |
  | W7424 | `home-assistant-entity-unique-id-static` | carried |
  | W7425 | `home-assistant-entity-unique-id-redundant-domain` | carried |
  | W7426 | `home-assistant-tests-direct-async-unload-entry` | carried |
  | W7427 | `home-assistant-entity-unique-id-redundant-platform` | carried |
  | W7428 | `home-assistant-config-flow-field-not-translated` | carried |
  | W7429 | `home-assistant-unnecessary-format-mac` | carried |
  | W7430 | `home-assistant-serial-port-selector-usb-dependency` | carried |
  | W7431 | `home-assistant-options-flow-field-not-translated` | carried |
  | W7432 | `home-assistant-subentry-flow-field-not-translated` | carried |
  | W7433 | `home-assistant-missing-test-before-configure` | carried |

- **The pre-commit hooks** run in CI because a hook that runs only on a developer's commit
  is skipped by `git commit -n` and by any edit made on GitHub; core runs its own through
  `prek` in CI for the same reason, and the step uses the prek action core pins. The hooks
  are the consumer's `.pre-commit-config.yaml` — codespell, `check-json`, yamllint and the
  JSON-sorting prettier — so a typo in `strings.json` or an unsorted manifest fails here
  and not first in review. `PREK_SKIP` drops `no-commit-to-branch`, which would fail every
  push to `main` as core's CI also skips it, and the two ruff hooks, which the pinned ruff
  step already runs.
- **quality-audit.yml** sets up the Python floor before running the scripts because the
  runner's own `python3` predates their syntax and once rejected it; that interpreter has
  no `pyyaml` preinstalled the way the runner's system python did, so it installs it.
  `GH_TOKEN` lets the checks that ask GitHub answer instead of downgrading, as What the
  audit checks now says they do without it; a required context with no producing job
  once blocked a PR here while CI stayed green.
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
| `validate` | `validate / Python validation` | yes |
| `audit` | `audit / ha-integration conformance check` | yes |
| `release` | `release / Auto release zip` | no, it runs on publish |

`check_required_contexts_have_producers` knows a caller pinned at the commit the audit
itself runs from by the job names in that checkout, so a ruleset still naming a job from
before a rename fails. Any other caller, of this repository at another commit or of
another repository, is known by the prefix it produces, since the checkout holds one
release only. A local run that can see the base branch therefore lets the old name
through on the bump PR itself, while the base still pins the old release; CI checks the
consumer out at depth 1, sees no base, and catches it.

## What the audit checks now

`python3 scripts/skill_audit.py --list` prints the registry; the list below is the shape
of it, not a substitute.

- **The callers.** `check_canonical_files` requires `pr-checks.yml`, `lint-pr.yml`,
  `python-validate.yml`, `quality-audit.yml`, `dependency-review.yml` and
  `release-drafter.yml` in every repo, with `.github/release-drafter.yml`,
  `.github/dependabot.yml`, `.gitignore` and `.pre-commit-config.yaml` beside them;
  `hacs-validate.yml`,
  `hassfest-validate.yml` and `release.yml` in an integration; `panel-bundle.yml` once
  `frontend/package.json` exists; and refuses the superseded `frontend_build.yml`.
  `check_callers` requires each caller's `uses:` to name the repository and workflow
  path it stands for (`release-flow` for `pr-checks`, `lint-pr`, `release-drafter` and
  `auto-draft-pr`, this repository for `python-validate`, `quality-audit` and `release`,
  `ha-panel-ci` for `panel-bundle`) and to carry no body; a file whose only trigger is
  `workflow_call` is a body in the repository that owns it, not a copy, and is skipped.
  `check_action_pins` holds every `uses:` line, caller or step, to a 40-hex SHA with a
  version comment, so a `{{sha}}` copied unresolved from a README fails; exempt are
  local `./` actions, which carry no ref, and `hacs/action` and
  `home-assistant/actions`, each of which documents a mutable ref. `dependency-review`,
  HACS and hassfest are plain files, not callers.
- **Whatever workflow bodies the consumer still carries.** No `<placeholder>` in a `run:`,
  `with:` or `env:` value; no `${{ }}` inside a `run:`; a `setup-python` step before any
  step that runs Python, per job; one writer of the release body; `pull_request_target`
  on `pr-checks.yml`; no second labeler; no `gh pr create` outside release-flow's draft
  opener, unless the workflow carries a `# skill-audit: sanctioned-opener` line (the
  check reads the marker; the reason it asks for beside it is for the reader). A check that
  reads a body skips a caller, because the body it would read lives in the repository
  the caller names and is judged there; the checks that read triggers still apply to
  a caller, because the triggers are the caller's own.
- **The integration itself.** `PLATFORMS` names with no module beside them, deprecated
  APIs, bare `# type: ignore`, multi-line docstrings on functions and classes under
  `custom_components/` (module docstrings are exempt, a missing docstring is not checked,
  and a consumer's own `scripts/` and `tests/` are not read), a tracked compiled artefact
  (a `.py[cod]` file or a `__pycache__`, per-interpreter bytes that churn every diff), the
  canonical `quality_scale.yaml` rule set and — since hassfest walks a claimed tier for
  core integrations only — every rule at or below the manifest's `quality_scale` marked
  `done` or `exempt`, manifest honesty (`integration_type`,
  `issue_tracker`, `config_flow` with a `config_flow.py`), a `done` rule with no `tests/`
  behind it, `test-coverage` marked `done` while a `frontend/` holds no `*.test.ts` or
  `*.spec.ts`, a `translations/en.json` that is not `strings.json` key for key or that
  carries a `[%key:…%]` reference (Home Assistant serves `en.json` as written, and only core
  resolves those references, at build time), the root `conftest.py`,
  `asyncio_mode = "auto"`, the pinned test harness (a
  warning when unpinned), a `mypy.ini` (a leftover `pyrightconfig.json` warns), a `home-assistant-frontend` pin in `requirements.test.txt` whenever the manifest
  depends on `frontend` or `panel_custom`, a `test` script in `frontend/package.json` (a
  warning), and `brand/icon.png`, the one brand file HACS gates a listing on — every other
  brand rule is quality and warns, a logo included, since the brands README says to ship
  only the icons where the two would be the same image.
- **The drafter config, the ruleset and the GitHub side.** Title-only autolabeler rules,
  v7 `when:`-shaped categories, every required context in `ruleset.json` and in the live
  branch rules produced by some job (on the base branch or in the working tree, and the
  verdict says which), required status checks present at all, the dependency graph on
  when `dependency-review.yml` is carried, `RELEASE_TOKEN` present when the draft-PR
  opener is — the one secret *The one secret* in release-flow's README describes, and
  nothing in its place. Without `gh`, or with a token that cannot answer, the required
  status checks, the live ruleset and the secret downgrade to a warning rather than a
  failure and the audit still exits green, so a clean run without `gh` is not evidence
  of the GitHub side. The dependency graph is the one exception, and only when `gh`
  answers: a probe that cannot read the graph is the failure the check exists to catch.
- **The commit hook.** Present, executable, enforcing the Conventional Commit subject
  shape and rejecting editorialising subjects, and `core.hooksPath` pointing at it.

`version_sync.py` compares the Python version across every workflow that sets one up in
the consumer's own `.github/workflows/`, the three reusable workflows in the
`.ha-integration-ci/` checkout beside it, ruff's `target-version` and the
`python_version` in `mypy.ini`'s `[mypy]` section, and requires the test harness to be pinned. A consumer's own
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
  with no hand edit. Nothing is copied in that PR. A major release is the exception: the
  edits its PR needs before it can go green are listed here, under its version.
- **v2.0.0's edits.** The required context `validate / Ruff, Pyright and Pytest` becomes
  `validate / Python validation` in `ruleset.json` and in the live ruleset, since the old
  name is never reported again and the bump PR waits on it forever; a `mypy.ini` joins
  the repository root, derived from core's; `pyrightconfig.json` goes. Core's
  `mypy.ini` disables `import-untyped` and fails an unused ignore, so a
  `# type: ignore[import-untyped]` the old audit asked for now fails and goes too. A
  repository without the skill's `.pre-commit-config.yaml` adds it, with the `.yamllint`,
  `.prettierrc.js` and `.prettierignore` it reads; `translations/en.json` becomes an exact
  copy of `strings.json` with every `[%key:…%]` written out; the tests reach every line of
  `config_flow.py` and `diagnostics.py`; every message the tests make a flow, repair
  issue or action show has its text in `translations/en.json`; and the integration and
  its tests pass the pylint rules.
- **A release is held for three days before it is offered.** Dependabot resolves the pinned
  SHA to its tag, sees the newer release, and then filters it: `Days since release : 0
  (cooldown days 3)`, `All versions are in cooldown period, returning current version`. That
  default applies with no `cooldown` key at all, is invisible under a weekly schedule and is
  the whole delay under a daily one. Read from the testbed's own update job after `v1.0.1`
  of release-flow. `default-days` cannot remove it, since GitHub documents cooldown days as
  1 to 90. `cooldown: {exclude: ["PineappleEmperor/*"]}` on the `github-actions` ecosystem
  does, for every action and workflow under that account, which in a caller means this
  stack's reusable workflows, because the updater treats an excluded dependency as never in
  cooldown; third-party actions and packages keep the hold, which GitHub added against
  malicious releases, and security updates skip it regardless.
- **The scripts ride the same pin.** `quality-audit.yml` checks this repository out at
  `github.job_workflow_sha`, the commit of the reusable workflow that is running, so a
  consumer can never run one release's workflow with another release's audit.
- **No repo runs integration workflows on itself.** This repository's own CI is Working
  on this repository, below. The testbed rule and the check that enforces it are
  `testbed-coverage.yml` under *The five workflows* in release-flow's README.

## This repository's own PR gate

This repository is a consumer of
[PineappleEmperor/release-flow](https://github.com/PineappleEmperor/release-flow) like
any other: its `.github/workflows/` carries release-flow's four callers, its
`.github/release-drafter.yml` and `.githooks/commit-msg` are the copies that README
lists, and `RELEASE_TOKEN` is set. `ci.yml` is its own.

It carries a fifth caller an integration does not, `testbed-coverage.yml`, described
under The five workflows in that README and given complete under Calling the workflows.

## Working on this repository

```
ruff check .
ruff format --check .
python -m pytest tests/ -q -p no:homeassistant
```

`ci.yml` runs the same three commands above, plus `python scripts/version_sync.py --root
.`. It tests the scripts under the same ruff tables and Python floor a consumer runs,
since the scripts execute inside every consumer's quality-audit job. It installs `pyyaml`
because `skill_audit.py` parses workflows and the tests import it — a local venv that
already has it installed would hide a missing dependency, so the job names every import
the suite reaches. It installs pytest-homeassistant-custom-component at the version the
skill's template pins, because `tests/test_ha_translations.py` runs a sample integration's
suite under the plugin in a child pytest; `-p no:homeassistant` keeps that harness out of
this suite's own run, whose sync tests its autouse async fixtures would break, and the
test module skips when the harness is absent. It installs pylint and astroid at
python-validate's pins, because `tests/test_ha_custom_pylint.py` lints sample
repositories with the pylint rules in a child pylint and skips without them.

Every check in `scripts/skill_audit.py` is a function returning `(failures, warnings)`
and has a test in `tests/test_skill_audit.py`. A changed check gets its test changed
first and seen to fail before the check moves; a check you have not seen fail is not a
check.
