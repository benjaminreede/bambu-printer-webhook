"""Whether the last request to the webhook failed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BambuWebhookConfigEntry
from .entity import WebhookEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: BambuWebhookConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([DeliveryProblemSensor(entry, entry.runtime_data.client, "delivery_problem")])


class DeliveryProblemSensor(WebhookEntity, BinarySensorEntity):
    """On when the last state or file could not be delivered; why is in its attributes."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool | None:
        if not self._client.attempted:
            return None
        return self._client.last_error is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"last_error": self._client.last_error}
