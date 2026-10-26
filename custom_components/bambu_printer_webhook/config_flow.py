"""Config flow: where to send the printer states, and with which token."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from yarl import URL

from .const import CONF_TOKEN, CONF_WEBHOOK_URL, DOMAIN, NAME


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_WEBHOOK_URL, default=defaults.get(CONF_WEBHOOK_URL, vol.UNDEFINED)): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            vol.Required(CONF_TOKEN, default=defaults.get(CONF_TOKEN, vol.UNDEFINED)): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


def _validate(user_input: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """The cleaned input and any errors by field."""
    errors: dict[str, str] = {}
    url = user_input[CONF_WEBHOOK_URL].strip()
    token = user_input[CONF_TOKEN].strip()

    try:
        parsed = URL(url)
    except ValueError:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.host:
        errors[CONF_WEBHOOK_URL] = "invalid_url"
    if not token:
        errors[CONF_TOKEN] = "invalid_token"

    return {CONF_WEBHOOK_URL: url, CONF_TOKEN: token}, errors


class BambuPrinterWebhookConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one webhook; every Bambu Lab printer is sent to it."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _validate(user_input)
            if not errors:
                await self.async_set_unique_id(data[CONF_WEBHOOK_URL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=URL(data[CONF_WEBHOOK_URL]).host or NAME, data=data)

        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _validate(user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=data[CONF_WEBHOOK_URL],
                    title=URL(data[CONF_WEBHOOK_URL]).host or NAME,
                    data=data,
                )

        return self.async_show_form(
            step_id="reconfigure", data_schema=_schema(user_input or dict(entry.data)), errors=errors
        )
