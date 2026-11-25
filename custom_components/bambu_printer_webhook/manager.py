"""Send each printer's state to the webhook when it changes and on a heartbeat.

While a print runs, its cover image and 3mf file follow each state that was
delivered, until each has been delivered once (again when it changes).

Once per printer, the prints it still has on its storage are sent too, so the
receiver can add the prints from before the integration was set up (see
history.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
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
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BAMBU_DOMAIN,
    DOMAIN,
    FILE_COVER_IMAGE,
    FILE_HISTORY,
    FILE_MODEL,
    FILE_REQUEST_TIMEOUT_SECONDS,
    FILE_STATUSES,
    MAX_MODEL_BYTES,
    NOT_READING_STATUSES,
    REDISCOVERY_DELAY,
    REQUEST_PRINT_FILES,
    REQUEST_TIMEOUT_SECONDS,
    RUNOUT_STAGE,
    SETTLE_DELAY,
    SETTLED_SEND_DELAY,
    STARTUP_DELAY,
)
from .discovery import PrinterEntities, find_printers
from .energy import CostMeter
from .files import cache_directories, find_model_file, find_model_file_of
from .history import PrinterStorage, StoredFile, read_gcode_head, read_model_info
from .state import Price, SettledTray, build_state, meter_kwh, price_per_kwh

_LOGGER = logging.getLogger(__name__)


def _layers_of(print_: dict[str, Any]) -> dict[str, Any]:
    """The print's layer count, as a 3mf has to match it."""
    return {"total_layers": print_["total_layers"]} if print_.get("total_layers") else {}


def _modified(stored: StoredFile) -> str:
    if stored.modified is None:
        return ""
    return stored.modified.isoformat() if stored.exact_time else stored.modified.date().isoformat()


class WebhookClient:
    """Posts states (JSON) and files (multipart) to the configured webhook.

    Keeps how the last request went, for the integration's own entities.
    """

    def __init__(self, hass: HomeAssistant, url: str, token: str) -> None:
        self._hass = hass
        self._url = url
        self._token = token
        self._listeners: list[Callable[[], None]] = []
        self.last_delivered: datetime | None = None
        """When a request was last answered with 2xx."""
        self.last_error: str | None = None
        """Why the last request failed, or None when it was delivered."""
        self.attempted = False

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call ``listener`` after every request; returns a callable that removes it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def _record(self, error: str | None) -> None:
        self.attempted = True
        self.last_error = error
        if error is None:
            self.last_delivered = dt_util.utcnow()
        for listener in list(self._listeners):
            listener()

    async def async_post_state(self, document: dict[str, Any]) -> dict[str, Any] | None:
        """Send a state; the receiver's reply when it was delivered (empty
        when it gave none), else None."""
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
        return (
            await self._post(
                f"the {kind} of printer {fields['printer_serial']}",
                FILE_REQUEST_TIMEOUT_SECONDS,
                data=form,
            )
            is not None
        )

    async def async_post_history(self, fields: dict[str, str], thumbnail: bytes | None) -> bool:
        """Send a print found on the printer's storage; whether it was delivered."""
        form = aiohttp.FormData()
        form.add_field("schema_version", "1")
        form.add_field("kind", FILE_HISTORY)
        for name, value in fields.items():
            form.add_field(name, value)
        if thumbnail is not None:
            form.add_field("file", thumbnail, filename="plate.png", content_type="image/png")
        return (
            await self._post(
                f"a stored print of printer {fields['printer_serial']}", FILE_REQUEST_TIMEOUT_SECONDS, data=form
            )
            is not None
        )

    async def _post(self, what: str, timeout: float, **body: Any) -> dict[str, Any] | None:
        """The receiver's JSON reply when delivered (empty without one), else None."""
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
                    self._record(f"HTTP {response.status}")
                    return None
                self._record(None)
                try:
                    reply = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    reply = None
                return reply if isinstance(reply, dict) else {}
        except (aiohttp.ClientError, TimeoutError) as error:
            # No retry: the next change or heartbeat sends it again.
            _LOGGER.warning("Could not send %s: %s", what, error)
            self._record(str(error) or type(error).__name__)
            return None


class SentFiles:
    """Which cover and 3mf were delivered for each printer's print, kept across restarts.

    Without it every restart of Home Assistant would send the running
    print's files again, a 3mf of tens of megabytes included.
    """

    SAVE_DELAY = 10

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, dict[str, list[Any]]]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.sent_files")
        self._sent: dict[str, dict[str, list[Any]]] = {}

    async def async_load(self) -> None:
        self._sent = await self._store.async_load() or {}

    def get(self, serial: str, kind: str) -> list[Any] | None:
        return self._sent.get(serial, {}).get(kind)

    @callback
    def set(self, serial: str, kind: str, key: list[Any]) -> None:
        self._sent.setdefault(serial, {})[kind] = key
        self._store.async_delay_save(lambda: self._sent, self.SAVE_DELAY)


class HistoryImports:
    """Which stored prints were delivered for each printer, and whether all were.

    Kept across restarts, so the import resumes where it stopped and runs
    only once.
    """

    SAVE_DELAY = 10

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, dict[str, Any]]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.history")
        self._imports: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        self._imports = await self._store.async_load() or {}

    def done(self, serial: str) -> bool:
        return bool(self._imports.get(serial, {}).get("done"))

    def sent(self, serial: str, stored: StoredFile) -> bool:
        return stored.key in self._imports.get(serial, {}).get("sent", [])

    @callback
    def mark_sent(self, serial: str, stored: StoredFile) -> None:
        self._imports.setdefault(serial, {"done": False, "sent": []})["sent"].append(stored.key)
        self._store.async_delay_save(lambda: self._imports, self.SAVE_DELAY)

    @callback
    def mark_done(self, serial: str) -> None:
        self._imports.setdefault(serial, {"done": False, "sent": []})["done"] = True
        self._store.async_delay_save(lambda: self._imports, self.SAVE_DELAY)


class EnergyCosts:
    """What each printer's plug has cost so far (see energy.py), kept across restarts."""

    SAVE_DELAY = 60

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, dict[str, Any]]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.energy_costs")
        self._meters: dict[str, CostMeter] = {}

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self._meters = {serial: CostMeter(meter.get("kwh"), meter.get("cost", 0.0)) for serial, meter in stored.items()}

    def cost(self, serial: str) -> float:
        meter = self._meters.get(serial)
        return meter.cost if meter is not None else 0.0

    @callback
    def read(self, serial: str, kwh: float | None, price_per_kwh: float | None) -> None:
        if self._meters.setdefault(serial, CostMeter()).read(kwh, price_per_kwh):
            self._store.async_delay_save(self._data, self.SAVE_DELAY)

    def _data(self) -> dict[str, dict[str, Any]]:
        return {serial: {"kwh": meter.kwh, "cost": meter.cost} for serial, meter in self._meters.items()}


class PrinterSender:
    """Watches one printer and sends its state once it has settled.

    It also follows each AMS slot reading a spool: while no print is
    underway, the AMS makes a slot active to read a newly inserted spool's
    RFID tag and lets go once it has read both the tag and the remaining
    percentage. Such a slot is reported ``settled`` for that spool. That
    moment, and a slot being emptied, go out right away instead of after
    the quiet period: neither is followed by intermediate states.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        printer: PrinterEntities,
        client: WebhookClient,
        sent_files: SentFiles,
        interval: timedelta,
        price: Price | None = None,
        history: HistoryImports | None = None,
        energy_costs: EnergyCosts | None = None,
        history_since: datetime | None = None,
    ) -> None:
        self._hass = hass
        self._history_since = history_since
        self.printer = printer
        self._price = price
        self._energy_costs = energy_costs
        self._history = history
        self._importing = False
        self._answering: set[Any] = set()
        self._answered: set[Any] = set()
        self._refused_model: list[Any] | None = None
        self._wanted_model: dict[str, Any] | None = None
        """A running print whose cached 3mf was another print's: its own 3mf
        is fetched from the printer's storage once that can be read."""
        self._fetching_model = False
        self._reported_no_access = False
        self._client = client
        self._sent_files = sent_files
        self._interval = interval
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._pending: CALLBACK_TYPE | None = None
        self._lock = asyncio.Lock()
        self._sent_significant: tuple[Any, ...] | None = None
        self._reading: set[tuple[int, int]] = set()
        self._empty: dict[tuple[int, int], bool] = {}
        self._settled: set[SettledTray] = set()
        self._sending_soon = False

    @callback
    def start(self, first_send_delay: float) -> None:
        """Start listening; the first state goes out after ``first_send_delay`` seconds."""
        self._unsubscribers.append(
            async_track_state_change_event(self._hass, sorted(self.printer.entity_ids), self._on_change)
        )
        self._unsubscribers.append(async_track_time_interval(self._hass, self._on_heartbeat, self._interval))
        if self.printer.energy is not None and self._pays_energy:
            # Not a reason to send: the reading goes out with the next state.
            self._unsubscribers.append(
                async_track_state_change_event(self._hass, [self.printer.energy], self._on_meter)
            )
            self._on_meter(None)
        # What each slot holds now, so the first change can be compared.
        self._follow_trays(self._build_state())
        self._schedule(first_send_delay)

    @callback
    def stop(self) -> None:
        """Stop listening and drop a send that was still waiting."""
        self._cancel_pending()
        while self._unsubscribers:
            self._unsubscribers.pop()()

    @callback
    def _on_change(self, _event: Event) -> None:
        document = self._build_state()
        if self._follow_trays(document):
            self._schedule(SETTLED_SEND_DELAY.total_seconds())
            self._sending_soon = True
        elif self._sending_soon:
            return
        # A significant change is sent once the printer has been quiet for a
        # while; while that wait runs, every change restarts it, so a burst
        # of intermediate states (a spool being inserted) only goes out once
        # it has ended. Anything else (progress, layers, a spool's
        # percentage) waits for the interval.
        elif self._pending is not None or self._significant(document) != self._sent_significant:
            self._schedule(SETTLE_DELAY.total_seconds())

    @property
    def _pays_energy(self) -> bool:
        return self._energy_costs is not None and self._price is not None and self._price.configured

    @callback
    def _on_meter(self, _event: Event | None) -> None:
        """Pay a new reading of the plug's meter at the price of this moment."""
        assert self._energy_costs is not None and self._price is not None and self.printer.energy is not None
        get_state = self._hass.states.get
        self._energy_costs.read(
            self.printer.serial, meter_kwh(get_state(self.printer.energy)), price_per_kwh(get_state, self._price)
        )

    def _build_state(self) -> dict[str, Any]:
        cost = self._energy_costs.cost(self.printer.serial) if self._pays_energy else None
        return build_state(self.printer, self._hass.states.get, dt_util.utcnow(), self._settled, cost)

    def _follow_trays(self, document: dict[str, Any]) -> bool:
        """Note which slots the AMS reads a spool in; whether a slot just
        finished reading or was emptied, which goes out right away."""
        reading_possible = document["status"] not in NOT_READING_STATUSES
        send_soon = False

        for unit in document["ams"]:
            for tray in unit["trays"]:
                slot = (unit["index"], tray["slot"])
                was_empty = self._empty.get(slot)
                self._empty[slot] = tray["empty"]
                # A slot's spool changed: whatever it read before is stale.
                self._settled = {
                    settled for settled in self._settled if settled[:2] != slot or settled[2] == tray.get("tray_uuid")
                }

                if tray["active"]:
                    # Reading starts while the slot still shows empty: the
                    # spool only appears once its tag has been read.
                    if reading_possible:
                        self._reading.add(slot)
                elif slot in self._reading:
                    if not tray["empty"] and tray["tray_uuid"] is not None and tray["remain"] == -1:
                        # Let go before the percentage came in: that
                        # reading still finishes this one.
                        continue
                    self._reading.discard(slot)
                    if not tray["empty"] and tray["tray_uuid"] is not None:
                        self._settled.add((*slot, tray["tray_uuid"]))
                    send_soon = True
                elif tray["empty"] and was_empty is False:
                    # Taken out: no reading follows, the slot is just empty.
                    send_soon = True

        # Offline, every slot reads empty because its entity is unavailable.
        return send_soon and document["online"]

    def _significant(self, document: dict[str, Any]) -> tuple[Any, ...]:
        """What in a state is worth sending right away: the printer's status,
        which print it runs, which spool each slot holds, and a spool that
        ran out."""
        print_ = document["print"] or {}
        trays = [tray for unit in document["ams"] for tray in unit["trays"]]
        if document["external_spool"]:
            trays.append(document["external_spool"])
        cover = self._hass.states.get(self.printer.cover_image) if self.printer.cover_image else None
        return (
            document["online"],
            document["status"],
            print_.get("task_name"),
            print_.get("start_time"),
            tuple((tray.get("empty"), tray.get("active"), tray.get("tray_uuid")) for tray in trays),
            cover.state if cover else None,
            document["stage"] == RUNOUT_STAGE,
            (document["print_error"] or {}).get("code"),
        )

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
        self._sending_soon = False
        self._hass.async_create_task(self.async_send_now())

    @callback
    def _cancel_pending(self) -> None:
        if self._pending is not None:
            self._pending()
            self._pending = None
        self._sending_soon = False

    async def async_send_now(self) -> None:
        """Build the printer's current state and send it, then the print's files."""
        async with self._lock:
            document = self._build_state()
            reply = await self._client.async_post_state(document)
            if reply is None:
                return
            self._sent_significant = self._significant(document)
            self._answer_requests(reply.get("requests"), document)
            # Only after a delivered state showing the print running, so the
            # receiver knows the print the files belong to.
            print_ = document["print"]
            if document["status"] in FILE_STATUSES and print_ and print_["start_time"]:
                await self._async_send_cover(print_)
                await self._async_send_model(print_)
            elif (
                self._wanted_model is not None
                and not self._fetching_model
                and document["online"]
                and document["status"] not in NOT_READING_STATUSES
            ):
                # Its own storage is readable again: the print has ended.
                self._fetching_model = True
                self._hass.async_create_background_task(
                    self._async_send_stored_model_of(self._wanted_model),
                    f"{DOMAIN} 3mf of the last print of {self.printer.serial}",
                )
            elif self._may_import(document):
                # After a delivered state, so the receiver knows the printer.
                self._importing = True
                self._hass.async_create_background_task(
                    self._async_import_history(), f"{DOMAIN} history of {self.printer.serial}"
                )

    def _answer_requests(self, requests: Any, document: dict[str, Any]) -> None:
        """Start on what the receiver asked for in its reply. The receiver asks
        again an hour later while it hasn't got the file; it is only looked
        for again when the printer's storage couldn't be read because it was
        printing, or after a restart."""
        for request in requests if isinstance(requests, list) else []:
            if not isinstance(request, dict) or request.get("kind") != REQUEST_PRINT_FILES:
                continue
            print_job_id = request.get("print_job_id")
            if print_job_id in self._answering or print_job_id in self._answered:
                continue
            self._answering.add(print_job_id)
            self._hass.async_create_background_task(
                self._async_send_requested_model(request, document), f"{DOMAIN} 3mf for print {print_job_id}"
            )

    async def _async_send_requested_model(self, request: dict[str, Any], document: dict[str, Any]) -> None:
        """Send the 3mf of a print the receiver asked for: from ha-bambulab's
        cache, else from the printer's storage while it isn't printing. Only
        a file of the same plate and weight as the print, as a later file of
        the same name can be sliced otherwise."""
        printing = document["status"] in NOT_READING_STATUSES
        done = False
        try:
            found = await self._async_cached_model(request)
            if found is None and not printing:
                found = await self._async_stored_model(request)
            done = found is not None or not printing
            if found is None:
                _LOGGER.info(
                    "No 3mf found for print %s of printer %s", request.get("print_job_id"), self.printer.serial
                )
                return
            filename, content = found
            await self._client.async_post_file(
                FILE_MODEL,
                {
                    "printer_serial": self.printer.serial,
                    "task_name": str(request.get("task_name") or ""),
                    "print_job_id": str(request.get("print_job_id")),
                },
                filename,
                "application/octet-stream",
                content,
            )
        except Exception:  # noqa: BLE001 - a failed lookup must not stop the states
            _LOGGER.warning("Could not look up a 3mf for printer %s", self.printer.serial, exc_info=True)
        finally:
            self._answering.discard(request.get("print_job_id"))
            if done:
                self._answered.add(request.get("print_job_id"))

    async def _async_cached_model(self, request: dict[str, Any]) -> tuple[str, bytes] | None:
        started = dt_util.parse_datetime(str(request.get("start_time") or ""))
        if not request.get("task_name") or started is None:
            return None
        directories = cache_directories(
            Path(self._hass.config.config_dir), self._hass.config.media_dirs.get("local"), self.printer.serial
        )
        model = await self._hass.async_add_executor_job(find_model_file_of, directories, request["task_name"], started)
        if model is None or model.size > MAX_MODEL_BYTES:
            return None
        content = await self._hass.async_add_executor_job(model.path.read_bytes)
        return (model.path.name, content) if self._fits(request, content) else None

    async def _async_stored_model(self, request: dict[str, Any]) -> tuple[str, bytes] | None:
        access = self._storage_access()
        if access is None or not request.get("task_name"):
            return None
        wanted = str(request["task_name"]).strip().casefold()
        storage = await self._hass.async_add_executor_job(PrinterStorage, *access, dt_util.DEFAULT_TIME_ZONE)
        try:
            stored_files = await self._hass.async_add_executor_job(storage.stored_files, dt_util.utcnow())
            for stored in reversed(stored_files):
                if stored.is_plate or stored.task_name.casefold() != wanted or stored.size > MAX_MODEL_BYTES:
                    continue
                content = await self._hass.async_add_executor_job(storage.read, stored.path)
                if self._fits(request, content):
                    return stored.path.rsplit("/", 1)[-1], content
        finally:
            await self._hass.async_add_executor_job(storage.close)
        return None

    async def _async_send_stored_model_of(self, print_: dict[str, Any]) -> None:
        """Send the 3mf of a print that ended, from the printer's own storage:
        the one of its name with the layers the printer printed. Tried once;
        the file may have been sent again for a later print since."""
        try:
            found = await self._async_stored_model({"task_name": print_["task_name"], **_layers_of(print_)})
            if found is None:
                _LOGGER.info(
                    "The 3mf of print %s of printer %s is not on its storage either",
                    print_["task_name"],
                    self.printer.serial,
                )
                return
            filename, content = found
            await self._client.async_post_file(
                FILE_MODEL, self._file_fields(print_), filename, "application/octet-stream", content
            )
        except Exception:  # noqa: BLE001 - a failed lookup must not stop the states
            _LOGGER.warning("Could not read the 3mf of the last print of %s", self.printer.serial, exc_info=True)
        finally:
            self._wanted_model = None
            self._fetching_model = False

    @staticmethod
    def _fits(request: dict[str, Any], content: bytes) -> bool:
        """Whether a 3mf prints the plate asked for, at its weight (within 5%)
        and with its number of layers."""
        info = read_model_info(content)
        layers = request.get("total_layers")
        if layers and info.layers is not None and info.layers != layers:
            return False
        plate = request.get("plate")
        if plate is not None and info.plate is not None and info.plate != plate:
            return False
        weight = request.get("weight_grams")
        if weight and info.weight_grams is not None and abs(info.weight_grams - float(weight)) > 0.05 * float(weight):
            return False
        return True

    def _may_import(self, document: dict[str, Any]) -> bool:
        return (
            self._history is not None
            and self._history_since is not None
            and not self._importing
            and not self._history.done(self.printer.serial)
            and document["online"]
            and document["status"] not in NOT_READING_STATUSES
        )

    def _storage_access(self) -> tuple[str, str] | None:
        """The printer's address and LAN access code from ha-bambulab's setup."""
        for entry in self._hass.config_entries.async_entries(BAMBU_DOMAIN):
            if entry.data.get("serial") == self.printer.serial:
                host = entry.options.get("host")
                access_code = entry.options.get("access_code")
                return (host, access_code) if host and access_code else None
        return None

    async def _async_import_history(self) -> None:
        """Send every print on the printer's storage not sent yet, oldest first.

        Stops when the printer starts printing or a print cannot be
        delivered; a later state picks it up again.
        """
        try:
            access = self._storage_access()
            if access is None:
                if not self._reported_no_access:
                    _LOGGER.info(
                        "Not importing the stored prints of printer %s: the Bambu Lab integration has no "
                        "local address and access code for it",
                        self.printer.serial,
                    )
                    self._reported_no_access = True
                return
            storage = await self._hass.async_add_executor_job(PrinterStorage, *access, dt_util.DEFAULT_TIME_ZONE)
            try:
                await self._async_send_stored(storage)
            finally:
                await self._hass.async_add_executor_job(storage.close)
        except Exception:  # noqa: BLE001 - an unreachable printer must not stop the states
            _LOGGER.warning("Could not read the stored prints of printer %s", self.printer.serial, exc_info=True)
        finally:
            self._importing = False

    async def _async_send_stored(self, storage: PrinterStorage) -> None:
        assert self._history is not None
        serial = self.printer.serial
        stored_files = await self._hass.async_add_executor_job(
            storage.stored_files, dt_util.utcnow(), self._history_since
        )
        for stored in stored_files:
            if self._history.sent(serial, stored):
                continue
            if self._build_state()["status"] in NOT_READING_STATUSES:
                return
            info = None
            if stored.is_plate:
                head = await self._hass.async_add_executor_job(storage.read_head, stored.path)
                info = await self._hass.async_add_executor_job(read_gcode_head, head)
            elif stored.size <= MAX_MODEL_BYTES:
                content = await self._hass.async_add_executor_job(storage.read, stored.path)
                info = await self._hass.async_add_executor_job(read_model_info, content)
            fields = {
                "printer_serial": serial,
                "task_name": stored.task_name,
                "file_name": stored.path,
                # Only the day, when that is all the storage gave.
                "modified": _modified(stored),
                "weight_grams": "" if info is None or info.weight_grams is None else str(info.weight_grams),
                "print_seconds": "" if info is None or info.print_seconds is None else str(info.print_seconds),
                "plate": "" if info is None or info.plate is None else str(info.plate),
                "plate_objects": "" if info is None or info.objects is None else str(info.objects),
            }
            if not await self._client.async_post_history(fields, info.thumbnail if info else None):
                return
            self._history.mark_sent(serial, stored)
        self._history.mark_done(serial)
        _LOGGER.info("Sent the %s stored prints of printer %s", len(stored_files), serial)

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
        try:
            image = await async_get_image(self._hass, entity_id)
        except HomeAssistantError:
            _LOGGER.debug("No cover image for printer %s yet", self.printer.serial)
            return
        # Compared by content: ha-bambulab sets the same picture again after
        # a restart, and that must not go out a second time.
        sent = [print_["start_time"], hashlib.sha256(image.content).hexdigest()]
        if sent == self._sent_files.get(self.printer.serial, FILE_COVER_IMAGE):
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
            self._sent_files.set(self.printer.serial, FILE_COVER_IMAGE, sent)

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
        # Not by modification time: ha-bambulab touches a cached file when it
        # reuses it, after a restart too.
        sent = [print_["start_time"], model.path.name, model.size]
        if sent == self._sent_files.get(self.printer.serial, FILE_MODEL):
            return
        if model.size > MAX_MODEL_BYTES:
            _LOGGER.warning("Not sending %s: larger than %s bytes", model.path.name, MAX_MODEL_BYTES)
            self._sent_files.set(self.printer.serial, FILE_MODEL, sent)
            return
        # ha-bambulab can take a print started through Bambu's cloud for the
        # one before it of the same name and reuse that one's cached 3mf. The
        # printer's own layer count tells: a 3mf of other layers is another
        # print's, and the print's own is fetched from the printer once the
        # print has ended. Kept until the layer count changes, as a print can
        # still report the previous print's count when it has just started.
        refused = [*sent, print_.get("total_layers")]
        if refused == self._refused_model:
            return
        content = await self._hass.async_add_executor_job(model.path.read_bytes)
        info = await self._hass.async_add_executor_job(read_model_info, content)
        if print_.get("total_layers") and info.layers is not None and info.layers != print_["total_layers"]:
            _LOGGER.info(
                "Not sending %s for print %s of printer %s: it has %s layers, the print %s",
                model.path.name,
                print_["task_name"],
                self.printer.serial,
                info.layers,
                print_["total_layers"],
            )
            self._refused_model = refused
            self._wanted_model = dict(print_)
            return
        if await self._client.async_post_file(
            FILE_MODEL, self._file_fields(print_), model.path.name, "application/octet-stream", content
        ):
            self._sent_files.set(self.printer.serial, FILE_MODEL, sent)
            self._wanted_model = None


class WebhookManager:
    """Finds every Bambu Lab printer and keeps a sender running for each."""

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        token: str,
        entry_id: str,
        interval: timedelta,
        energy_sensors: dict[str, str] | None = None,
        price: Price | None = None,
        history_since: datetime | None = None,
    ) -> None:
        self._hass = hass
        self._history_since = history_since
        self._interval = interval
        self._energy_sensors = energy_sensors or {}
        self._price = price
        self._client = WebhookClient(hass, url, token)
        self._sent_files = SentFiles(hass, entry_id)
        self._history = HistoryImports(hass, entry_id)
        self._energy_costs = EnergyCosts(hass, entry_id)
        self._senders: dict[str, PrinterSender] = {}
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._rediscovery: CALLBACK_TYPE | None = None

    @property
    def client(self) -> WebhookClient:
        """The client every printer's requests go through."""
        return self._client

    @property
    def senders(self) -> dict[str, PrinterSender]:
        """The running senders by printer serial."""
        return self._senders

    async def async_load(self) -> None:
        """Read back what was kept from before a restart: delivered files, imports and energy costs."""
        await self._sent_files.async_load()
        await self._history.async_load()
        await self._energy_costs.async_load()

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
        for serial, printer in printers.items():
            printer.energy = self._energy_sensors.get(serial)

        for serial in list(self._senders):
            unchanged = serial in printers and printers[serial] == self._senders[serial].printer
            if not unchanged:
                self._senders.pop(serial).stop()

        for serial, printer in printers.items():
            if serial in self._senders:
                continue
            sender = PrinterSender(
                self._hass,
                printer,
                self._client,
                self._sent_files,
                self._interval,
                self._price,
                self._history,
                self._energy_costs,
                self._history_since,
            )
            sender.start(first_send_delay)
            self._senders[serial] = sender

        if not printers:
            _LOGGER.warning("No Bambu Lab printers found; is the Bambu Lab integration set up?")
