"""Send each printer's state to the webhook when it changes and on a heartbeat.

While a print runs, its cover image and 3mf file follow each state that was
delivered, until each has been delivered once (again when it changes).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.components.image import async_get_image
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
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
    FILE_COVER_IMAGE,
    FILE_MODEL,
    FILE_REQUEST_TIMEOUT_SECONDS,
    FILE_STATUSES,
    HEARTBEAT_INTERVAL,
    MAX_MODEL_BYTES,
    REDISCOVERY_DELAY,
    REQUEST_TIMEOUT_SECONDS,
    SETTLE_DELAY,
    STARTUP_DELAY,
)
from .discovery import PrinterEntities, find_printers
from .files import cache_directories, find_model_file
from .state import build_state

_LOGGER = logging.getLogger(__name__)


class WebhookClient:
    """Posts states (JSON) and files (multipart) to the configured webhook."""

    def __init__(self, hass: HomeAssistant, url: str, token: str) -> None:
        self._hass = hass
        self._url = url
        self._token = token

    async def async_post_state(self, document: dict[str, Any]) -> bool:
        """Send a state; whether it was delivered."""
        return await self._post(
            f"the state of printer {document['printer']['serial']}",
            REQUEST_TIMEOUT_SECONDS,
            json=document,
        )

    async def async_post_file(
        self, kind: str, fields: dict[str, str], filename: str, content_type: str, content: bytes
    ) -> bool:
        """Send a print's file as a multipart form; whether it was delivered."""
        form = aiohttp.FormData()
        form.add_field("schema_version", "1")
        form.add_field("kind", kind)
        for name, value in fields.items():
            form.add_field(name, value)
        form.add_field("file", content, filename=filename, content_type=content_type)
        return await self._post(
            f"the {kind} of printer {fields['printer_serial']}",
            FILE_REQUEST_TIMEOUT_SECONDS,
            data=form,
        )

    async def _post(self, what: str, timeout: float, **body: Any) -> bool:
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                self._url,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=timeout),
                **body,
            ) as response:
                if response.status >= 300:
                    _LOGGER.warning(
                        "Webhook answered %s to %s: %s", response.status, what, (await response.text())[:200]
                    )
                    return False
                return True
        except (aiohttp.ClientError, TimeoutError) as error:
            # No retry: the next change or heartbeat sends it again.
            _LOGGER.warning("Could not send %s: %s", what, error)
            return False


class PrinterSender:
    """Watches one printer and sends its state once it has settled."""

    def __init__(self, hass: HomeAssistant, printer: PrinterEntities, client: WebhookClient) -> None:
        self._hass = hass
        self.printer = printer
        self._client = client
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._pending: CALLBACK_TYPE | None = None
        self._lock = asyncio.Lock()
        self._sent_cover: tuple[Any, ...] | None = None
        self._sent_model: tuple[Any, ...] | None = None

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
        """Build the printer's current state and send it, then the print's files."""
        async with self._lock:
            document = build_state(self.printer, self._hass.states.get, dt_util.utcnow())
            if not await self._client.async_post_state(document):
                return
            # Only after a delivered state showing the print running, so the
            # receiver knows the print the files belong to.
            print_ = document["print"]
            if document["status"] in FILE_STATUSES and print_ and print_["start_time"]:
                await self._async_send_cover(print_)
                await self._async_send_model(print_)

    def _file_fields(self, print_: dict[str, Any]) -> dict[str, str]:
        return {
            "printer_serial": self.printer.serial,
            "task_name": print_["task_name"] or "",
            "start_time": print_["start_time"],
        }

    async def _async_send_cover(self, print_: dict[str, Any]) -> None:
        entity_id = self.printer.cover_image
        state = self._hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        # The image entity's state is when its picture last changed.
        sent = (print_["start_time"], state.state)
        if sent == self._sent_cover:
            return
        try:
            image = await async_get_image(self._hass, entity_id)
        except HomeAssistantError:
            _LOGGER.debug("No cover image for printer %s yet", self.printer.serial)
            return
        # ha-bambulab says JPEG, but the cover from a 3mf is a PNG.
        is_png = image.content.startswith(b"\x89PNG")
        if await self._client.async_post_file(
            FILE_COVER_IMAGE,
            self._file_fields(print_),
            "cover.png" if is_png else "cover.jpg",
            "image/png" if is_png else image.content_type,
            image.content,
        ):
            self._sent_cover = sent

    async def _async_send_model(self, print_: dict[str, Any]) -> None:
        if not print_["task_name"]:
            return
        started = dt_util.parse_datetime(print_["start_time"])
        if started is None:
            return
        directories = cache_directories(
            Path(self._hass.config.config_dir), self._hass.config.media_dirs.get("local"), self.printer.serial
        )
        model = await self._hass.async_add_executor_job(find_model_file, directories, print_["task_name"], started)
        if model is None:
            return
        sent = (print_["start_time"], model.path, model.size, model.modified)
        if sent == self._sent_model:
            return
        if model.size > MAX_MODEL_BYTES:
            _LOGGER.warning("Not sending %s: larger than %s bytes", model.path.name, MAX_MODEL_BYTES)
            self._sent_model = sent
            return
        content = await self._hass.async_add_executor_job(model.path.read_bytes)
        if await self._client.async_post_file(
            FILE_MODEL, self._file_fields(print_), model.path.name, "application/octet-stream", content
        ):
            self._sent_model = sent


class WebhookManager:
    """Finds every Bambu Lab printer and keeps a sender running for each."""

    def __init__(self, hass: HomeAssistant, url: str, token: str) -> None:
        self._hass = hass
        self._client = WebhookClient(hass, url, token)
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
            sender = PrinterSender(self._hass, printer, self._client)
            sender.start(first_send_delay)
            self._senders[serial] = sender

        if not printers:
            _LOGGER.warning("No Bambu Lab printers found; is the Bambu Lab integration set up?")
