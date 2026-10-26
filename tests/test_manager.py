"""Sending printer states: settling, heartbeat and failures."""

from __future__ import annotations

from datetime import timedelta

import pytest
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

    # Inserting a spool: the previous spool, then the new one unmeasured,
    # then its real reading, each within the settle time of the last.
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="AAAA", remain=0))
    await hass.async_block_till_done()
    await _tick(hass, freezer, 5)
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="BBBB", remain=-1))
    await hass.async_block_till_done()
    await _tick(hass, freezer, 15)
    hass.states.async_set(tray, "Bambu PLA Matte", tray_attributes(tray_uuid="BBBB", remain=83))
    await hass.async_block_till_done()
    await _tick(hass, freezer, 19)

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
