"""The integration's own entities: how sending to the webhook goes."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, NAME, VERSION
from .manager import WebhookClient


class WebhookEntity(Entity):
    """A diagnostic entity of one webhook, updated after every request to it."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, client: WebhookClient, key: str) -> None:
        self._client = client
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            entry_type=DeviceEntryType.SERVICE,
            sw_version=VERSION,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._client.async_add_listener(self.async_write_ha_state))
