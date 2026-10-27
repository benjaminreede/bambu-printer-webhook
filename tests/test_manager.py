"""Sending printer states: settling, heartbeat and failures."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import FormData
from homeassistant.components.image import Image
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bambu_printer_webhook.const import DOMAIN

from .conftest import SERIAL, BambuSetup, tray_attributes

URL = "https://example.test/printer-state"


async def _set_up(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"webhook_url": URL, "token": "secret"}, unique_id=URL)
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
    assert entry.options == {"interval": 120}
    await _tick(hass, freezer, 31)
    webhook.mock_calls.clear()

    await _tick(hass, freezer, 121)

    assert webhook.call_count == 1
