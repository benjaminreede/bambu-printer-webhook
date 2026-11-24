"""Bambu Printer Webhook: send the state of Bambu Lab printers to a webhook."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_SENSORS,
    CONF_HISTORY_SINCE,
    CONF_INTERVAL,
    CONF_PRICE_ENTITY,
    CONF_PRICE_PER_KWH,
    CONF_TOKEN,
    CONF_WEBHOOK_URL,
    DEFAULT_INTERVAL_SECONDS,
)
from .manager import WebhookManager
from .state import Price

type BambuWebhookConfigEntry = ConfigEntry[WebhookManager]

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: BambuWebhookConfigEntry) -> bool:
    """Start sending printer states to the configured webhook."""
    manager = WebhookManager(
        hass,
        entry.data[CONF_WEBHOOK_URL],
        entry.data[CONF_TOKEN],
        entry.entry_id,
        timedelta(seconds=entry.options.get(CONF_INTERVAL, DEFAULT_INTERVAL_SECONDS)),
        entry.options.get(CONF_ENERGY_SENSORS, {}),
        Price(entry.options.get(CONF_PRICE_ENTITY), entry.options.get(CONF_PRICE_PER_KWH)),
        _start_of(entry.options.get(CONF_HISTORY_SINCE)),
    )
    await manager.async_load()
    manager.async_start()
    entry.runtime_data = manager
    entry.async_on_unload(manager.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BambuWebhookConfigEntry) -> bool:
    """Stop sending; the listeners are removed through async_on_unload."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: BambuWebhookConfigEntry) -> None:
    """Start over with the changed options."""
    await hass.config_entries.async_reload(entry.entry_id)


def _start_of(day: str | None) -> datetime | None:
    """The start of a day (``2026-09-01``) in Home Assistant's time zone."""
    parsed = dt_util.parse_date(day) if day else None
    return dt_util.start_of_local_day(parsed) if parsed else None
