"""Config flow: where to send the printer states, and with which token."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    DateSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from yarl import URL

from .const import (
    CONF_ENERGY_SENSORS,
    CONF_HISTORY_SINCE,
    CONF_INTERVAL,
    CONF_PRICE_ENTITY,
    CONF_PRICE_PER_KWH,
    CONF_TOKEN,
    CONF_WEBHOOK_URL,
    DEFAULT_INTERVAL_SECONDS,
    DOMAIN,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    NAME,
    REQUEST_TIMEOUT_SECONDS,
    SCHEMA_VERSION,
)
from .discovery import find_printers


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


async def _async_ping(hass: HomeAssistant, data: dict[str, str]) -> dict[str, str]:
    """Send a ping (docs/schema-v1.md) and turn the answer into errors by field."""
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            data[CONF_WEBHOOK_URL],
            json={"schema_version": SCHEMA_VERSION, "ping": True},
            headers={"Authorization": f"Bearer {data[CONF_TOKEN]}", "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
        ) as response:
            status = response.status
    except (aiohttp.ClientError, TimeoutError):
        return {"base": "cannot_connect"}

    if status < 300:
        return {}
    if status in (401, 403):
        return {CONF_TOKEN: "invalid_auth"}
    if status in (404, 405):
        return {CONF_WEBHOOK_URL: "wrong_endpoint"}
    return {"base": "unexpected_response"}


async def _async_check(hass: HomeAssistant, user_input: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """The cleaned input, and any errors from checking it and then pinging the webhook."""
    data, errors = _validate(user_input)
    if not errors:
        errors = await _async_ping(hass, data)
    return data, errors


class BambuPrinterWebhookConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one webhook; every Bambu Lab printer is sent to it."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BambuPrinterWebhookOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_check(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(data[CONF_WEBHOOK_URL])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=URL(data[CONF_WEBHOOK_URL]).host or NAME, data=data)

        return self.async_show_form(step_id="user", data_schema=_schema(user_input or {}), errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_check(self.hass, user_input)
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


class BambuPrinterWebhookOptionsFlow(OptionsFlow):
    """How often minor changes are sent, and each printer's energy meter and
    the electricity price, so the receiver can work out what a print cost."""

    def __init__(self) -> None:
        self._meter_fields: dict[str, str] = {}
        """Field name to printer serial; the fields are named after the printers."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self.config_entry.options
        if user_input is not None:
            price = user_input.get(CONF_PRICE_PER_KWH)
            return self.async_create_entry(
                data={
                    CONF_INTERVAL: int(user_input[CONF_INTERVAL]),
                    CONF_ENERGY_SENSORS: {
                        serial: user_input[field]
                        for field, serial in self._meter_fields.items()
                        if user_input.get(field)
                    },
                    CONF_PRICE_ENTITY: user_input.get(CONF_PRICE_ENTITY) or None,
                    CONF_PRICE_PER_KWH: float(price) if price not in (None, "") else None,
                    CONF_HISTORY_SINCE: user_input.get(CONF_HISTORY_SINCE) or None,
                }
            )

        meters = options.get(CONF_ENERGY_SENSORS, {})
        schema: dict[Any, Any] = {
            vol.Required(CONF_INTERVAL, default=options.get(CONF_INTERVAL, DEFAULT_INTERVAL_SECONDS)): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_INTERVAL_SECONDS,
                    max=MAX_INTERVAL_SECONDS,
                    step=1,
                    unit_of_measurement="s",
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }

        self._meter_fields = {}
        for printer in sorted(find_printers(self.hass), key=lambda printer: printer.name or printer.serial):
            field = f"kWh-meter {printer.name or printer.serial}"
            if field in self._meter_fields:
                field = f"{field} ({printer.serial})"
            self._meter_fields[field] = printer.serial
            schema[vol.Optional(field, description={"suggested_value": meters.get(printer.serial)})] = EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="energy")
            )

        schema[vol.Optional(CONF_PRICE_ENTITY, description={"suggested_value": options.get(CONF_PRICE_ENTITY)})] = (
            EntitySelector(EntitySelectorConfig(domain="sensor"))
        )
        schema[vol.Optional(CONF_PRICE_PER_KWH, description={"suggested_value": options.get(CONF_PRICE_PER_KWH)})] = (
            NumberSelector(
                NumberSelectorConfig(
                    min=0, max=10, step="any", unit_of_measurement="€/kWh", mode=NumberSelectorMode.BOX
                )
            )
        )

        schema[
            vol.Optional(
                CONF_HISTORY_SINCE,
                description={"suggested_value": options.get(CONF_HISTORY_SINCE)},
            )
        ] = DateSelector()

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
