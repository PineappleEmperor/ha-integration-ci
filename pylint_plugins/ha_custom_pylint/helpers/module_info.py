"""Helpers for parsing integration module paths."""

from dataclasses import dataclass
import importlib.machinery
from pathlib import Path
import re
import sys

import astroid
from astroid import nodes

_INTEGRATION_ROOT = "homeassistant.components"
_INTEGRATION_ROOT_DOT = f"{_INTEGRATION_ROOT}."
_INTEGRATION_TEST_ROOT = "tests.components"
_INTEGRATION_TEST_ROOT_DOT = f"{_INTEGRATION_TEST_ROOT}."
_ROOT_SEGMENT_COUNT = _INTEGRATION_ROOT.count(".") + 1
_MODULE_REGEX: re.Pattern[str] = re.compile(
    rf"^{re.escape(_INTEGRATION_ROOT)}\.\w+(\.\w+)?$"
)

# ha_custom_pylint: a custom integration is named ``custom_components.<domain>``
# when imported from the repository root, and a bare ``<domain>`` when pylint
# lints ``custom_components/<domain>``, because ``custom_components/`` has no
# ``__init__.py``. The bare form counts only when ``<domain>`` resolves to a
# ``custom_components/<domain>/`` directory holding a ``manifest.json``.
_CUSTOM_ROOT = "custom_components"
_CUSTOM_ROOT_DOT = f"{_CUSTOM_ROOT}."
_custom_domains: dict[str, bool] = {}
_custom_domains_path: list[str] = []


def _find_custom_domain(domain: str) -> bool:
    """Return True if *domain* resolves to ``custom_components/<domain>/``."""
    if not domain.isidentifier():
        return False
    try:
        spec = importlib.machinery.PathFinder.find_spec(domain)
    except ImportError, ValueError:
        return False
    if spec is None or not spec.submodule_search_locations:
        return False
    return any(
        Path(location).parent.name == _CUSTOM_ROOT
        and (Path(location) / "manifest.json").is_file()
        for location in spec.submodule_search_locations
    )


def is_custom_domain(domain: str) -> bool:
    """Return True if a bare *domain* is a custom integration on ``sys.path``.

    The answer depends on ``sys.path``, which pylint extends for the files it
    lints, so the cache is dropped whenever ``sys.path`` changes.
    """
    if sys.path != _custom_domains_path:
        _custom_domains.clear()
        _custom_domains_path[:] = sys.path
    if domain not in _custom_domains:
        _custom_domains[domain] = _find_custom_domain(domain)
    return _custom_domains[domain]


@dataclass(frozen=True, slots=True)
class IntegrationModule:
    """Parsed integration module path."""

    root: str
    """The integration root, e.g. ``homeassistant.components``."""
    domain: str
    """The integration domain, e.g. ``hue``."""
    module: str | None
    """The sub-module name, e.g. ``sensor``, ``config_flow``, ``const``.

    ``None`` when the module is the integration's ``__init__``.
    """


def parse_module(
    module_name: str, *, include_test: bool = False
) -> IntegrationModule | None:
    """Parse a dotted module name into integration parts.

    Returns ``None`` if *module_name* is not under the integration root.
    For deep sub-modules (e.g. ``homeassistant.components.hue.light.v2``),
    ``module`` is set to the first segment after the domain (``light``).
    """
    segment_count = _ROOT_SEGMENT_COUNT
    if module_name.startswith(_INTEGRATION_ROOT_DOT):
        root = _INTEGRATION_ROOT
    elif include_test and module_name.startswith(_INTEGRATION_TEST_ROOT_DOT):
        root = _INTEGRATION_TEST_ROOT
    # ha_custom_pylint: the two names a custom integration's modules carry.
    elif module_name.startswith(_CUSTOM_ROOT_DOT):
        root = _CUSTOM_ROOT
        segment_count = 1
    elif is_custom_domain(module_name.partition(".")[0]):
        root = _CUSTOM_ROOT
        segment_count = 0
    else:
        return None

    parts = module_name.split(".")
    n = len(parts)
    if n < segment_count + 1:
        return None
    if n == segment_count + 1:
        return IntegrationModule(
            root=root,
            domain=parts[segment_count],
            module=None,
        )
    # n >= segment_count + 2: domain.module[.submodule...]
    return IntegrationModule(
        root=root,
        domain=parts[segment_count],
        module=parts[segment_count + 1],
    )


def is_integration_module(module_name: str) -> bool:
    """Return True if *module_name* is under the integration root."""
    if module_name.startswith(_INTEGRATION_ROOT_DOT):
        return True
    # ha_custom_pylint: or under a custom integration's root.
    parsed = parse_module(module_name)
    return parsed is not None and parsed.root == _CUSTOM_ROOT


def get_module_platform(module_name: str) -> str | None:
    """Return the platform for the module name.

    Returns ``"__init__"`` for the integration's root module,
    the platform name for a sub-module, or ``None`` if not matched.
    """
    if not (module_match := _MODULE_REGEX.match(module_name)):
        # ha_custom_pylint: the same two shapes under a custom integration.
        parsed = parse_module(module_name)
        if parsed is None or parsed.root != _CUSTOM_ROOT:
            return None
        depth = len(module_name.removeprefix(_CUSTOM_ROOT_DOT).split("."))
        if depth > 2:
            return None
        return parsed.module or "__init__"
    platform = module_match.group(1)
    return platform.lstrip(".") if platform else "__init__"


def is_test_module(module_name: str) -> bool:
    """Return True if *module_name* is a test module."""
    return module_name.startswith("tests.")


def parse_import_source(name: nodes.Name, imported: str) -> IntegrationModule | None:
    """Parse the integration module that *name* is ``imported`` from, if any.

    ha_custom_pylint: core's direct-call checkers match the callee's own name,
    which an alias changes. This reads the ``from ... import`` that binds
    *name* and, when it imports *imported*, parses that import's source.
    """
    try:
        _, assignments = name.lookup(name.name)
    except astroid.exceptions.AstroidError:
        return None
    for assignment in assignments:
        if not isinstance(assignment, nodes.ImportFrom):
            continue
        for original, alias in assignment.names:
            if original != imported or (alias or original) != name.name:
                continue
            modname = assignment.modname
            if assignment.level:
                try:
                    modname = assignment.root().relative_to_absolute_name(
                        modname, assignment.level
                    )
                except astroid.exceptions.AstroidError:
                    continue
            if (parsed := parse_module(modname)) is not None:
                return parsed
    return None
