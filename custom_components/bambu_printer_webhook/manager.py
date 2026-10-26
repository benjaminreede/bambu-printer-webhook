"""Send each printer's state to the webhook when it changes and on a heartbeat."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import aiohttp
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    BAMBU_DOMAIN,
    HEARTBEAT_INTERVAL,
    REDISCOVERY_DELAY,
    REQUEST_TIMEOUT_SECONDS,
    SETTLE_DELAY,
    STARTUP_DELAY,
)
from .discovery import PrinterEntities, find_printers
from .state import build_state

_LOGGER = logging.getLogger(__name__)


class PrinterSender:
    """Watches one printer and sends its state once it has settled."""

    def __init__(self, hass: HomeAssistant, printer: PrinterEntities, send: Callable[[dict[str, Any]], Any]) -> None:
        self._hass = hass
        self.printer = printer
        self._send = send
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._pending: CALLBACK_TYPE | None = None

    @callback
    def start(self, first_send_delay: float) -> None:
        """Start listening; the first state goes out after ``first_send_delay`` seconds."""
        self._unsubscribers.append(
            async_track_state_change_event(self._hass, sorted(self.printer.entity_ids), self._on_change)
        )
        self._unsubscribers.append(async_track_time_interval(self._hass, self._on_heartbeat, HEARTBEAT_INTERVAL))
        self._schedule(first_send_delay)

    @callback
    def stop(self) -> None:
        """Stop listening and drop a send that was still waiting."""
        self._cancel_pending()
        while self._unsubscribers:
            self._unsubscribers.pop()()

    @callback
    def _on_change(self, _event: Event) -> None:
        # Every change restarts the wait, so a burst of intermediate states
        # (a spool being inserted) only goes out once it has ended.
        self._schedule(SETTLE_DELAY.total_seconds())

    @callback
    def _on_heartbeat(self, _now: Any) -> None:
        if self._pending is None:
            self._hass.async_create_task(self.async_send_now())

    @callback
    def _schedule(self, delay: float) -> None:
        self._cancel_pending()
        self._pending = async_call_later(self._hass, delay, self._on_settled)

    @callback
    def _on_settled(self, _now: Any) -> None:
        self._pending = None
        self._hass.async_create_task(self.async_send_now())

    @callback
    def _cancel_pending(self) -> None:
        if self._pending is not None:
            self._pending()
            self._pending = None

    async def async_send_now(self) -> None:
        """Build the printer's current state and send it."""
        document = build_state(self.printer, self._hass.states.get, dt_util.utcnow())
        await self._send(document)


class WebhookManager:
    """Finds every Bambu Lab printer and keeps a sender running for each."""

    def __init__(self, hass: HomeAssistant, url: str, token: str) -> None:
        self._hass = hass
        self._url = url
        self._token = token
        self._senders: dict[str, PrinterSender] = {}
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._rediscovery: CALLBACK_TYPE | None = None

    @property
    def senders(self) -> dict[str, PrinterSender]:
        """The running senders by printer serial."""
        return self._senders

    @callback
    def async_start(self) -> None:
        """Start once Home Assistant is up, when ha-bambulab has registered its entities."""
        self._unsubscribers.append(
            self._hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_updated)
        )
        if self._hass.state is CoreState.running:
            self._discover(STARTUP_DELAY.total_seconds())
        else:
            self._unsubscribers.append(
                self._hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED,
                    lambda _event: self._discover(STARTUP_DELAY.total_seconds()),
                )
            )

    @callback
    def async_stop(self) -> None:
        """Stop every sender and listener."""
        if self._rediscovery is not None:
            self._rediscovery()
            self._rediscovery = None
        while self._unsubscribers:
            self._unsubscribers.pop()()
        for sender in self._senders.values():
            sender.stop()
        self._senders.clear()

    @callback
    def _on_registry_updated(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        # A printer or AMS added, removed or re-set up in ha-bambulab: look
        # the printers up again once the registry has gone quiet.
        entity = er.async_get(self._hass).async_get(event.data["entity_id"])
        if entity is not None and entity.platform != BAMBU_DOMAIN:
            return
        if self._rediscovery is not None:
            self._rediscovery()
        self._rediscovery = async_call_later(self._hass, REDISCOVERY_DELAY.total_seconds(), self._on_rediscovery)

    @callback
    def _on_rediscovery(self, _now: Any) -> None:
        self._rediscovery = None
        self._discover(SETTLE_DELAY.total_seconds())

    @callback
    def _discover(self, first_send_delay: float) -> None:
        printers = {printer.serial: printer for printer in find_printers(self._hass)}

        for serial in list(self._senders):
            unchanged = serial in printers and printers[serial] == self._senders[serial].printer
            if not unchanged:
                self._senders.pop(serial).stop()

        for serial, printer in printers.items():
            if serial in self._senders:
                continue
            sender = PrinterSender(self._hass, printer, self._post)
            sender.start(first_send_delay)
            self._senders[serial] = sender

        if not printers:
            _LOGGER.warning("No Bambu Lab printers found; is the Bambu Lab integration set up?")

    async def _post(self, document: dict[str, Any]) -> None:
        serial = document["printer"]["serial"]
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                self._url,
                json=document,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as response:
                if response.status >= 300:
                    _LOGGER.warning(
                        "Webhook answered %s for printer %s: %s",
                        response.status,
                        serial,
                        (await response.text())[:200],
                    )
        except (aiohttp.ClientError, TimeoutError) as error:
            # No retry: the next change or heartbeat sends the state again.
            _LOGGER.warning("Could not send the state of printer %s: %s", serial, error)
