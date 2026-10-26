"""Fixtures: a Home Assistant with ha-bambulab's registry entries, without ha-bambulab itself."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytest_plugins = "pytest_homeassistant_custom_component"

SERIAL = "01P09C510500566"
MODEL = "P1S"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@dataclass
class BambuSetup:
    """The fake ha-bambulab printer and the entity ids it was given."""

    hass: HomeAssistant
    entry: MockConfigEntry
    printer_device: dr.DeviceEntry
    entity_ids: dict[str, str]

    def ams_device(self, index: int, ams_serial: str) -> dr.DeviceEntry:
        return dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={("bambu_lab", ams_serial)},
            name=f"{MODEL}_{SERIAL}_AMS_{index}",
            model="AMS",
            via_device=("bambu_lab", SERIAL),
        )

    def entity(self, key: str, domain: str, unique_id: str, device: dr.DeviceEntry, object_id: str) -> str:
        entry = er.async_get(self.hass).async_get_or_create(
            domain,
            "bambu_lab",
            unique_id,
            config_entry=self.entry,
            device_id=device.id,
            suggested_object_id=object_id,
        )
        self.entity_ids[key] = entry.entity_id
        return entry.entity_id


@pytest.fixture
def bambu(hass: HomeAssistant) -> BambuSetup:
    """A P1S with Dutch entity ids, one AMS with trays 1 to 4, and an external spool."""
    entry = MockConfigEntry(domain="bambu_lab", data={"serial": SERIAL, "device_type": MODEL}, title=MODEL)
    entry.add_to_hass(hass)
    printer_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("bambu_lab", SERIAL)},
        name=f"{MODEL}_{SERIAL}",
        model=MODEL,
    )
    setup = BambuSetup(hass, entry, printer_device, {})

    for key, object_id in {
        "print_status": "printstatus",
        "subtask_name": "taaknaam",
        "print_progress": "printvoortgang",
        "current_layer": "huidige_laag",
        "total_layers": "hoeveelheid_lagen",
        "start_time": "starttijd",
        "end_time": "eindtijd",
        "print_weight": "gewicht_van_print",
    }.items():
        setup.entity(key, "sensor", f"{SERIAL}_{key}", printer_device, f"p1s_{SERIAL.lower()}_{object_id}")
    setup.entity("online", "binary_sensor", f"{SERIAL}_online", printer_device, f"p1s_{SERIAL.lower()}_online")
    setup.entity(
        "cover_image", "image", f"{SERIAL}_cover_image", printer_device, f"p1s_{SERIAL.lower()}_cover_afbeelding"
    )

    ams = setup.ams_device(1, "AMSSERIAL0001")
    for slot in range(1, 5):
        setup.entity(
            f"ams_1_tray_{slot}",
            "sensor",
            f"{MODEL}_{SERIAL}_AMS_AMSSERIAL0001_tray_{slot}",
            ams,
            f"p1s_{SERIAL.lower()}_ams_1_lade_{slot}",
        )
    setup.entity(
        "ams_1_humidity_index",
        "sensor",
        f"{MODEL}_{SERIAL}_AMS_AMSSERIAL0001_humidity_index",
        ams,
        "ams_1_vochtigheidsindex",
    )

    spool = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("bambu_lab", f"{SERIAL}_ExternalSpool")},
        name=f"{MODEL}_{SERIAL}_ExternalSpool",
        model="External Spool",
    )
    setup.entity("external_spool", "sensor", f"{MODEL}_{SERIAL}_ExternalSpool_external_spool", spool, "externe_spoel")

    return setup


def tray_attributes(**overrides: object) -> dict[str, object]:
    """The attributes ha-bambulab gives a loaded AMS tray sensor."""
    return {
        "active": False,
        "color": "#7D6556FF",
        "empty": False,
        "name": "Bambu PLA Matte",
        "remain": 79,
        "tag_uid": "A288022C00000100",
        "tray_uuid": "02976CE8418148D988489D5D82B1467C",
        "tray_weight": 1000,
        "type": "PLA",
        **overrides,
    }
