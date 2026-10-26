"""Building the printer state document from ha-bambulab's entities."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.core import HomeAssistant

from custom_components.bambu_printer_webhook.discovery import find_printers
from custom_components.bambu_printer_webhook.state import build_state

from .conftest import MODEL, SERIAL, BambuSetup, tray_attributes

NOW = datetime(2026, 10, 26, 10, 17, 45, tzinfo=UTC)


def _set_printing(bambu: BambuSetup) -> None:
    states = bambu.hass.states
    ids = bambu.entity_ids
    states.async_set(ids["print_status"], "running")
    states.async_set(ids["subtask_name"], "Christmas Moose Tea Light")
    states.async_set(ids["print_progress"], "49")
    states.async_set(ids["current_layer"], "269")
    states.async_set(ids["total_layers"], "614")
    states.async_set(ids["start_time"], "2026-10-26T08:17:45+00:00")
    states.async_set(ids["end_time"], "2026-10-26T12:02:00+00:00")
    states.async_set(ids["print_weight"], "38.0")
    states.async_set(ids["online"], "on")
    states.async_set(ids["ams_1_tray_1"], "Bambu PLA Matte", tray_attributes(active=True))
    states.async_set(ids["ams_1_tray_2"], "Leeg", {"empty": True, "active": False, "tray_uuid": "0" * 32})
    states.async_set(
        ids["ams_1_tray_3"],
        "Generic PLA",
        tray_attributes(tray_uuid="0" * 32, tag_uid="0" * 16, name="Generic PLA", color="#ffffff", remain=-1),
    )
    states.async_set(ids["ams_1_tray_4"], "unavailable")
    states.async_set(ids["ams_1_humidity_index"], "2")
    states.async_set(ids["external_spool"], "Leeg", {"empty": True, "active": False})


async def test_builds_the_full_state_of_a_printing_printer(hass: HomeAssistant, bambu: BambuSetup) -> None:
    _set_printing(bambu)

    (printer,) = find_printers(hass)
    document = build_state(printer, hass.states.get, NOW)

    assert document == {
        "schema_version": 1,
        "sent_at": "2026-10-26T10:17:45+00:00",
        "source": {"integration": "bambu_printer_webhook", "version": "0.1.0"},
        "printer": {"serial": SERIAL, "model": MODEL, "name": f"{MODEL}_{SERIAL}"},
        "online": True,
        "status": "running",
        "print": {
            "task_name": "Christmas Moose Tea Light",
            "progress_percent": 49,
            "current_layer": 269,
            "total_layers": 614,
            "start_time": "2026-10-26T08:17:45+00:00",
            "end_time": "2026-10-26T12:02:00+00:00",
            "weight_grams": 38,
        },
        "ams": [
            {
                "index": 1,
                "humidity": None,
                "humidity_index": 2,
                "trays": [
                    {
                        "slot": 1,
                        "empty": False,
                        "active": True,
                        "tray_uuid": "02976CE8418148D988489D5D82B1467C",
                        "tag_uid": "A288022C00000100",
                        "name": "Bambu PLA Matte",
                        "type": "PLA",
                        "color": "#7D6556FF",
                        "tray_weight": 1000,
                        "remain": 79,
                    },
                    {"slot": 2, "empty": True, "active": False},
                    {
                        "slot": 3,
                        "empty": False,
                        "active": False,
                        "tray_uuid": None,
                        "tag_uid": None,
                        "name": "Generic PLA",
                        "type": "PLA",
                        "color": "#FFFFFFFF",
                        "tray_weight": 1000,
                        "remain": -1,
                    },
                    {"slot": 4, "empty": True, "active": False},
                ],
            }
        ],
        "external_spool": {"empty": True, "active": False},
    }


async def test_a_second_ams_is_reported_with_its_own_index(hass: HomeAssistant, bambu: BambuSetup) -> None:
    second = bambu.ams_device(2, "AMSSERIAL0002")
    tray = bambu.entity("ams_2_tray_1", "sensor", f"{MODEL}_{SERIAL}_AMS_AMSSERIAL0002_tray_1", second, "ams_2_lade_1")
    hass.states.async_set(tray, "Bambu PETG HF", tray_attributes(type="PETG", name="Bambu PETG HF"))

    (printer,) = find_printers(hass)
    document = build_state(printer, hass.states.get, NOW)

    assert [unit["index"] for unit in document["ams"]] == [1, 2]
    assert document["ams"][1]["trays"][0]["type"] == "PETG"


async def test_an_offline_printer_reports_offline_and_no_print(hass: HomeAssistant, bambu: BambuSetup) -> None:
    hass.states.async_set(bambu.entity_ids["print_status"], "offline")
    hass.states.async_set(bambu.entity_ids["online"], "off")

    (printer,) = find_printers(hass)
    document = build_state(printer, hass.states.get, NOW)

    assert document["online"] is False
    assert document["status"] == "offline"
    assert document["print"] is None


async def test_an_unexpected_status_is_reported_as_unknown(hass: HomeAssistant, bambu: BambuSetup) -> None:
    hass.states.async_set(bambu.entity_ids["print_status"], "SOMETHING_NEW")

    (printer,) = find_printers(hass)

    assert build_state(printer, hass.states.get, NOW)["status"] == "unknown"
