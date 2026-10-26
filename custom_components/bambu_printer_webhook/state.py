"""Build the printer state document (docs/schema-v1.md) from entity states.

Kept free of side effects: it only reads the states it is handed, so the
whole document can be tested from a fixed set of states.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PRINT_STATUSES, SCHEMA_VERSION, VERSION
from .discovery import AmsEntities, PrinterEntities

StateGetter = Callable[[str], State | None]

_MISSING = {None, "", STATE_UNKNOWN, STATE_UNAVAILABLE, "none"}
_NO_RFID = {"0" * 32, "0" * 16}


def build_state(printer: PrinterEntities, get_state: StateGetter, now: datetime) -> dict[str, Any]:
    """The full state of one printer, as sent to the webhook."""
    status = _status(printer, get_state)

    return {
        "schema_version": SCHEMA_VERSION,
        "sent_at": dt_util.as_utc(now).isoformat(),
        "source": {"integration": DOMAIN, "version": VERSION},
        "printer": {"serial": printer.serial, "model": printer.model, "name": printer.name},
        "online": _online(printer, get_state, status),
        "status": status,
        "print": _print(printer, get_state),
        "ams": [_ams(unit, get_state) for unit in printer.ams],
        "external_spool": _external_spool(printer, get_state),
    }


def _value(get_state: StateGetter, entity_id: str | None) -> str | None:
    if entity_id is None:
        return None
    state = get_state(entity_id)
    if state is None or state.state in _MISSING:
        return None
    return state.state


def _status(printer: PrinterEntities, get_state: StateGetter) -> str:
    value = (_value(get_state, printer.sensors.get("status")) or "unknown").lower()
    return value if value in PRINT_STATUSES else "unknown"


def _online(printer: PrinterEntities, get_state: StateGetter, status: str) -> bool:
    if printer.online is None:
        return status != "offline"
    return _value(get_state, printer.online) == STATE_ON


def _print(printer: PrinterEntities, get_state: StateGetter) -> dict[str, Any] | None:
    def sensor(field: str) -> str | None:
        return _value(get_state, printer.sensors.get(field))

    document = {
        "task_name": sensor("task_name"),
        "progress_percent": _int(sensor("progress_percent")),
        "current_layer": _int(sensor("current_layer")),
        "total_layers": _int(sensor("total_layers")),
        "start_time": _timestamp(sensor("start_time")),
        "end_time": _timestamp(sensor("end_time")),
        "weight_grams": _number(sensor("weight_grams")),
    }

    if document["task_name"] is None and document["progress_percent"] is None and document["start_time"] is None:
        return None
    return document


def _ams(unit: AmsEntities, get_state: StateGetter) -> dict[str, Any]:
    return {
        "index": unit.index,
        "humidity": _int(_value(get_state, unit.humidity)),
        "humidity_index": _int(_value(get_state, unit.humidity_index)),
        "trays": [_tray(get_state(entity_id), slot=slot) for slot, entity_id in sorted(unit.trays.items())],
    }


def _external_spool(printer: PrinterEntities, get_state: StateGetter) -> dict[str, Any] | None:
    if printer.external_spool is None:
        return None
    state = get_state(printer.external_spool)
    if state is None or state.state == STATE_UNAVAILABLE:
        return None
    return _tray(state, slot=None)


def _tray(state: State | None, slot: int | None) -> dict[str, Any]:
    """One AMS slot or the external spool holder."""
    attributes: Mapping[str, Any] = state.attributes if state is not None else {}
    position = {} if slot is None else {"slot": slot}
    active = bool(attributes.get("active", False))

    empty = attributes.get("empty")
    if empty is None:
        # Older ha-bambulab versions have no "empty" attribute; a slot
        # without a filament name is empty there.
        empty = not attributes.get("name")
    if state is None or state.state == STATE_UNAVAILABLE or empty:
        return {**position, "empty": True, "active": active}

    return {
        **position,
        "empty": False,
        "active": active,
        "tray_uuid": _rfid(attributes.get("tray_uuid")),
        "tag_uid": _rfid(attributes.get("tag_uid")),
        "name": attributes.get("name") or None,
        "type": attributes.get("type") or None,
        "color": _color(attributes.get("color")),
        "tray_weight": _int(attributes.get("tray_weight")),
        "remain": _int(attributes.get("remain"), default=-1),
    }


def _rfid(value: Any) -> str | None:
    """A tray uuid or tag uid, ``None`` for a spool without a Bambu RFID tag."""
    if value is None:
        return None
    text = str(value).strip().upper()
    return None if text in _NO_RFID or text == "" else text


def _color(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if not text.startswith("#") or len(text) not in (7, 9):
        return None
    return text if len(text) == 9 else f"{text}FF"


def _int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _number(value: Any) -> float | int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else round(number, 2)


def _timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return dt_util.as_utc(parsed).isoformat()
