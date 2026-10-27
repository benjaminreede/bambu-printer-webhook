"""Find the Bambu Lab printers and their entities in Home Assistant's registries.

Entity ids depend on the language Home Assistant runs in (a Dutch install has
``sensor.p1s_<serial>_printstatus``), so nothing here looks at them. Instead
it relies on the unique ids ha-bambulab gives its entities:

- printer entities: ``{serial}_{key}``, e.g. ``01P09C510500566_print_status``,
  the binary sensor ``{serial}_print_error`` and the image entity
  ``{serial}_cover_image``;
- AMS entities: ``{model}_{serial}_AMS_{ams serial}_{key}``, with keys
  ``tray_1`` to ``tray_4``, ``humidity`` and ``humidity_index``; the AMS's
  number comes from its device, named ``{model}_{serial}_AMS_{n}`` (1-based,
  128 and up for an AMS HT);
- external spool: ``{model}_{serial}_ExternalSpool{suffix}_external_spool``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import BAMBU_DOMAIN, COVER_IMAGE_KEY, ONLINE_KEY, PRINT_ERROR_KEY, PRINTER_SENSOR_KEYS

_AMS_DEVICE_NAME = re.compile(r"_AMS_(\d+)$")
_AMS_TRAY_KEY = re.compile(r"_AMS_.+_tray_(\d+)$")


@dataclass
class AmsEntities:
    """The entities of one AMS unit."""

    index: int
    trays: dict[int, str] = field(default_factory=dict)
    humidity: str | None = None
    humidity_index: str | None = None


@dataclass
class PrinterEntities:
    """One printer and the entity ids its state is built from."""

    serial: str
    model: str | None
    name: str | None
    sensors: dict[str, str] = field(default_factory=dict)
    online: str | None = None
    print_error: str | None = None
    cover_image: str | None = None
    ams: list[AmsEntities] = field(default_factory=list)
    external_spool: str | None = None

    @property
    def entity_ids(self) -> set[str]:
        """Every entity whose changes should trigger a new state."""
        ids = set(self.sensors.values())
        printer_ids = (self.online, self.print_error, self.cover_image, self.external_spool)
        ids.update(entity_id for entity_id in printer_ids if entity_id)
        for unit in self.ams:
            ids.update(unit.trays.values())
            ids.update(entity_id for entity_id in (unit.humidity, unit.humidity_index) if entity_id)
        return ids


def find_printers(hass: HomeAssistant) -> list[PrinterEntities]:
    """Every printer ha-bambulab has set up, one per config entry."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    printers: list[PrinterEntities] = []

    for entry in hass.config_entries.async_entries(BAMBU_DOMAIN):
        serial = entry.data.get("serial")
        if not serial:
            continue

        # Looked up among this config entry's own devices: identifiers are no
        # longer unique across config entries.
        device = next(
            (
                device
                for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
                if (BAMBU_DOMAIN, serial) in device.identifiers
            ),
            None,
        )
        printer = PrinterEntities(
            serial=serial,
            model=(device.model if device else None) or entry.data.get("device_type"),
            name=(device.name_by_user or device.name) if device else entry.title,
        )
        ams_by_device: dict[str, AmsEntities] = {}

        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
            if entity.platform != BAMBU_DOMAIN or entity.disabled:
                continue
            _sort_entity(printer, entity, ams_by_device, device_registry)

        printer.ams = sorted(ams_by_device.values(), key=lambda unit: unit.index)
        printers.append(printer)

    return printers


def _sort_entity(
    printer: PrinterEntities,
    entity: er.RegistryEntry,
    ams_by_device: dict[str, AmsEntities],
    device_registry: dr.DeviceRegistry,
) -> None:
    unique_id = entity.unique_id
    serial = printer.serial

    for field_name, key in PRINTER_SENSOR_KEYS.items():
        if entity.domain == "sensor" and unique_id == f"{serial}_{key}":
            printer.sensors[field_name] = entity.entity_id
            return

    if entity.domain == "binary_sensor" and unique_id == f"{serial}_{ONLINE_KEY}":
        printer.online = entity.entity_id
        return

    if entity.domain == "binary_sensor" and unique_id == f"{serial}_{PRINT_ERROR_KEY}":
        printer.print_error = entity.entity_id
        return

    if entity.domain == "image" and unique_id == f"{serial}_{COVER_IMAGE_KEY}":
        printer.cover_image = entity.entity_id
        return

    if entity.domain == "sensor" and "_ExternalSpool" in unique_id and unique_id.endswith("_external_spool"):
        # Printers with two nozzles have a spool holder per side; the first
        # one found is reported, preferring the one without a suffix.
        if printer.external_spool is None or unique_id.endswith("_ExternalSpool_external_spool"):
            printer.external_spool = entity.entity_id
        return

    if entity.domain != "sensor" or "_AMS_" not in unique_id or entity.device_id is None:
        return

    unit = ams_by_device.get(entity.device_id)
    if unit is None:
        index = _ams_index(device_registry.async_get(entity.device_id))
        if index is None:
            return
        unit = ams_by_device[entity.device_id] = AmsEntities(index=index)

    if match := _AMS_TRAY_KEY.search(unique_id):
        unit.trays[int(match.group(1))] = entity.entity_id
    elif unique_id.endswith("_humidity_index"):
        unit.humidity_index = entity.entity_id
    elif unique_id.endswith("_humidity"):
        unit.humidity = entity.entity_id


def _ams_index(device: dr.DeviceEntry | None) -> int | None:
    """The AMS number from its device name, which the user cannot rename."""
    if device is None or device.name is None:
        return None
    match = _AMS_DEVICE_NAME.search(device.name)
    return int(match.group(1)) if match else None
