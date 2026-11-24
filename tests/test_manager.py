"""Sending printer states: settling, heartbeat and failures."""

from __future__ import annotations

import io
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import FormData
from homeassistant.components.image import Image
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bambu_printer_webhook.const import DOMAIN
from custom_components.bambu_printer_webhook.history import StoredFile

from .conftest import SERIAL, BambuSetup, tray_attributes

URL = "https://example.test/printer-state"


async def _set_up(hass: HomeAssistant, options: dict[str, object] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"webhook_url": URL, "token": "secret"}, unique_id=URL, options=options or {}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _tick(hass: HomeAssistant, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


@pytest.fixture
def webhook(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    aioclient_mock.post(URL, json={"ok": True})
    return aioclient_mock


async def test_sends_the_state_once_after_startup(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    hass.states.async_set(bambu.entity_ids["print_status"], "idle")
    await _set_up(hass)

    await _tick(hass, freezer, 31)

    assert webhook.call_count == 1
    method, url, body, headers = webhook.mock_calls[0]
    assert (method, str(url)) == ("POST", URL)
    assert headers["Authorization"] == "Bearer secret"
    assert body["printer"]["serial"] == SERIAL
    assert body["status"] == "idle"


async def test_a_burst_of_changes_is_sent_once_it_has_settled(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    await _set_up(hass)
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()
    tray = bambu.entity_ids["ams_1_tray_3"]

    # Inserting a spool, with the gaps seen on a real P1S: the previous
    # spool, 5 s later the new one unmeasured, 23 s later its real reading.
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="AAAA", remain=0))
    await hass.async_block_till_done()
    await _tick(hass, freezer, 5)
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="BBBB", remain=-1))
    await hass.async_block_till_done()
    await _tick(hass, freezer, 23)
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="BBBB", remain=83))
    await hass.async_block_till_done()
    await _tick(hass, freezer, 29)

    assert webhook.call_count == 0

    await _tick(hass, freezer, 2)

    assert webhook.call_count == 1
    sent_tray = webhook.mock_calls[0][2]["ams"][0]["trays"][2]
    assert (sent_tray["tray_uuid"], sent_tray["remain"]) == ("BBBB", 83)


async def test_a_spool_the_ams_finished_reading_goes_out_right_away_as_settled(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    hass.states.async_set(bambu.entity_ids["print_status"], "idle")
    hass.states.async_set(bambu.entity_ids["online"], "on")
    await _set_up(hass)
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()
    tray = bambu.entity_ids["ams_1_tray_3"]

    # The AMS reading a newly inserted spool: the slot goes active, still
    # shows the previous spool, then the new one unmeasured, then measured,
    # and lets go.
    for attributes in (
        tray_attributes(active=True, tray_uuid="AAAA", remain=0),
        tray_attributes(active=True, tray_uuid="BBBB", remain=-1),
        tray_attributes(active=True, tray_uuid="BBBB", remain=83),
    ):
        hass.states.async_set(tray, "Bambu PLA Matte", attributes)
        await hass.async_block_till_done()
        await _tick(hass, freezer, 5)
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="BBBB", remain=83))
    await hass.async_block_till_done()
    await _tick(hass, freezer, 2)

    assert webhook.call_count == 0

    await _tick(hass, freezer, 2)

    assert webhook.call_count == 1
    sent_tray = webhook.mock_calls[0][2]["ams"][0]["trays"][2]
    assert (sent_tray["tray_uuid"], sent_tray["remain"], sent_tray["settled"]) == ("BBBB", 83, True)


async def test_a_spool_read_in_a_slot_that_still_showed_empty_goes_out_right_away(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    # As seen on a P1S: the slot goes active while still empty, the spool
    # appears once its tag is read, and here it lets go in that same update.
    hass.states.async_set(bambu.entity_ids["print_status"], "idle")
    hass.states.async_set(bambu.entity_ids["online"], "on")
    tray = bambu.entity_ids["ams_1_tray_4"]
    hass.states.async_set(tray, "Leeg", {"empty": True, "active": False})
    await _set_up(hass)
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()

    hass.states.async_set(tray, "Leeg", {"empty": True, "active": True})
    await hass.async_block_till_done()
    await _tick(hass, freezer, 12)
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="CCCC", remain=69))
    await hass.async_block_till_done()
    await _tick(hass, freezer, 4)

    assert webhook.call_count == 1
    sent_tray = webhook.mock_calls[0][2]["ams"][0]["trays"][3]
    assert (sent_tray["tray_uuid"], sent_tray["settled"]) == ("CCCC", True)


async def test_a_spool_taken_out_goes_out_right_away(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    hass.states.async_set(bambu.entity_ids["print_status"], "finish")
    hass.states.async_set(bambu.entity_ids["online"], "on")
    tray = bambu.entity_ids["ams_1_tray_4"]
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="BBBB"))
    await _set_up(hass)
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()

    hass.states.async_set(tray, "Leeg", {"empty": True, "active": False})
    await hass.async_block_till_done()
    await _tick(hass, freezer, 4)

    assert webhook.call_count == 1
    assert webhook.mock_calls[0][2]["ams"][0]["trays"][3]["empty"] is True


async def test_slots_going_unavailable_with_the_printer_do_not_go_out_right_away(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    hass.states.async_set(bambu.entity_ids["print_status"], "idle")
    hass.states.async_set(bambu.entity_ids["online"], "on")
    tray = bambu.entity_ids["ams_1_tray_4"]
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="BBBB"))
    await _set_up(hass)
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()

    hass.states.async_set(bambu.entity_ids["online"], "off")
    hass.states.async_set(tray, "unavailable")
    await hass.async_block_till_done()
    await _tick(hass, freezer, 4)

    assert webhook.call_count == 0


async def test_a_slot_letting_go_during_a_print_is_not_a_spool_read(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    _set_print(hass, bambu, "running")
    tray = bambu.entity_ids["ams_1_tray_3"]
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(active=True, tray_uuid="BBBB"))
    await _set_up(hass)
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()

    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="BBBB"))
    await hass.async_block_till_done()
    await _tick(hass, freezer, 4)

    assert webhook.call_count == 0

    await _tick(hass, freezer, 27)

    assert webhook.call_count == 1
    assert webhook.mock_calls[0][2]["ams"][0]["trays"][2]["settled"] is False


async def test_a_spool_running_out_goes_out_soon(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    _set_print(hass, bambu, "running")
    hass.states.async_set(bambu.entity_ids["stage"], "printing")
    await _set_up(hass)
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()

    hass.states.async_set(bambu.entity_ids["stage"], "paused_filament_runout")
    await hass.async_block_till_done()
    await _tick(hass, freezer, 31)

    assert webhook.call_count == 1
    assert webhook.mock_calls[0][2]["stage"] == "paused_filament_runout"


async def test_sends_a_heartbeat_without_changes(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    await _set_up(hass)
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()

    await _tick(hass, freezer, 300)

    assert webhook.call_count == 1


async def test_a_failing_webhook_is_logged_and_not_retried(
    hass: HomeAssistant, bambu: BambuSetup, aioclient_mock: AiohttpClientMocker, freezer, caplog
) -> None:
    aioclient_mock.post(URL, status=500, text="boom")
    await _set_up(hass)

    await _tick(hass, freezer, 31)
    await _tick(hass, freezer, 60)

    assert aioclient_mock.call_count == 1
    assert "Webhook answered 500" in caplog.text


async def test_unloading_stops_sending(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    entry = await _set_up(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)

    await _tick(hass, freezer, 600)

    assert webhook.call_count == 0


PNG = b"\x89PNG\r\n\x1a\nfake"
START = "2026-10-26T08:17:45+00:00"


def _form(form: FormData) -> dict[str, object]:
    """The fields of a multipart form as sent, the file as (name, bytes)."""
    fields: dict[str, object] = {}
    for options, _headers, value in form._fields:
        fields[options["name"]] = (options["filename"], value) if "filename" in options else value
    return fields


def _set_print(hass: HomeAssistant, bambu: BambuSetup, status: str) -> None:
    hass.states.async_set(bambu.entity_ids["print_status"], status)
    hass.states.async_set(bambu.entity_ids["subtask_name"], "Christmas Moose")
    hass.states.async_set(bambu.entity_ids["start_time"], START)


def _cache_model(hass: HomeAssistant, name: str, modified: str) -> Path:
    path = Path(hass.config.config_dir) / "www/media/ha-bambulab" / SERIAL / "prints/cache" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"3mf-bytes")
    timestamp = datetime.fromisoformat(modified).timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


@pytest.fixture
def cover():
    with patch(
        "custom_components.bambu_printer_webhook.manager.async_get_image",
        AsyncMock(return_value=Image("image/jpeg", PNG)),
    ) as mock:
        yield mock


async def test_a_running_prints_cover_and_3mf_follow_its_state_once(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer, cover, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    _set_print(hass, bambu, "running")
    hass.states.async_set(bambu.entity_ids["cover_image"], "2026-10-26T08:18:00+00:00")
    _cache_model(hass, "12958658-Christmas Moose.gcode.3mf", "2026-10-26T08:18:00+00:00")
    await _set_up(hass)

    await _tick(hass, freezer, 31)

    assert webhook.call_count == 3
    assert webhook.mock_calls[0][2]["status"] == "running"
    cover_form = _form(webhook.mock_calls[1][2])
    assert cover_form["kind"] == "cover_image"
    assert cover_form["printer_serial"] == SERIAL
    assert cover_form["start_time"] == START
    assert cover_form["file"] == ("cover.png", PNG)
    model_form = _form(webhook.mock_calls[2][2])
    assert model_form["kind"] == "model"
    assert model_form["task_name"] == "Christmas Moose"
    assert model_form["file"] == ("12958658-Christmas Moose.gcode.3mf", b"3mf-bytes")

    webhook.mock_calls.clear()
    await _tick(hass, freezer, 300)

    assert webhook.call_count == 1, "the heartbeat sends the state, not the files again"


async def test_no_files_are_sent_before_the_print_is_running(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer, cover, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    _set_print(hass, bambu, "prepare")
    hass.states.async_set(bambu.entity_ids["cover_image"], "2026-10-26T08:18:00+00:00")
    _cache_model(hass, "Christmas Moose.3mf", "2026-10-26T08:18:00+00:00")
    await _set_up(hass)

    await _tick(hass, freezer, 31)

    assert webhook.call_count == 1


async def test_a_3mf_of_another_or_an_earlier_print_is_not_sent(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    _set_print(hass, bambu, "running")
    _cache_model(hass, "100-Benchy.gcode.3mf", "2026-10-26T08:18:00+00:00")
    _cache_model(hass, "200-Christmas Moose.gcode.3mf", "2026-10-25T20:00:00+00:00")
    await _set_up(hass)

    await _tick(hass, freezer, 31)

    assert webhook.call_count == 1


async def test_a_file_the_webhook_refused_is_sent_again_with_the_next_state(
    hass: HomeAssistant, bambu: BambuSetup, aioclient_mock: AiohttpClientMocker, freezer, cover
) -> None:
    _set_print(hass, bambu, "running")
    hass.states.async_set(bambu.entity_ids["cover_image"], "2026-10-26T08:18:00+00:00")
    aioclient_mock.post(URL, json={"ok": True})
    await _set_up(hass)
    with patch(
        "custom_components.bambu_printer_webhook.manager.WebhookClient.async_post_file",
        AsyncMock(return_value=False),
    ) as post_file:
        await _tick(hass, freezer, 31)
        await _tick(hass, freezer, 300)

    assert post_file.await_count == 2


async def test_its_entities_show_the_last_delivery_and_any_problem(
    hass: HomeAssistant, bambu: BambuSetup, aioclient_mock: AiohttpClientMocker, freezer
) -> None:
    aioclient_mock.post(URL, json={"ok": True})
    entry = await _set_up(hass)
    registry = er.async_get(hass)
    delivered = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_delivered")
    problem = registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_delivery_problem")

    assert hass.states.get(delivered).state == "unknown"
    assert hass.states.get(problem).state == "unknown"

    await _tick(hass, freezer, 31)

    assert hass.states.get(delivered).state != "unknown"
    assert hass.states.get(problem).state == "off"

    aioclient_mock.clear_requests()
    aioclient_mock.post(URL, status=401)
    await _tick(hass, freezer, 300)

    assert hass.states.get(problem).state == "on"
    assert hass.states.get(problem).attributes["last_error"] == "HTTP 401"


async def test_a_restart_does_not_send_the_same_files_again(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer, cover, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    _set_print(hass, bambu, "running")
    hass.states.async_set(bambu.entity_ids["cover_image"], "2026-10-26T08:18:00+00:00")
    _cache_model(hass, "12958658-Christmas Moose.gcode.3mf", "2026-10-26T08:18:00+00:00")
    entry = await _set_up(hass)
    await _tick(hass, freezer, 31)
    assert webhook.call_count == 3
    await _tick(hass, freezer, 11)
    assert await hass.config_entries.async_unload(entry.entry_id)
    webhook.mock_calls.clear()

    # ha-bambulab sets the same cover again, and touches the cached 3mf.
    hass.states.async_set(bambu.entity_ids["cover_image"], "2026-10-26T09:00:00+00:00")
    _cache_model(hass, "12958658-Christmas Moose.gcode.3mf", "2026-10-26T09:00:00+00:00")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _tick(hass, freezer, 31)

    assert webhook.call_count == 1, "only the state goes out again"


async def test_progress_waits_for_the_interval_but_a_status_change_goes_out_soon(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    _set_print(hass, bambu, "running")
    hass.states.async_set(bambu.entity_ids["print_progress"], "10")
    await _set_up(hass)
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()

    for progress in ("11", "12", "13"):
        hass.states.async_set(bambu.entity_ids["print_progress"], progress)
        await hass.async_block_till_done()
        await _tick(hass, freezer, 60)

    assert webhook.call_count == 0, "a new layer or percentage waits for the interval"

    hass.states.async_set(bambu.entity_ids["print_status"], "finish")
    await hass.async_block_till_done()
    await _tick(hass, freezer, 31)

    assert webhook.call_count == 1
    assert webhook.mock_calls[0][2]["status"] == "finish"


async def test_the_interval_can_be_changed_in_the_options(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    entry = await _set_up(hass)
    await _tick(hass, freezer, 31)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"interval": 120})
    await hass.async_block_till_done()
    assert entry.options == {
        "interval": 120,
        "energy_sensors": {},
        "price_entity": None,
        "price_per_kwh": None,
        "history_since": None,
    }
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()

    await _tick(hass, freezer, 121)

    assert webhook.call_count == 1


async def test_each_printers_plug_meter_and_the_price_are_chosen_in_the_options(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    entry = await _set_up(hass)
    await _tick(hass, freezer, 31)
    hass.states.async_set("sensor.plug_energy", "3.2", {"unit_of_measurement": "kWh", "device_class": "energy"})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    meter_field = next(str(key) for key in result["data_schema"].schema if str(key).startswith("kWh-meter"))
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"interval": 300, meter_field: "sensor.plug_energy", "price_per_kwh": 0.27}
    )
    await hass.async_block_till_done()
    assert entry.options["energy_sensors"] == {SERIAL: "sensor.plug_energy"}
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()

    await _tick(hass, freezer, 301)

    assert webhook.mock_calls[-1][2]["energy"] == {"kwh": 3.2, "cost": 0.0}


async def test_each_kwh_is_paid_at_the_price_of_the_moment_it_was_used(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer
) -> None:
    hass.states.async_set("sensor.plug_energy", "10.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.tariff", "0.20", {"unit_of_measurement": "EUR/kWh"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"webhook_url": URL, "token": "secret"},
        unique_id=URL,
        options={"interval": 300, "energy_sensors": {SERIAL: "sensor.plug_energy"}, "price_entity": "sensor.tariff"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _tick(hass, freezer, 31)

    hass.states.async_set("sensor.plug_energy", "11.0", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    hass.states.async_set("sensor.tariff", "40", {"unit_of_measurement": "ct/kWh"})
    await hass.async_block_till_done()
    hass.states.async_set("sensor.plug_energy", "11.5", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    webhook.mock_calls.clear()
    await _tick(hass, freezer, 301)

    assert webhook.mock_calls[-1][2]["energy"] == {"kwh": 11.5, "cost": 0.4}


class _FakeStorage:
    """A printer's storage with three prints on it: two 3mf's, one known only
    by its day, and the G-code of another plate of a project."""

    files = [
        StoredFile("/cache/Old box.gcode.3mf", 9, datetime(2026, 3, 2, tzinfo=UTC), exact_time=False),
        StoredFile("/Benchy.gcode.3mf", 9, datetime(2026, 9, 1, 8, 30, tzinfo=UTC)),
        StoredFile("/cache/Benchy_plate_2.gcode", 9, datetime(2026, 9, 2, 10, 0, tzinfo=UTC), project="Benchy"),
    ]
    opened: list[tuple[str, str]] = []
    since: list[datetime | None] = []

    def __init__(self, host: str, access_code: str, _timezone: object = None) -> None:
        _FakeStorage.opened.append((host, access_code))

    def stored_files(self, _now: datetime, since: datetime | None = None) -> list[StoredFile]:
        _FakeStorage.since.append(since)
        return list(self.files)

    def read(self, _path: str) -> bytes:
        return b"not a zip"

    def read_head(self, _path: str) -> bytes:
        return b"; total filament weight [g] : 12.5\n; model printing time: 1h; total estimated time: 1h 2m 3s\n"

    def close(self) -> None:
        pass


@pytest.fixture
def storage():
    _FakeStorage.opened = []
    _FakeStorage.since = []
    with patch("custom_components.bambu_printer_webhook.manager.PrinterStorage", _FakeStorage):
        yield _FakeStorage


def _history_posts(webhook: AiohttpClientMocker) -> list[dict[str, object]]:
    return [
        _form(call[2])
        for call in webhook.mock_calls
        if isinstance(call[2], FormData) and _form(call[2])["kind"] == "history"
    ]


async def test_the_prints_on_the_printers_storage_are_sent_once(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer, storage
) -> None:
    hass.config_entries.async_update_entry(bambu.entry, options={"host": "192.168.1.50", "access_code": "12345678"})
    hass.states.async_set(bambu.entity_ids["print_status"], "idle")
    hass.states.async_set(bambu.entity_ids["online"], "on")
    await _set_up(hass, {"history_since": "2026-09-01"})

    await _tick(hass, freezer, 31)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert storage.opened == [("192.168.1.50", "12345678")]
    posts = _history_posts(webhook)
    assert [post["task_name"] for post in posts] == ["Old box", "Benchy", "Benchy"]
    assert posts[0]["file_name"] == "/cache/Old box.gcode.3mf"
    assert posts[0]["modified"] == "2026-03-02", "only the day, as that is all the storage gave"
    assert "file" not in posts[0], "a file that is no 3mf has no picture"
    assert posts[1]["modified"] == "2026-09-01T08:30:00+00:00"
    assert (posts[2]["file_name"], posts[2]["weight_grams"], posts[2]["print_seconds"]) == (
        "/cache/Benchy_plate_2.gcode",
        "12.5",
        "3723",
    )
    assert storage.since == [datetime(2026, 9, 1, tzinfo=dt_util.DEFAULT_TIME_ZONE)]

    webhook.mock_calls.clear()
    await _tick(hass, freezer, 301)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _history_posts(webhook) == []
    assert storage.opened == [("192.168.1.50", "12345678")]


async def test_the_storage_is_not_read_while_the_printer_prints(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer, storage
) -> None:
    hass.config_entries.async_update_entry(bambu.entry, options={"host": "192.168.1.50", "access_code": "12345678"})
    hass.states.async_set(bambu.entity_ids["print_status"], "running")
    hass.states.async_set(bambu.entity_ids["online"], "on")
    await _set_up(hass, {"history_since": "2026-09-01"})

    await _tick(hass, freezer, 31)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert storage.opened == []


async def test_without_a_day_to_import_from_the_storage_is_left_alone(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer, storage
) -> None:
    hass.config_entries.async_update_entry(bambu.entry, options={"host": "192.168.1.50", "access_code": "12345678"})
    hass.states.async_set(bambu.entity_ids["print_status"], "idle")
    hass.states.async_set(bambu.entity_ids["online"], "on")
    await _set_up(hass)

    await _tick(hass, freezer, 31)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert storage.opened == []
    assert _history_posts(webhook) == []


async def test_without_a_local_address_the_storage_is_left_alone(
    hass: HomeAssistant, bambu: BambuSetup, webhook: AiohttpClientMocker, freezer, storage
) -> None:
    hass.states.async_set(bambu.entity_ids["print_status"], "idle")
    hass.states.async_set(bambu.entity_ids["online"], "on")
    await _set_up(hass, {"history_since": "2026-09-01"})

    await _tick(hass, freezer, 31)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert storage.opened == []


_REQUEST = {
    "kind": "print_files",
    "print_job_id": 582,
    "task_name": "Christmas Moose",
    "start_time": "2026-10-26T08:17:00+00:00",
    "plate": None,
    "weight_grams": None,
}


async def test_a_3mf_asked_for_in_the_reply_is_sent_once_from_the_cache_closest_to_its_print(
    hass: HomeAssistant, bambu: BambuSetup, aioclient_mock: AiohttpClientMocker, freezer, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    _set_print(hass, bambu, "finish")
    _cache_model(hass, "100-Christmas Moose.gcode.3mf", "2026-10-26T08:18:00+00:00")
    _cache_model(hass, "200-Christmas Moose.gcode.3mf", "2026-11-01T09:00:00+00:00")
    aioclient_mock.post(URL, json={"ok": True, "requests": [_REQUEST]})
    await _set_up(hass)

    await _tick(hass, freezer, 31)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _tick(hass, freezer, 301)
    await hass.async_block_till_done(wait_background_tasks=True)

    models = [
        _form(call[2])
        for call in aioclient_mock.mock_calls
        if isinstance(call[2], FormData) and _form(call[2])["kind"] == "model"
    ]
    assert len(models) == 1, "asked for again, but it was answered"
    assert models[0]["print_job_id"] == "582"
    assert models[0]["file"][0] == "100-Christmas Moose.gcode.3mf"


async def test_a_3mf_of_another_weight_than_the_print_asked_for_is_not_sent(
    hass: HomeAssistant, bambu: BambuSetup, aioclient_mock: AiohttpClientMocker, freezer, tmp_path
) -> None:
    hass.config.config_dir = str(tmp_path)
    _set_print(hass, bambu, "finish")
    path = _cache_model(hass, "100-Christmas Moose.gcode.3mf", "2026-10-26T08:18:00+00:00")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "Metadata/slice_info.config",
            '<config><plate><metadata key="index" value="1"/><metadata key="weight" value="211.77"/></plate></config>',
        )
        archive.writestr("Metadata/plate_1.gcode", "G28")
    path.write_bytes(buffer.getvalue())
    aioclient_mock.post(URL, json={"ok": True, "requests": [{**_REQUEST, "plate": 1, "weight_grams": 106}]})
    await _set_up(hass)

    await _tick(hass, freezer, 31)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not any(
        isinstance(call[2], FormData) and _form(call[2])["kind"] == "model" for call in aioclient_mock.mock_calls
    )
