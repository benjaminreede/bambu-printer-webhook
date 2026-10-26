"""Bambu Printer Webhook: send the state of Bambu Lab printers to a webhook."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN, CONF_WEBHOOK_URL
from .manager import WebhookManager

type BambuWebhookConfigEntry = ConfigEntry[WebhookManager]

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: BambuWebhookConfigEntry) -> bool:
    """Start sending printer states to the configured webhook."""
    manager = WebhookManager(hass, entry.data[CONF_WEBHOOK_URL], entry.data[CONF_TOKEN], entry.entry_id)
    await manager.async_load()
    manager.async_start()
    entry.runtime_data = manager
    entry.async_on_unload(manager.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BambuWebhookConfigEntry) -> bool:
    """Stop sending; the listeners are removed through async_on_unload."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
