"""Core's translation check, for a custom integration's test suite.

A port of the autouse `check_translations` fixture in Home Assistant core's
`tests/components/conftest.py` at 2026.9.0, which pytest-homeassistant-custom-component
does not ship. README.md says what it checks and how python-validate.yml loads it.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine, Mapping
from functools import lru_cache
import inspect
from pathlib import Path
import string
from typing import Any
from unittest.mock import patch

from homeassistant import components, loader
from homeassistant.components import repairs
from homeassistant.config_entries import (
    DISCOVERY_SOURCES,
    ConfigEntriesFlowManager,
    FlowResult,
    OptionsFlowManager,
)
from homeassistant.core import (
    Context,
    EntityServiceResponse,
    HassJobType,
    HomeAssistant,
    ServiceCall,
    ServiceRegistry,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.data_entry_flow import (
    FlowContext,
    FlowHandler,
    FlowManager,
    FlowResultType,
    section,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.typing import VolSchemaType
from homeassistant.util import yaml as yaml_util
import pytest
import voluptuous as vol


def _validate_translation_placeholders(
    full_key: str,
    translation: str,
    description_placeholders: Mapping[str, str] | None,
    translation_errors: dict[str, str],
) -> None:
    """Record a translation whose placeholders the caller does not supply."""
    for _, placeholder, _, _ in string.Formatter().parse(translation):
        if placeholder is None:
            continue
        if (
            description_placeholders is None
            or placeholder not in description_placeholders
        ):
            translation_errors[full_key] = (
                f"Description not found for placeholder `{placeholder}` in {full_key}"
            )


async def _validate_translation(
    hass: HomeAssistant,
    translation_errors: dict[str, str],
    ignore_translations_for_mock_domains: set[str],
    category: str,
    component: str,
    key: str,
    description_placeholders: Mapping[str, str] | None,
    *,
    translation_required: bool = True,
) -> None:
    """Record a translation that does not exist."""
    full_key = f"component.{component}.{category}.{key}"
    if component in ignore_translations_for_mock_domains:
        try:
            integration = await loader.async_get_integration(hass, component)
        except loader.IntegrationNotFound:
            return
        if not any(
            Path(f"{component_path}/{component}") == integration.file_path
            for component_path in components.__path__
        ):
            return
        translation_errors[full_key] = f"The integration '{component}' exists"
        return

    translations = await async_get_translations(hass, "en", category, [component])

    if full_key.endswith("."):
        for subkey, translation in translations.items():
            if subkey.startswith(full_key):
                _validate_translation_placeholders(
                    subkey, translation, description_placeholders, translation_errors
                )
        return
    if (translation := translations.get(full_key)) is not None:
        _validate_translation_placeholders(
            full_key, translation, description_placeholders, translation_errors
        )
        return

    if not translation_required:
        return

    if full_key not in translation_errors:
        for k in translation_errors:
            if k.endswith(".") and full_key.startswith(k):
                full_key = k
                break
    if translation_errors.get(full_key) in {"used", "unused"}:
        try:
            await loader.async_get_integration(hass, component)
        except loader.IntegrationNotFound:
            translation_errors[full_key] = (
                f"Translation not found for {component}: `{category}.{key}`. "
                f"The integration '{component}' does not exist."
            )
            return
        translation_errors[full_key] = "used"
        return

    translation_errors[full_key] = (
        f"Translation not found for {component}: `{category}.{key}`. "
        f"Please add to custom_components/{component}/translations/en.json"
    )


@pytest.fixture
def ignore_missing_translations() -> str | list[str]:
    """Ignore specific missing translations; override it to list them."""
    return []


@pytest.fixture
def ignore_translations_for_mock_domains() -> str | list[str]:
    """Skip validation for mocked integrations; override it to list their domains."""
    return []


@lru_cache
def _quality_scale_at(path: Path) -> dict[str, Any]:
    """The `rules:` mapping of one quality_scale.yaml, or empty when there is none."""
    try:
        return yaml_util.load_yaml_dict(str(path)).get("rules", {})
    except FileNotFoundError:
        return {}


def _rule_status(rules: Mapping[str, Any], rule: str) -> str:
    """A rule's status, `todo` when the file does not name it."""
    if rule not in rules:
        return "todo"
    status = rules[rule]
    return status if isinstance(status, str) else status["status"]


async def _integration_rule(hass: HomeAssistant, domain: str, rule: str) -> str:
    """A rule's status in the quality_scale.yaml beside the loaded integration."""
    try:
        integration = await loader.async_get_integration(hass, domain)
    except loader.IntegrationNotFound:
        return "todo"
    return _rule_status(
        _quality_scale_at(integration.file_path / "quality_scale.yaml"), rule
    )


def _repository_rule(request: pytest.FixtureRequest, rule: str) -> str:
    """A rule's status for the one integration this repository carries."""
    found = sorted(
        request.config.rootpath.glob("custom_components/*/quality_scale.yaml")
    )
    return _rule_status(_quality_scale_at(found[0]), rule) if found else "todo"


async def _check_step_or_section_translations(
    hass: HomeAssistant,
    translation_errors: dict[str, str],
    category: str,
    integration: str,
    translation_prefix: str,
    description_placeholders: Mapping[str, str] | None,
    data_schema: vol.Schema | None,
    ignore_translations_for_mock_domains: set[str],
) -> None:
    """Check a form step's headers, and each field unless it is a section."""
    for header in ("title", "description"):
        await _validate_translation(
            hass,
            translation_errors,
            ignore_translations_for_mock_domains,
            category,
            integration,
            f"{translation_prefix}.{header}",
            description_placeholders,
            translation_required=False,
        )

    if not data_schema:
        return

    for data_key, data_value in data_schema.schema.items():
        if isinstance(data_value, section):
            await _check_step_or_section_translations(
                hass,
                translation_errors,
                category,
                integration,
                f"{translation_prefix}.sections.{data_key}",
                description_placeholders,
                data_value.schema,
                ignore_translations_for_mock_domains,
            )
            continue
        iqs_config_flow = await _integration_rule(hass, integration, "config-flow")
        for header in ("data", "data_description"):
            await _validate_translation(
                hass,
                translation_errors,
                ignore_translations_for_mock_domains,
                category,
                integration,
                f"{translation_prefix}.{header}.{data_key}",
                description_placeholders,
                translation_required=(iqs_config_flow == "done"),
            )


async def _check_config_flow_result_translations(
    manager: FlowManager,
    flow: FlowHandler,
    result: FlowResult[FlowContext, str],
    translation_errors: dict[str, str],
    ignore_translations_for_mock_domains: set[str],
) -> None:
    """Check the strings a config, options or repair flow result shows."""
    if result["type"] is FlowResultType.CREATE_ENTRY:
        return

    key_prefix = ""
    description_placeholders = result.get("description_placeholders")
    if isinstance(manager, ConfigEntriesFlowManager):
        category = "config"
        integration = flow.handler
    elif isinstance(manager, OptionsFlowManager):
        category = "options"
        integration = flow.hass.config_entries.async_get_entry(flow.handler).domain
    elif isinstance(manager, repairs.RepairsFlowManager):
        category = "issues"
        integration = flow.handler
        issue_id = flow.issue_id
        issue = ir.async_get(flow.hass).async_get_issue(integration, issue_id)
        if issue is None:
            return
        key_prefix = f"{issue.translation_key}.fix_flow."
        description_placeholders = {
            **(issue.translation_placeholders or {}),
            **(description_placeholders or {}),
        }
    else:
        return

    setattr(flow, "__flow_seen_before", hasattr(flow, "__flow_seen_before"))

    if result["type"] is FlowResultType.FORM:
        if step_id := result.get("step_id"):
            await _check_step_or_section_translations(
                flow.hass,
                translation_errors,
                category,
                integration,
                f"{key_prefix}step.{step_id}",
                description_placeholders,
                result["data_schema"],
                ignore_translations_for_mock_domains,
            )

        if errors := result.get("errors"):
            for error in errors.values():
                await _validate_translation(
                    flow.hass,
                    translation_errors,
                    ignore_translations_for_mock_domains,
                    category,
                    integration,
                    f"{key_prefix}error.{error}",
                    description_placeholders,
                )
        return

    if result["type"] is FlowResultType.ABORT:
        if not getattr(flow, "__flow_seen_before") and flow.source in DISCOVERY_SOURCES:
            return
        if (abort_domain := result.get("translation_domain")) is not None:
            integration = abort_domain
        await _validate_translation(
            flow.hass,
            translation_errors,
            ignore_translations_for_mock_domains,
            category,
            integration,
            f"{key_prefix}abort.{result['reason']}",
            description_placeholders,
        )


async def _check_create_issue_translations(
    issue_registry: ir.IssueRegistry,
    issue: ir.IssueEntry,
    translation_errors: dict[str, str],
    ignore_translations_for_mock_domains: set[str],
) -> None:
    """Check a repair issue's title, and its description when it has no fix flow."""
    if issue.translation_key is None:
        return
    await _validate_translation(
        issue_registry.hass,
        translation_errors,
        ignore_translations_for_mock_domains,
        "issues",
        issue.domain,
        f"{issue.translation_key}.title",
        issue.translation_placeholders,
    )
    if (
        not issue.is_fixable
        and issue.translation_key
        not in ir.FRONTEND_HANDLED_ISSUES.get(issue.domain, ())
    ):
        await _validate_translation(
            issue_registry.hass,
            translation_errors,
            ignore_translations_for_mock_domains,
            "issues",
            issue.domain,
            f"{issue.translation_key}.description",
            issue.translation_placeholders,
        )


async def _check_exception_translation(
    hass: HomeAssistant,
    exception: HomeAssistantError,
    translation_errors: dict[str, str],
    request: pytest.FixtureRequest,
    ignore_translations_for_mock_domains: set[str],
) -> None:
    """Check the message an action's exception shows."""
    if exception.translation_key is None:
        if _repository_rule(request, "exception-translations") == "done":
            translation_errors["quality_scale"] = (
                f"Found untranslated {type(exception).__name__} exception: {exception}"
            )
        return
    await _validate_translation(
        hass,
        translation_errors,
        ignore_translations_for_mock_domains,
        "exceptions",
        exception.translation_domain,
        f"{exception.translation_key}.message",
        exception.translation_placeholders,
    )


_DYNAMIC_SERVICE_DOMAINS = {
    "esphome",
    "notify",
    "rest_command",
    "script",
    "shell_command",
    "tts",
}


async def _check_service_registration_translation(
    hass: HomeAssistant,
    domain: str,
    service_name: str,
    description_placeholders: Mapping[str, str] | None,
    translation_errors: dict[str, str],
    ignore_translations_for_mock_domains: set[str],
) -> None:
    """Check an action's placeholders, and its name and description."""
    await _validate_translation(
        hass,
        translation_errors,
        ignore_translations_for_mock_domains,
        "services",
        domain,
        f"{service_name}.",
        description_placeholders,
    )
    if domain not in _DYNAMIC_SERVICE_DOMAINS:
        for subkey in ("name", "description"):
            await _validate_translation(
                hass,
                translation_errors,
                ignore_translations_for_mock_domains,
                "services",
                domain,
                f"{service_name}.{subkey}",
                description_placeholders,
                translation_required=True,
            )


@pytest.fixture(autouse=True)
async def check_translations(
    ignore_missing_translations: str | list[str],
    ignore_translations_for_mock_domains: str | list[str],
    request: pytest.FixtureRequest,
) -> AsyncGenerator[None]:
    """Fail a test that shows a flow, issue, exception or action with no translation."""
    if not isinstance(ignore_missing_translations, list):
        ignore_missing_translations = [ignore_missing_translations]

    if not isinstance(ignore_translations_for_mock_domains, list):
        ignored_domains = {ignore_translations_for_mock_domains}
    else:
        ignored_domains = set(ignore_translations_for_mock_domains)

    translation_errors = dict.fromkeys(ignore_missing_translations, "unused")
    translation_coros = set()
    tests_dir = str(request.config.rootpath / "tests")

    # Wrapping HA's own step handler is the check, as it is in core.
    _original_flow_manager_async_handle_step = FlowManager._async_handle_step  # noqa: SLF001
    _original_issue_registry_async_create_issue = ir.IssueRegistry.async_get_or_create
    _original_service_registry_async_call = ServiceRegistry.async_call
    _original_service_registry_async_register = ServiceRegistry.async_register

    async def _flow_manager_async_handle_step(
        self: FlowManager, flow: FlowHandler, *args
    ) -> FlowResult:
        result = await _original_flow_manager_async_handle_step(self, flow, *args)
        await _check_config_flow_result_translations(
            self, flow, result, translation_errors, ignored_domains
        )
        return result

    def _issue_registry_async_create_issue(
        self: ir.IssueRegistry, domain: str, issue_id: str, *args, **kwargs
    ) -> ir.IssueEntry:
        result = _original_issue_registry_async_create_issue(
            self, domain, issue_id, *args, **kwargs
        )
        translation_coros.add(
            _check_create_issue_translations(
                self, result, translation_errors, ignored_domains
            )
        )
        return result

    async def _service_registry_async_call(
        self: ServiceRegistry,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        blocking: bool = False,
        context: Context | None = None,
        target: dict[str, Any] | None = None,
        return_response: bool = False,
    ) -> ServiceResponse:
        try:
            return await _original_service_registry_async_call(
                self,
                domain,
                service,
                service_data,
                blocking,
                context,
                target,
                return_response,
            )
        except HomeAssistantError as err:
            translation_coros.add(
                _check_exception_translation(
                    self._hass, err, translation_errors, request, ignored_domains
                )
            )
            raise

    @callback
    def _service_registry_async_register(
        self: ServiceRegistry,
        domain: str,
        service: str,
        service_func: Callable[
            [ServiceCall],
            Coroutine[Any, Any, ServiceResponse | EntityServiceResponse]
            | ServiceResponse
            | EntityServiceResponse
            | None,
        ],
        schema: VolSchemaType | None = None,
        supports_response: SupportsResponse = SupportsResponse.NONE,
        job_type: HassJobType | None = None,
        *,
        description_placeholders: Mapping[str, str] | None = None,
    ) -> None:
        # A service a test registers for itself is not the integration's to translate.
        if (
            (current_frame := inspect.currentframe()) is None
            or (caller := current_frame.f_back) is None
            or (
                caller.f_code.co_name != "async_mock_service"
                and not caller.f_code.co_filename.startswith(tests_dir)
            )
        ):
            translation_coros.add(
                _check_service_registration_translation(
                    self._hass,
                    domain,
                    service,
                    description_placeholders,
                    translation_errors,
                    ignored_domains,
                )
            )

        _original_service_registry_async_register(
            self,
            domain,
            service,
            service_func,
            schema,
            supports_response,
            job_type,
            description_placeholders=description_placeholders,
        )

    with (
        patch(
            "homeassistant.data_entry_flow.FlowManager._async_handle_step",
            _flow_manager_async_handle_step,
        ),
        patch(
            "homeassistant.helpers.issue_registry.IssueRegistry.async_get_or_create",
            _issue_registry_async_create_issue,
        ),
        patch(
            "homeassistant.core.ServiceRegistry.async_call",
            _service_registry_async_call,
        ),
        patch(
            "homeassistant.core.ServiceRegistry.async_register",
            _service_registry_async_register,
        ),
    ):
        yield

    await asyncio.gather(*translation_coros)

    unused_ignore = [k for k, v in translation_errors.items() if v == "unused"]
    if unused_ignore:
        pytest.fail(
            f"Unused ignore translations: {', '.join(unused_ignore)}. "
            "Please remove them from the ignore_missing_translations fixture."
        )
    for description in translation_errors.values():
        if description != "used":
            pytest.fail(description)
