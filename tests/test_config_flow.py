"""Setting up and changing the webhook."""

from __future__ import annotations

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bambu_printer_webhook.const import DOMAIN

URL = "https://example.test/printer-state"


async def _submit(hass: HomeAssistant, user_input: dict[str, str]):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_creates_an_entry_once_the_webhook_answers_a_ping(
    hass: HomeAssistant, bambu, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(URL, json={"ok": True})

    result = await _submit(hass, {"webhook_url": f" {URL} ", "token": " secret "})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "example.test"
    assert result["data"] == {"webhook_url": URL, "token": "secret"}
    _method, _url, body, headers = aioclient_mock.mock_calls[0]
    assert body == {"schema_version": 1, "ping": True}
    assert headers["Authorization"] == "Bearer secret"


async def test_rejects_an_address_that_is_not_http(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    result = await _submit(hass, {"webhook_url": "ftp://example.test", "token": "secret"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"webhook_url": "invalid_url"}
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    ("status", "errors"),
    [
        (401, {"token": "invalid_auth"}),
        (405, {"webhook_url": "wrong_endpoint"}),
        (404, {"webhook_url": "wrong_endpoint"}),
        (500, {"base": "unexpected_response"}),
    ],
)
async def test_explains_why_the_webhook_refused_the_ping(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, errors: dict[str, str]
) -> None:
    aioclient_mock.post(URL, status=status)

    result = await _submit(hass, {"webhook_url": URL, "token": "secret"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors


async def test_says_when_the_webhook_cannot_be_reached(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(URL, exc=aiohttp.ClientConnectionError())

    result = await _submit(hass, {"webhook_url": URL, "token": "secret"})

    assert result["errors"] == {"base": "cannot_connect"}


async def test_the_same_webhook_cannot_be_added_twice(
    hass: HomeAssistant, bambu, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(URL, json={"ok": True})

    for expected in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT):
        result = await _submit(hass, {"webhook_url": URL, "token": "secret"})
        assert result["type"] is expected
