"""When the webhook last accepted a request."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BambuWebhookConfigEntry
from .entity import WebhookEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: BambuWebhookConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([LastDeliveredSensor(entry, entry.runtime_data.client, "last_delivered")])


class LastDeliveredSensor(WebhookEntity, SensorEntity):
    """The last time a state or file was delivered, unknown until the first one since startup."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self._client.last_delivered
