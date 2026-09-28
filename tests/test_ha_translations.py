"""Tests for pytest_plugins/ha_translations.py.

Each test builds a one-integration repository in a temporary directory and runs its
suite in a child pytest with the plugin loaded, the way python-validate.yml runs a
consumer's. The child needs pytest-homeassistant-custom-component; this suite's own run
must not load it, which is why ci.yml passes `-p no:homeassistant` here.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

_PLUGINS = pathlib.Path(__file__).resolve().parents[1] / "pytest_plugins"

_MANIFEST = {
    "domain": "sample",
    "name": "Sample",
    "codeowners": [],
    "config_flow": True,
    "documentation": "https://example.com",
    "integration_type": "hub",
    "iot_class": "local_polling",
    "requirements": [],
    "version": "0.0.0",
}

_INIT = '''"""Sample integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up nothing."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload nothing."""
    return True
'''

_FLOW = '''"""Sample config flow."""

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult


class SampleConfigFlow(ConfigFlow, domain="sample"):
    """Ask for a host; `bad` fails to connect."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the form, or create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input["host"] == "bad":
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=user_input["host"], data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("host"): str}),
            errors=errors,
        )
'''

_CONFTEST = '''"""Claim custom_components before the harness does."""

import pytest

import custom_components  # noqa: F401


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let HA load the sample integration."""
'''

_CASE = '''"""The flow shows its error, then creates the entry."""

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType


async def test_error_then_entry(hass: HomeAssistant) -> None:
    """A failed connection shows cannot_connect; a good host creates the entry."""
    result = await hass.config_entries.flow.async_init(
        "sample", context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "bad"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "good"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
'''


def _translations(**drop: bool) -> dict:
    """The sample's en.json, with the named keys left out."""
    step = {
        "title": "Connect",
        "data": {"host": "Host"},
        "data_description": {"host": "The device's address"},
    }
    errors = {"cannot_connect": "Failed to connect"}
    if drop.get("data_description"):
        del step["data_description"]
    if drop.get("cannot_connect"):
        del errors["cannot_connect"]
    return {"config": {"step": {"user": step}, "error": errors}}


def _run(tmp_path: pathlib.Path, translations: dict, config_flow_rule: str) -> str:
    """Build the sample repository, run its suite with the plugin, return the output."""
    pkg = tmp_path / "custom_components/sample"
    (pkg / "translations").mkdir(parents=True)
    (pkg / "manifest.json").write_text(json.dumps(_MANIFEST))
    (pkg / "__init__.py").write_text(_INIT)
    (pkg / "config_flow.py").write_text(_FLOW)
    (pkg / "translations/en.json").write_text(json.dumps(translations))
    (pkg / "quality_scale.yaml").write_text(
        f"rules:\n  config-flow: {config_flow_rule}\n"
    )
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nasyncio_mode = "auto"\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_flow.py").write_text(_CASE)
    env = {**os.environ, "PYTHONPATH": str(_PLUGINS)}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "ha_translations"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return f"rc={run.returncode}\n{run.stdout}\n{run.stderr}"


def test_a_translated_flow_passes(tmp_path) -> None:
    """Every string the flow shows exists, so the suite is green."""
    out = _run(tmp_path, _translations(), "done")
    assert out.startswith("rc=0"), out


def test_a_missing_error_message_fails(tmp_path) -> None:
    """The flow shows cannot_connect, and en.json has no text for it."""
    out = _run(tmp_path, _translations(cannot_connect=True), "todo")
    assert out.startswith("rc=1"), out
    assert "`config.error.cannot_connect`" in out
    assert "custom_components/sample/translations/en.json" in out


def test_a_field_description_is_required_once_config_flow_is_done(tmp_path) -> None:
    """Core requires data_description only when quality_scale marks config-flow done."""
    assert _run(
        tmp_path / "todo", _translations(data_description=True), "todo"
    ).startswith("rc=0")
    out = _run(tmp_path / "done", _translations(data_description=True), "done")
    assert out.startswith("rc=1"), out
    assert "data_description.host" in out
