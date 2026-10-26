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
