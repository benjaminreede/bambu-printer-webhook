"""Setting up and changing the webhook."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bambu_printer_webhook.const import DOMAIN

URL = "https://example.test/printer-state"


async def test_creates_an_entry_for_a_valid_url_and_token(hass: HomeAssistant, bambu) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"webhook_url": f" {URL} ", "token": " secret "}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "example.test"
    assert result["data"] == {"webhook_url": URL, "token": "secret"}


async def test_rejects_an_address_that_is_not_http(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"webhook_url": "ftp://example.test", "token": "secret"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"webhook_url": "invalid_url"}


async def test_the_same_webhook_cannot_be_added_twice(hass: HomeAssistant, bambu) -> None:
    for expected in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"webhook_url": URL, "token": "secret"}
        )
        assert result["type"] is expected
