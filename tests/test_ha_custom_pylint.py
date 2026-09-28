"""Tests for pylint_plugins/ha_custom_pylint, core's pylint rules on custom paths.

Two kinds. The end-to-end tests write throwaway repositories under tmp_path, with
``custom_components/<domain>/`` and a repo-root ``tests/``, and lint them in a child
pylint the way python-validate.yml does, with exactly the ids UPSTREAM.json carries:
every carried id must fire on a seeded violation, and clean code must draw nothing.
The unit tests walk one checker over a parsed snippet, as core's own tests in
``tests/pylint/`` do; the cases marked as ported are core's, respelled for a custom
integration's module names.
"""

from collections.abc import Iterator
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("pylint")
astroid = pytest.importorskip("astroid")
pytest.importorskip("homeassistant")

from pylint.checkers import BaseChecker  # noqa: E402
from pylint.testutils import UnittestLinter  # noqa: E402
from pylint.utils.ast_walker import ASTWalker  # noqa: E402

_PLUGINS = pathlib.Path(__file__).resolve().parents[1] / "pylint_plugins"
if str(_PLUGINS) not in sys.path:
    sys.path.insert(0, str(_PLUGINS))

from ha_custom_pylint.checkers.imports import HassImportsFormatChecker  # noqa: E402
from ha_custom_pylint.checkers.quality_scale.parallel_updates import (  # noqa: E402
    ParallelUpdatesChecker,
)
from ha_custom_pylint.checkers.tests.direct_async_setup_entry import (  # noqa: E402
    DirectAsyncSetupEntry,
)
from ha_custom_pylint.helpers import module_info  # noqa: E402
from ha_custom_pylint.helpers.integration import clear_caches  # noqa: E402
from ha_custom_pylint.helpers.quality_scale import (  # noqa: E402
    clear_quality_scale_cache,
)

_UPSTREAM = json.loads((_PLUGINS / "ha_custom_pylint" / "UPSTREAM.json").read_text())
CARRIED = sorted(_UPSTREAM["carried"])


def _write(root: pathlib.Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip())


# A clean integration: every quality-scale claim the plugin can check is made
# and kept, so the gated checkers run and find nothing.
_SAMPLE = {
    "custom_components/sample/manifest.json": """
        {"domain": "sample", "name": "Sample", "codeowners": [], "config_flow": true,
         "documentation": "https://example.invalid", "integration_type": "hub",
         "iot_class": "local_polling", "issue_tracker": "https://example.invalid",
         "requirements": [], "version": "0.0.0"}
        """,
    "custom_components/sample/quality_scale.yaml": """
        rules:
          config-entry-unloading: done
          diagnostics: done
          entity-unique-id: done
          exception-translations: done
          has-entity-name: done
          parallel-updates: done
          reauthentication-flow: done
          test-before-configure: done
        """,
    "custom_components/sample/icons.json": """
        {"services": {"ping": {"service": "mdi:thermometer"}}}
        """,
    "custom_components/sample/translations/en.json": """
        {"config": {"step": {"user": {"data": {"host": "Host"}}},
                    "error": {"cannot_connect": "Cannot connect"}},
         "entity": {"sensor": {"reading": {"name": "Reading"}}},
         "exceptions": {"no_ping": {"message": "Cannot ping {host}"}}}
        """,
    "custom_components/sample/const.py": '''
        """Constants for the sample integration."""

        DOMAIN = "sample"
        ''',
    "custom_components/sample/__init__.py": '''
        """The sample integration."""

        from homeassistant.config_entries import ConfigEntry
        from homeassistant.const import Platform
        from homeassistant.core import HomeAssistant, ServiceCall
        from homeassistant.exceptions import HomeAssistantError
        from homeassistant.helpers.typing import ConfigType

        from .const import DOMAIN

        PLATFORMS: list[Platform] = [Platform.SENSOR]

        type SampleConfigEntry = ConfigEntry[int]


        async def _async_ping(call: ServiceCall) -> None:
            """Refuse every ping."""
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_ping",
                translation_placeholders={"host": call.data["host"]},
            )


        async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
            """Register the actions."""
            hass.services.async_register(DOMAIN, "ping", _async_ping)
            return True


        async def async_setup_entry(hass: HomeAssistant, entry: SampleConfigEntry) -> bool:
            """Set up one entry."""
            entry.runtime_data = 1
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
            return True


        async def async_unload_entry(hass: HomeAssistant, entry: SampleConfigEntry) -> bool:
            """Unload one entry."""
            return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


        async def async_migrate_entry(hass: HomeAssistant, entry: SampleConfigEntry) -> bool:
            """Nothing to migrate yet."""
            return True
        ''',
    "custom_components/sample/sensor.py": '''
        """Sensor platform for the sample integration."""

        from homeassistant.components.sensor import SensorEntity
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

        from . import SampleConfigEntry

        PARALLEL_UPDATES = 0


        async def async_setup_entry(
            hass: HomeAssistant,
            entry: SampleConfigEntry,
            async_add_entities: AddConfigEntryEntitiesCallback,
        ) -> None:
            """Add the one sensor."""
            async_add_entities([SampleSensor(entry.entry_id)])


        class SampleSensor(SensorEntity):
            """The one sensor."""

            _attr_has_entity_name = True
            _attr_translation_key = "reading"

            def __init__(self, entry_id: str) -> None:
                """Key the sensor on its entry."""
                self._attr_unique_id = entry_id
        ''',
    "custom_components/sample/config_flow.py": '''
        """Config flow for the sample integration."""

        from collections.abc import Mapping
        from typing import Any

        import voluptuous as vol

        from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
        from homeassistant.const import CONF_HOST

        from .const import DOMAIN


        class SampleConfigFlow(ConfigFlow, domain=DOMAIN):
            """Ask for the host, and test it."""

            VERSION = 1

            async def async_step_user(
                self, user_input: dict[str, Any] | None = None
            ) -> ConfigFlowResult:
                """Ask for the host."""
                errors: dict[str, str] = {}
                if user_input is not None:
                    if user_input[CONF_HOST].startswith("bad"):
                        errors["base"] = "cannot_connect"
                    else:
                        await self.async_set_unique_id("serial-1")
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(title="Sample", data=user_input)
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
                    errors=errors,
                )

            async def async_step_reauth(
                self, entry_data: Mapping[str, Any]
            ) -> ConfigFlowResult:
                """Ask again."""
                return await self.async_step_user()
        ''',
    "custom_components/sample/diagnostics.py": '''
        """Diagnostics for the sample integration."""

        from typing import Any

        from homeassistant.core import HomeAssistant

        from . import SampleConfigEntry


        async def async_get_config_entry_diagnostics(
            hass: HomeAssistant, entry: SampleConfigEntry
        ) -> dict[str, Any]:
            """Return the entry's title."""
            return {"title": entry.title}
        ''',
}

_ROOT_CONFTEST = {
    "conftest.py": '''
        """Root fixtures."""

        import pytest


        @pytest.fixture(autouse=True)
        def root_autouse(enable_custom_integrations: None) -> None:
            """Enable the custom integration in every test."""
        ''',
}

_CLEAN_TESTS = {
    "tests/__init__.py": '"""Tests for the sample integration."""\n',
    "tests/conftest.py": '''
        """Fixtures for the sample tests."""

        import pytest
        from pytest_homeassistant_custom_component.common import MockConfigEntry

        from custom_components.sample.const import DOMAIN


        @pytest.fixture
        def mock_config_entry() -> MockConfigEntry:
            """An entry for the sample integration."""
            return MockConfigEntry(domain=DOMAIN, data={"host": "h"}, unique_id="serial-1")
        ''',
    "tests/test_init.py": '''
        """Tests for the sample integration's setup."""

        from pytest_homeassistant_custom_component.common import MockConfigEntry

        from homeassistant.config_entries import ConfigEntryState
        from homeassistant.core import HomeAssistant


        async def test_setup_and_unload(
            hass: HomeAssistant, mock_config_entry: MockConfigEntry
        ) -> None:
            """The entry loads through the pipeline and unloads again."""
            mock_config_entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
            assert mock_config_entry.state is ConfigEntryState.LOADED
            assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
        ''',
}

# Every test-side rule, seeded against the clean integration above.
_DIRTY_TESTS = {
    "tests/__init__.py": '"""Tests that break every test rule."""\n',
    "tests/conftest.py": '''
        """Fixtures."""

        import pytest


        @pytest.fixture(autouse=True)
        def tests_autouse() -> None:
            """Applied to every test by tests/conftest.py."""


        @pytest.fixture(scope="session")
        def session_thing() -> int:
            """Session-scoped without autouse: W7403."""
            return 1
        ''',
    "tests/test_dirty.py": '''
        """Tests that break every test rule."""

        import pytest
        from pytest_homeassistant_custom_component.common import (
            MockConfigEntry,
            load_fixture,
        )

        from homeassistant.const import Platform
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import entity_registry as er

        from custom_components.sample import (
            async_migrate_entry,
            async_setup,
            async_setup_entry,
            async_unload_entry,
        )
        from custom_components.sample.sensor import async_setup_entry as sensor_setup


        async def test_direct(hass: HomeAssistant, caplog, enable_custom_integrations: None):
            """Every direct call, and the rest of the test rules."""
            entry = MockConfigEntry(domain=Platform.SENSOR)
            await async_setup(hass, {})
            await async_setup_entry(hass, entry)
            await sensor_setup(hass, entry, None)
            await async_migrate_entry(hass, entry)
            await async_unload_entry(hass, entry)
            load_fixture("x.json")
            registry = er.async_get(hass)
            if registry:
                assert entry


        @pytest.mark.usefixtures("tests_autouse")
        async def test_redundant_flat(hass: HomeAssistant) -> None:
            """tests/conftest.py already applies it."""
            assert hass


        @pytest.mark.usefixtures("root_autouse")
        async def test_redundant_root(hass: HomeAssistant) -> None:
            """The root conftest.py already applies it."""
            assert hass
        ''',
}

# Every integration-side rule. Derived from the study's probe integration.
_DIRTY = {
    "custom_components/dirty/manifest.json": """
        {"domain": "dirty", "name": "Dirty", "codeowners": [], "config_flow": true,
         "documentation": "https://example.invalid", "integration_type": "hub",
         "iot_class": "local_polling", "issue_tracker": "https://example.invalid",
         "requirements": [], "version": "0.0.0"}
        """,
    "custom_components/dirty/quality_scale.yaml": """
        rules:
          config-entry-unloading: done
          diagnostics: done
          entity-unique-id: done
          exception-translations: done
          has-entity-name: done
          parallel-updates: done
          reauthentication-flow: done
          test-before-configure: done
        """,
    "custom_components/dirty/icons.json": """
        {"entity": {"sensor": {"temp": {"default": "mdi:definitely-not-an-icon"}}}}
        """,
    "custom_components/dirty/translations/en.json": """
        {"config": {"step": {"user": {"data": {"host": "Host"}}}},
         "options": {"step": {"init": {"data": {}}}},
         "exceptions": {"known": {"message": "Failed on {device}"}}}
        """,
    "custom_components/dirty/const.py": '''
        """Constants."""

        DOMAIN = "dirty"
        CONF_HOST = "host"
        UNIT = "µg/m³"
        ''',
    "custom_components/dirty/diagnostics.py": '"""Diagnostics with nothing in it."""\n',
    "custom_components/dirty/__init__.py": '''
        """Dirty integration."""

        from datetime import UTC, datetime
        from functools import cached_property
        import logging
        from zoneinfo import ZoneInfo

        from homeassistant.components.sensor import DOMAIN
        from homeassistant.components.sensor.const import SensorDeviceClass
        from homeassistant.config_entries import ConfigEntry
        from homeassistant.const import Platform
        from homeassistant.core import HomeAssistant, ServiceCall, callback
        from homeassistant.exceptions import HomeAssistantError
        from homeassistant.helpers.device_registry import (
            CONNECTION_NETWORK_MAC,
            DeviceInfo,
            async_get,
            format_mac,
        )

        from custom_components.dirty.const import CONF_HOST

        _LOGGER = logging.getLogger(__name__)

        PLATFORMS = [Platform.SWITCH, Platform.SENSOR]

        __all__ = ["CONF_HOST", "DOMAIN", "SensorDeviceClass", "async_get", "cached_property"]


        async def _handle(call: ServiceCall) -> None:
            """Handle the service."""
            try:
                await call.hass.async_add_executor_job(print)
            except OSError:
                _LOGGER.error("Service failed")


        async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
            """Set up."""
            hass.data[DOMAIN] = {}
            hass.services.async_register(DOMAIN, "do", _handle)
            _LOGGER.warning("setting up.")
            _LOGGER.info(
                "Now %s %s %s",
                datetime.now(UTC),
                datetime.now(),
                datetime.now(ZoneInfo("Europe/Paris")),
            )
            await hass.async_add_executor_job(print)
            await hass.async_add_executor_job(print)
            DeviceInfo(connections={(CONNECTION_NETWORK_MAC, format_mac("aa"))})
            if entry.data:
                raise HomeAssistantError("hard coded")
            if entry.options:
                raise HomeAssistantError(translation_key="known")
            if entry.title:
                raise HomeAssistantError(translation_domain="dirty", translation_key="missing")
            if entry.version:
                raise HomeAssistantError(
                    translation_domain="dirty",
                    translation_key="known",
                    translation_placeholders={"x": "y"},
                )
            if entry.minor_version:
                raise HomeAssistantError(
                    "msg", translation_domain="dirty", translation_key="known"
                )
            return True


        @callback
        async def bad_callback() -> None:
            """Async callback."""
        ''',
    "custom_components/dirty/config_flow.py": '''
        """Config flow."""

        from typing import Any

        import voluptuous as vol

        from homeassistant.config_entries import (
            ConfigEntry,
            ConfigFlow,
            ConfigFlowResult,
            ConfigSubentryFlow,
            OptionsFlow,
            SubentryFlowResult,
        )
        from homeassistant.const import CONF_HOST, CONF_NAME, CONF_SCAN_INTERVAL
        from homeassistant.core import callback
        from homeassistant.helpers.selector import SerialPortSelector

        from .const import DOMAIN


        class DirtyFlow(ConfigFlow, domain=DOMAIN):
            """Flow."""

            async def async_step_user(
                self, user_input: dict[str, Any] | None = None
            ) -> ConfigFlowResult:
                """User step."""
                if user_input is not None:
                    await self.async_set_unique_id(user_input[CONF_HOST])
                    return self.async_create_entry(title="x", data=user_input)
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_HOST): str,
                            vol.Required(CONF_NAME): str,
                            vol.Optional(CONF_SCAN_INTERVAL): int,
                            vol.Optional("port"): SerialPortSelector(),
                        }
                    ),
                )

            @classmethod
            @callback
            def async_get_supported_subentry_types(
                cls, config_entry: ConfigEntry
            ) -> dict[str, type[ConfigSubentryFlow]]:
                """One subentry type."""
                return {"thing": ThingFlow}


        class DirtyOptions(OptionsFlow):
            """Options."""

            async def async_step_init(
                self, user_input: dict[str, Any] | None = None
            ) -> ConfigFlowResult:
                """Init."""
                return self.async_show_form(
                    step_id="init", data_schema=vol.Schema({vol.Optional("level"): int})
                )


        class ThingFlow(ConfigSubentryFlow):
            """Subentry flow."""

            async def async_step_user(
                self, user_input: dict[str, Any] | None = None
            ) -> SubentryFlowResult:
                """User step."""
                return self.async_show_form(
                    step_id="user", data_schema=vol.Schema({vol.Required("colour"): str})
                )
        ''',
    "custom_components/dirty/sensor.py": '''
        """Sensor platform."""

        from homeassistant.components.binary_sensor import BinarySensorEntity
        from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
        from homeassistant.config_entries import ConfigEntry
        from homeassistant.helpers.restore_state import RestoreEntity
        from homeassistant.helpers.update_coordinator import CoordinatorEntity

        from .const import DOMAIN

        DESC = SensorEntityDescription(key="temp", entity_registry_enabled_default=True)
        ICON = "mdi:definitely-not-an-icon"
        MICRO = "µs"


        async def async_setup_entry(hass, entry: ConfigEntry, add):
            """Set up without hints."""


        class DirtySensor(SensorEntity, RestoreEntity):
            """No unique id, no has_entity_name."""

            async def async_added_to_hass(self) -> None:
                """Forget super."""
                self._attr_native_value = 1


        class StaticSensor(SensorEntity):
            """Static unique id."""

            _attr_has_entity_name = True
            _attr_unique_id = "static"


        class DomainSensor(SensorEntity):
            """Domain and platform in unique id."""

            _attr_has_entity_name = True

            def __init__(self, serial: str) -> None:
                """Init."""
                self._attr_unique_id = f"{DOMAIN}_sensor_{serial}"


        class WrongModule(BinarySensorEntity):
            """Binary sensor in sensor.py."""


        class CoordSensor(CoordinatorEntity, SensorEntity):
            """Forgets super on a coordinator entity."""

            _attr_has_entity_name = True
            _attr_unique_id = "x"

            async def async_added_to_hass(self) -> None:
                """Forget super."""
                self._attr_native_value = 2
        ''',
    "custom_components/dirty/switch.py": '''
        """Switch platform."""

        import logging
        from typing import Any

        from homeassistant.components.switch import SwitchEntity

        from .const import DOMAIN as OWN_DOMAIN

        _LOGGER = logging.getLogger(__name__)
        PARALLEL_UPDATES = 0


        class DirtySwitch(SwitchEntity):
            """Swallows."""

            _attr_has_entity_name = True
            _attr_unique_id = None
            _attr_name = OWN_DOMAIN

            async def async_turn_on(self, **kwargs: Any) -> None:
                """Turn on."""
                try:
                    await self.hass.async_add_executor_job(print)
                except OSError:
                    _LOGGER.error("Failed")
        ''',
}

# Where each carried id is seeded: the integration or the tests.
_TESTS_SIDE = {
    "C7415",
    "R7401",
    "R7402",
    "R7403",
    "R7404",
    "W7403",
    "W7404",
    "W7409",
    "W7418",
    "W7420",
    "W7421",
    "W7422",
    "W7426",
}


def _pylint(repo: pathlib.Path, target: str) -> list[dict]:
    """Lint *target* in *repo* as python-validate.yml does; return the messages."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pylint",
            "--load-plugins=ha_custom_pylint",
            "--disable=all",
            f"--enable={','.join(CARRIED)}",
            "--persistent=n",
            "--output-format=json",
            target,
        ],
        cwd=repo,
        env={**os.environ, "PYTHONPATH": str(_PLUGINS)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Traceback" not in result.stderr, result.stderr
    return json.loads(result.stdout or "[]")


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[dict]]:
    """The five pylint runs: dirty and clean, integration and tests."""
    dirty = tmp_path_factory.mktemp("dirty")
    _write(dirty, _DIRTY)
    tested = tmp_path_factory.mktemp("tested")
    _write(tested, _SAMPLE | _ROOT_CONFTEST | _DIRTY_TESTS)
    clean = tmp_path_factory.mktemp("clean")
    _write(clean, _SAMPLE | _ROOT_CONFTEST | _CLEAN_TESTS)
    return {
        "dirty integration": _pylint(dirty, "custom_components/"),
        "dirty tests": _pylint(tested, "tests/"),
        "clean integration": _pylint(tested, "custom_components/"),
        "clean tests": _pylint(clean, "tests/"),
        "clean both": _pylint(clean, "custom_components/"),
    }


def _ids(messages: list[dict]) -> set[str]:
    return {message["message-id"] for message in messages}


def test_the_carried_set_is_the_whole_plugin_but_the_skipped() -> None:
    """63 carried, C7404 skipped: the 64 messages core's plugin defines at 2026.9.0."""
    assert len(CARRIED) == 63
    assert set(_UPSTREAM["skipped"]) == {"C7404"}
    assert set(CARRIED) >= _TESTS_SIDE


@pytest.mark.parametrize("msg_id", CARRIED)
def test_every_carried_message_fires_on_its_seeded_violation(
    runs: dict[str, list[dict]], msg_id: str
) -> None:
    """Linted as custom_components/<domain> and tests/, with no shim."""
    run = "dirty tests" if msg_id in _TESTS_SIDE else "dirty integration"
    assert msg_id in _ids(runs[run])


@pytest.mark.parametrize("run", ["clean integration", "clean tests", "clean both"])
def test_clean_code_draws_nothing(runs: dict[str, list[dict]], run: str) -> None:
    """Every gated claim is made and kept, and the tests use the pipeline."""
    assert runs[run] == []


def test_the_integration_is_named_bare_and_the_tests_by_package(
    runs: dict[str, list[dict]],
) -> None:
    """The module names the gate has to accept, as pylint really produces them."""
    assert {m["module"] for m in runs["dirty integration"]} >= {
        "dirty",
        "dirty.sensor",
        "dirty.config_flow",
    }
    assert {m["module"] for m in runs["dirty tests"]} == {
        "tests.conftest",
        "tests.test_dirty",
    }


def test_an_aliased_platform_setup_import_is_caught(
    runs: dict[str, list[dict]],
) -> None:
    """Core matches the callee's name, so `as sensor_setup` evaded W7420."""
    lines = {m["line"] for m in runs["dirty tests"] if m["message-id"] == "W7420"}
    source = textwrap.dedent(_DIRTY_TESTS["tests/test_dirty.py"]).lstrip()
    call = source.splitlines().index("    await sensor_setup(hass, entry, None)")
    assert lines == {call + 1}


def test_redundant_usefixtures_reads_both_custom_conftests(
    runs: dict[str, list[dict]],
) -> None:
    """R7403 sees autouse fixtures in tests/conftest.py and the root conftest.py."""
    flagged = {
        m["message"].split("'")[1]
        for m in runs["dirty tests"]
        if m["message-id"] == "R7403"
    }
    assert flagged == {"tests_autouse", "root_autouse"}


def test_own_integration_imports_in_tests_are_not_root_imports(
    runs: dict[str, list[dict]],
) -> None:
    """Tests importing their own integration's modules are not C7405 or C7407."""
    assert not _ids(runs["dirty tests"]) & {"C7405", "C7407", "C7408"}


# Unit tests, in the shape of core's tests/pylint/.


def _walk(linter: UnittestLinter, checker: BaseChecker, node) -> None:
    walker = ASTWalker(linter)
    walker.add_checker(checker)
    walker.walk(node)


@pytest.fixture(name="linter")
def linter_fixture() -> UnittestLinter:
    """A linter that only collects messages."""
    clear_caches()
    clear_quality_scale_cache()
    return UnittestLinter()


@pytest.fixture(name="custom_root")
def custom_root_fixture(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    """A repository whose custom_components/ is on sys.path, as pylint puts it.

    ``pylint_test`` is an integration; ``nomanifest`` lacks a manifest.json;
    ``elsewhere`` has one but does not sit in custom_components/.
    """
    _write(
        tmp_path,
        {
            "custom_components/pylint_test/__init__.py": "",
            "custom_components/pylint_test/manifest.json": '{"domain": "pylint_test"}',
            "custom_components/pylint_test/quality_scale.yaml": "",
            "custom_components/nomanifest/__init__.py": "",
            "lib/elsewhere/__init__.py": "",
            "lib/elsewhere/manifest.json": '{"domain": "elsewhere"}',
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path / "custom_components"))
    monkeypatch.syspath_prepend(str(tmp_path / "lib"))
    return tmp_path


@pytest.mark.parametrize(
    ("module_name", "domain", "module", "platform"),
    [
        ("homeassistant.components.hue", "hue", None, "__init__"),
        ("homeassistant.components.hue.light", "hue", "light", "light"),
        ("custom_components.pylint_test", "pylint_test", None, "__init__"),
        ("custom_components.pylint_test.sensor", "pylint_test", "sensor", "sensor"),
        ("custom_components.pylint_test.api.hub", "pylint_test", "api", None),
        ("pylint_test", "pylint_test", None, "__init__"),
        ("pylint_test.sensor", "pylint_test", "sensor", "sensor"),
        ("pylint_test.api.hub", "pylint_test", "api", None),
    ],
)
@pytest.mark.usefixtures("custom_root")
def test_the_gate_accepts_every_integration_spelling(
    module_name: str, domain: str, module: str | None, platform: str | None
) -> None:
    """Core's own spelling, the repo-root spelling and pylint's bare spelling."""
    parsed = module_info.parse_module(module_name)
    assert parsed is not None
    assert (parsed.domain, parsed.module) == (domain, module)
    assert module_info.is_integration_module(module_name)
    assert module_info.get_module_platform(module_name) == platform


@pytest.mark.parametrize(
    "module_name",
    [
        "nomanifest.sensor",
        "elsewhere.sensor",
        "voluptuous",
        "homeassistant.const",
        "tests.test_init",
        "custom_components",
    ],
)
@pytest.mark.usefixtures("custom_root")
def test_the_gate_refuses_what_is_not_an_integration(module_name: str) -> None:
    """A bare name needs custom_components/<domain>/manifest.json behind it."""
    assert module_info.parse_module(module_name) is None
    assert not module_info.is_integration_module(module_name)
    assert module_info.get_module_platform(module_name) is None


@pytest.fixture(name="imports_checker")
def imports_checker_fixture(linter: UnittestLinter) -> HassImportsFormatChecker:
    """Core's fixture of the same name."""
    return HassImportsFormatChecker(linter)


def _import_messages(
    linter: UnittestLinter,
    checker: HassImportsFormatChecker,
    statement: str,
    module_name: str,
    file: pathlib.Path | None = None,
) -> list[str]:
    node = astroid.extract_node(f"{statement} #@", module_name)
    if file is not None:
        node.root().file = str(file)
    checker.visit_module(node.root())
    if statement.startswith("import "):
        checker.visit_import(node)
    else:
        checker.visit_importfrom(node)
    # C7404 is skipped; imports.py still defines it, so a unit linter sees it.
    return [
        message.msg_id
        for message in linter.release_messages()
        if message.msg_id != "home-assistant-absolute-import"
    ]


_SPELLINGS = pytest.mark.parametrize(
    "own", ["custom_components.pylint_test", "pylint_test"]
)


@_SPELLINGS
@pytest.mark.parametrize(
    ("module", "statement"),
    [
        # Ported from core's test_good_import.
        ("sensor", "from homeassistant.const import CONSTANT"),
        ("sensor", "from custom_components.pylint_testing import CONSTANT"),
        ("sensor", "from .const import CONSTANT"),
        ("sensor", "from . import CONSTANT"),
        ("api.hub", "from homeassistant.const import CONSTANT"),
        ("api.hub", "from ..const import CONSTANT"),
        ("api.hub", "from .. import CONSTANT"),
        # Ported from core's test_good_root_import.
        ("climate", "from homeassistant.components import climate"),
        (
            "climate",
            "from homeassistant.components.climate import ClimateEntityFeature",
        ),
    ],
)
@pytest.mark.usefixtures("custom_root")
def test_good_import(
    linter: UnittestLinter,
    imports_checker: HassImportsFormatChecker,
    own: str,
    module: str,
    statement: str,
) -> None:
    """Imports core accepts stay accepted under either custom spelling."""
    assert _import_messages(linter, imports_checker, statement, f"{own}.{module}") == []


@_SPELLINGS
@pytest.mark.parametrize(
    ("module", "statement", "msg_id"),
    [
        # Ported from core's test_bad_import, the integration spelled as a
        # custom repository imports it.
        (
            "sensor",
            "from custom_components.pylint_test.const import CONSTANT",
            "home-assistant-relative-import",
        ),
        (
            "api.hub",
            "from custom_components.pylint_test.api.const import CONSTANT",
            "home-assistant-relative-import",
        ),
        (
            "api.hub",
            "from custom_components import pylint_test",
            "home-assistant-relative-import",
        ),
        (
            "sensor",
            "import custom_components.pylint_test.const",
            "home-assistant-relative-import",
        ),
        # Ported from core's test_bad_root_import.
        (
            "climate",
            "import homeassistant.components.climate.const as climate",
            "home-assistant-component-root-import",
        ),
        (
            "climate",
            "from homeassistant.components.climate.const import CONSTANT",
            "home-assistant-component-root-import",
        ),
        (
            "sensor",
            "from custom_components.other.const import CONSTANT",
            "home-assistant-component-root-import",
        ),
        # Ported from core's constant-alias cases.
        (
            "sensor",
            "from homeassistant.components.climate import DOMAIN",
            "home-assistant-import-constant-alias",
        ),
        (
            "sensor",
            "from .const import DOMAIN as PYLINT_TEST_DOMAIN",
            "home-assistant-import-constant-unnecessary-alias",
        ),
    ],
)
@pytest.mark.usefixtures("custom_root")
def test_bad_import(
    linter: UnittestLinter,
    imports_checker: HassImportsFormatChecker,
    own: str,
    module: str,
    statement: str,
    msg_id: str,
) -> None:
    """Imports core rejects are rejected under either custom spelling."""
    assert _import_messages(linter, imports_checker, statement, f"{own}.{module}") == [
        msg_id
    ]


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("from custom_components.pylint_test.const import CONSTANT", []),
        ("from custom_components.pylint_test.const import DOMAIN", []),
        ("import custom_components.pylint_test.const as const", []),
        (
            "from custom_components.pylint_test.const import DOMAIN as TEST_DOMAIN",
            ["home-assistant-import-constant-unnecessary-alias"],
        ),
        (
            "from homeassistant.components.climate.const import CONSTANT",
            ["home-assistant-component-root-import"],
        ),
        (
            "from custom_components.other.const import CONSTANT",
            ["home-assistant-component-root-import"],
        ),
    ],
)
def test_a_repo_root_test_module_owns_its_integration(
    linter: UnittestLinter,
    imports_checker: HassImportsFormatChecker,
    custom_root: pathlib.Path,
    statement: str,
    expected: list[str],
) -> None:
    """Core's tests.components.<domain> exemptions, for tests/ beside the integration.

    ``custom_root`` holds one manifest under custom_components/, so its tests/
    tests that integration.
    """
    file = custom_root / "tests" / "test_init.py"
    assert (
        _import_messages(linter, imports_checker, statement, "tests.test_init", file)
        == expected
    )


@pytest.fixture(scope="module", name="sample_repo")
def sample_repo_fixture(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[pathlib.Path]:
    """The clean sample integration, importable as custom_components.sample."""
    repo = tmp_path_factory.mktemp("sample_repo")
    _write(repo, _SAMPLE)
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(repo))
        astroid.MANAGER.ast_from_module_name("custom_components.sample")
        astroid.MANAGER.ast_from_module_name("custom_components.sample.sensor")
        yield repo


def _setup_entry_messages(linter: UnittestLinter, code: str, module_name: str) -> list:
    _walk(linter, DirectAsyncSetupEntry(linter), astroid.parse(code, module_name))
    return [message.msg_id for message in linter.release_messages()]


@pytest.mark.parametrize(
    ("code", "module_name"),
    [
        # Ported from core's test_direct_async_setup_entry.py test_no_warning.
        pytest.param(
            """
async def test_setup(hass):
    await hass.config_entries.async_setup(entry.entry_id)
""",
            "tests.test_init",
            id="proper_setup_call",
        ),
        pytest.param(
            """
from custom_components.sample import async_setup_entry

async def test_setup(hass, mock_config_entry):
    await async_setup_entry(hass, mock_config_entry)
""",
            "custom_components.sample",
            id="not_a_test_module",
        ),
        pytest.param(
            """
async def test_setup(hass, mock_config_entry):
    await some_local.async_setup_entry(hass, mock_config_entry)
""",
            "tests.test_init",
            id="unresolved_attribute_call",
        ),
        pytest.param(
            """
async def async_setup_entry(hass, entry):
    return True

async def test_setup(hass, entry):
    await async_setup_entry(hass, entry)
""",
            "tests.test_init",
            id="local_async_setup_entry_not_an_integration",
        ),
        # The alias fix must not flag an alias of something else.
        pytest.param(
            """
from pytest_homeassistant_custom_component.common import async_fire_time_changed as fire

async def test_setup(hass):
    fire(hass)
""",
            "tests.test_init",
            id="aliased_non_integration_call",
        ),
    ],
)
@pytest.mark.usefixtures("sample_repo")
def test_direct_setup_entry_no_warning(
    linter: UnittestLinter, code: str, module_name: str
) -> None:
    """Calls that are not an integration's async_setup_entry draw nothing."""
    assert _setup_entry_messages(linter, code, module_name) == []


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # Ported from core's test_direct_async_setup_entry.py test_warning.
        pytest.param(
            """
from custom_components.sample import async_setup_entry

async def test_setup(hass, mock_config_entry):
    await async_setup_entry(hass, mock_config_entry)
""",
            "home-assistant-tests-direct-async-setup-entry",
            id="direct_name_call_from_init",
        ),
        pytest.param(
            """
from custom_components import sample

async def test_setup(hass, mock_config_entry):
    await sample.async_setup_entry(hass, mock_config_entry)
""",
            "home-assistant-tests-direct-async-setup-entry",
            id="attribute_call_from_init",
        ),
        pytest.param(
            """
from custom_components.sample.sensor import async_setup_entry

async def test_setup(hass, mock_config_entry, add_entities):
    await async_setup_entry(hass, mock_config_entry, add_entities)
""",
            "home-assistant-tests-direct-platform-async-setup-entry",
            id="direct_call_from_platform",
        ),
        pytest.param(
            """
from custom_components.sample import sensor

async def test_setup(hass, mock_config_entry, add_entities):
    await sensor.async_setup_entry(hass, mock_config_entry, add_entities)
""",
            "home-assistant-tests-direct-platform-async-setup-entry",
            id="attribute_call_from_platform",
        ),
        # The alias hole: core never looks past the callee's own name.
        pytest.param(
            """
from custom_components.sample.sensor import async_setup_entry as setup_sensor

async def test_setup(hass, mock_config_entry, add_entities):
    await setup_sensor(hass, mock_config_entry, add_entities)
""",
            "home-assistant-tests-direct-platform-async-setup-entry",
            id="aliased_call_from_platform",
        ),
        pytest.param(
            """
from custom_components.sample import async_setup_entry as setup_sample

async def test_setup(hass, mock_config_entry):
    await setup_sample(hass, mock_config_entry)
""",
            "home-assistant-tests-direct-async-setup-entry",
            id="aliased_call_from_init",
        ),
    ],
)
@pytest.mark.usefixtures("sample_repo")
def test_direct_setup_entry_warning(
    linter: UnittestLinter, code: str, expected: str
) -> None:
    """Each direct call is one message, of the kind its target decides."""
    assert _setup_entry_messages(linter, code, "tests.test_init") == [expected]


@pytest.mark.usefixtures("sample_repo")
def test_direct_setup_entry_multiple_calls_each_flagged(
    linter: UnittestLinter,
) -> None:
    """Ported from core: two direct calls are two messages."""
    code = """
from custom_components.sample import async_setup_entry

async def test_a(hass, mock_config_entry):
    await async_setup_entry(hass, mock_config_entry)

async def test_b(hass, mock_config_entry):
    await async_setup_entry(hass, mock_config_entry)
"""
    assert len(_setup_entry_messages(linter, code, "tests.test_init")) == 2


def _integration_module(custom_root: pathlib.Path, code: str, rules: str | None):
    """A parsed pylint_test.sensor, named bare, with its quality_scale.yaml."""
    integration = custom_root / "custom_components/pylint_test"
    if rules is not None:
        (integration / "quality_scale.yaml").write_text(f"rules:\n  {rules}\n")
    node = astroid.parse(code, "pylint_test.sensor")
    node.file = str(integration / "sensor.py")
    return node


@pytest.mark.parametrize(
    ("code", "rules", "fires"),
    [
        # Ported from core's quality_scale/test_parallel_updates.py.
        ("PARALLEL_UPDATES = 1\n", "parallel-updates: done", False),
        ("PARALLEL_UPDATES = 0\n", "parallel-updates: done", False),
        ("PARALLEL_UPDATES: Final = 0\n", "parallel-updates: done", False),
        (
            "async def async_setup_entry(hass, entry, async_add_entities): pass\n",
            "parallel-updates: done",
            True,
        ),
        ("async def async_setup_entry(hass, entry, add): pass\n", None, False),
        (
            "async def async_setup_entry(hass, entry, add): pass\n",
            "parallel-updates: todo",
            False,
        ),
    ],
)
def test_parallel_updates_on_a_bare_named_platform(
    linter: UnittestLinter,
    custom_root: pathlib.Path,
    code: str,
    rules: str | None,
    fires: bool,
) -> None:
    """A quality-scale gate reached through the bare module name and its file."""
    node = _integration_module(custom_root, code, rules)
    _walk(linter, ParallelUpdatesChecker(linter), node)
    messages = [message.msg_id for message in linter.release_messages()]
    assert messages == (["home-assistant-missing-parallel-updates"] if fires else [])
