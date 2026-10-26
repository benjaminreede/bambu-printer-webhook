# Printer state, schema version 1

The integration sends the complete state of one Bambu Lab printer as a single
JSON document. It never sends partial updates or events: the receiver keeps the
previous state and works out what changed (a print started, a spool was
swapped) by comparing the two.

## Request

```
POST <webhook URL configured in Home Assistant>
Authorization: Bearer <token configured in Home Assistant>
Content-Type: application/json
Accept: application/json
```

One request per printer. A Home Assistant with two printers sends two
requests.

## When it is sent

- After any change to one of the printer's entities, once nothing has changed
  for **20 seconds**. Inserting a spool makes the printer report a burst of
  intermediate states (the previous spool, then the new one without a
  weight, then its real reading); waiting for the burst to end means only
  the settled state is sent.
- Every **5 minutes** regardless, as a heartbeat, so a missed request or a
  receiver that was down catches up.
- Once shortly after Home Assistant starts.

A receiver must therefore treat every request as idempotent: the same state
can arrive more than once.

## Body

```json
{
  "schema_version": 1,
  "sent_at": "2026-10-26T10:17:45+00:00",
  "source": { "integration": "bambu_printer_webhook", "version": "0.1.0" },
  "printer": {
    "serial": "01P09C510500566",
    "model": "P1S",
    "name": "Garage P1S"
  },
  "online": true,
  "status": "running",
  "print": {
    "task_name": "Christmas Moose Tea Light",
    "progress_percent": 49,
    "current_layer": 269,
    "total_layers": 614,
    "start_time": "2026-10-26T08:17:45+00:00",
    "end_time": "2026-10-26T12:02:00+00:00",
    "weight_grams": 38
  },
  "ams": [
    {
      "index": 1,
      "humidity": 3,
      "trays": [
        {
          "slot": 1,
          "empty": false,
          "active": true,
          "tray_uuid": "02976CE8418148D988489D5D82B1467C",
          "tag_uid": "A288022C00000100",
          "name": "Bambu PLA Matte",
          "type": "PLA",
          "color": "#7D6556FF",
          "tray_weight": 1000,
          "remain": 79
        },
        { "slot": 2, "empty": true, "active": false }
      ]
    }
  ],
  "external_spool": null
}
```

### Fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Always `1` for this document. A receiver should reject versions it doesn't know. |
| `sent_at` | ISO 8601 | When the integration built this state. |
| `source.integration` | string | Always `bambu_printer_webhook`. |
| `source.version` | string | The integration's version, for troubleshooting. |
| `printer.serial` | string | The printer's serial number: its stable identity. |
| `printer.model` | string or null | e.g. `P1S`, `X1C`, `A1`. |
| `printer.name` | string or null | The device name in Home Assistant, as the user sees it. |
| `online` | boolean | Whether Home Assistant currently reaches the printer. When `false`, everything else is the last known state. |
| `status` | string | The print status as the Bambu Lab integration reports it, lowercase: `idle`, `prepare`, `slicing`, `running`, `pause`, `finish`, `failed`, `offline` or `unknown`. |
| `print` | object or null | The current or most recent print, `null` when the printer reports none. |
| `print.task_name` | string or null | The task name as the printer reports it. |
| `print.progress_percent` | integer or null | 0 to 100. |
| `print.current_layer` / `print.total_layers` | integer or null | |
| `print.start_time` | ISO 8601 or null | When the printer says the print started. It can shift once while a print is being prepared, so it is **not** an identity. |
| `print.end_time` | ISO 8601 or null | The printer's estimated (while running) or actual (once finished) end time. |
| `print.weight_grams` | number or null | The print's total filament weight according to the slicer. |
| `ams` | array | One entry per AMS unit, possibly empty. |
| `ams[].index` | integer | 1 for the first AMS, 2 for the second, and so on. |
| `ams[].humidity` | integer or null | The AMS humidity level as reported. |
| `ams[].trays` | array | Always every slot of that AMS (4 for an AMS, 1 for an AMS HT), including empty ones. |
| `trays[].slot` | integer | 1 to 4 within its AMS. |
| `trays[].empty` | boolean | `true` when the slot holds no spool; the spool fields are then left out. |
| `trays[].active` | boolean | Whether the printer is feeding from this slot right now. |
| `trays[].tray_uuid` | string or null | The spool's identity, the same from both of its RFID tags. `null` for a spool without a Bambu RFID tag. |
| `trays[].tag_uid` | string or null | The RFID tag that was read. A spool has a tag on each side, so this can differ between two readings of the same spool. |
| `trays[].name` | string or null | Filament name, e.g. `Bambu PLA Matte`. |
| `trays[].type` | string or null | Material, e.g. `PLA`. |
| `trays[].color` | string or null | `#RRGGBBAA`. |
| `trays[].tray_weight` | integer or null | Full spool weight in grams. |
| `trays[].remain` | integer | Remaining filament in percent, `-1` when the printer has not measured it (yet). |
| `external_spool` | tray or null | The external spool holder, in the same shape as a tray (without `slot`), or `null` when the printer has none. |

## Response

Any `2xx` counts as delivered. On anything else, or when the receiver is
unreachable, the integration logs the failure and does not retry on its own:
the next change or heartbeat sends the state again anyway.
