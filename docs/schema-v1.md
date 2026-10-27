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
requests. The print's files follow as separate requests, see
[Print files](#print-files).

## When it is sent

- After a significant change, once nothing has changed for **30 seconds**:
  the printer's status, whether it is online, the task name or start time of
  its print, or a slot's spool (inserted, taken out, or the one being printed
  from). Inserting a spool makes the printer report a burst of
  intermediate states (the previous spool, then the new one without a
  weight, then its real reading, up to 23 seconds later); waiting for the
  burst to end means only the settled state is sent. A receiver should
  still not trust a spool's percentage in the first state it shows up in a
  slot, unless that slot is `settled` (below).
- After **3 seconds** when an AMS has finished reading a spool. While no
  print is underway, the AMS makes a slot active to read a newly inserted
  spool's RFID tag, and lets go once it has read the tag and the remaining
  percentage; that slot is then sent with `settled: true`. The AMS reads no
  tags during a print: a spool inserted then stays unread (all zeros) until
  the AMS reads it later.
- After a spool runs out (stage `paused_filament_runout`) or a print error
  appears or clears, once nothing has changed for 30 seconds.
- Every **5 minutes** regardless (configurable from 60 to 3600 seconds), as a
  heartbeat. Everything else (progress, layers, end time, a spool's
  percentage, humidity) only reaches the receiver this way, and a missed
  request or a receiver that was down catches up.
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
  "stage": "printing",
  "print_error": null,
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
      "humidity": null,
      "humidity_index": 2,
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
          "remain": 79,
          "settled": false
        },
        { "slot": 2, "empty": true, "active": false, "settled": false }
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
| `status` | string | The print status as the Bambu Lab integration reports it, lowercase: `idle`, `init`, `prepare`, `slicing`, `running`, `pause`, `finish`, `failed`, `offline` or `unknown`. Anything else the integration does not know is sent as `unknown`. |
| `stage` | string or null | What the printer is doing in more detail, as the Bambu Lab integration names it: `printing`, `changing_filament`, `paused_filament_runout` and so on. `null` when unknown. Added in 0.4.0. |
| `print_error` | object or null | The error the printer reports for its print, `null` without one. Added in 0.4.0. |
| `print_error.code` | string or null | Bambu's error code, e.g. `0700_8011`. A run-out spool in an AMS is `XXYY_8011`, with `XX` the kind of AMS (`07` AMS, `12` AMS Lite, `18` AMS HT) and `YY` its number counted from 0 (`FE`/`FF` for an external spool); the slot is not in the code, but it is the one that was active. |
| `print_error.text` | string or null | The error's description in Home Assistant's language. |
| `print` | object or null | The current or most recent print, `null` when the printer reports no task name, progress or start time. |
| `print.task_name` | string or null | The task name as the printer reports it. |
| `print.progress_percent` | integer or null | 0 to 100. |
| `print.current_layer` / `print.total_layers` | integer or null | |
| `print.start_time` | ISO 8601 or null | When the printer says the print started. It can shift once while a print is being prepared, so it is **not** an identity. |
| `print.end_time` | ISO 8601 or null | The printer's estimated (while running) or actual (once finished) end time. |
| `print.weight_grams` | number or null | The print's total filament weight according to the slicer. |
| `ams` | array | One entry per AMS unit, possibly empty. |
| `ams[].index` | integer | The AMS number as the printer gives it: 1 for the first AMS, 2 for the second, and so on; an AMS HT counts from 128. |
| `ams[].humidity` | integer or null | Relative humidity in percent, on AMS units that measure it. |
| `ams[].humidity_index` | integer or null | Humidity level from 1 (dry) to 5 (wet), as Bambu Handy and Studio show it. |
| `ams[].trays` | array | Always every slot of that AMS (4 for an AMS, 1 for an AMS HT), including empty ones. |
| `trays[].slot` | integer | 1 to 4 within its AMS. |
| `trays[].empty` | boolean | `true` when the slot holds no spool, or when Home Assistant cannot currently read the slot; the spool fields are then left out. |
| `trays[].active` | boolean | Whether the printer is feeding from this slot right now. |
| `trays[].tray_uuid` | string or null | The spool's identity, the same from both of its RFID tags. `null` for a spool without a Bambu RFID tag (the printer reports all zeros there). |
| `trays[].tag_uid` | string or null | The RFID tag that was read. A spool has a tag on each side, so this can differ between two readings of the same spool. `null` without a Bambu RFID tag. |
| `trays[].name` | string or null | Filament name, e.g. `Bambu PLA Matte`. |
| `trays[].type` | string or null | Material, e.g. `PLA`. |
| `trays[].color` | string or null | `#RRGGBBAA`, uppercase; a 6-digit colour is sent with `FF` added. |
| `trays[].tray_weight` | integer or null | Full spool weight in grams. |
| `trays[].remain` | integer | Remaining filament in percent, `-1` when the printer has not measured it (yet). |
| `trays[].settled` | boolean | `true` when the integration saw the AMS read this spool in this slot (see When it is sent): its identity and percentage are final. Always `false` for the external spool, and `false` after Home Assistant restarts until the AMS reads the slot again. Added in 0.4.0. |
| `external_spool` | tray or null | The external spool holder, in the same shape as a tray (without `slot`), or `null` when the printer has none. A printer with two holders (one per nozzle) reports the first. |

## Response

Any `2xx` counts as delivered. On anything else, or when the receiver is
unreachable, the integration logs the failure and does not retry on its own:
the next change or heartbeat sends the state again anyway.

## Ping

When the webhook is set up or changed in Home Assistant, the integration first
sends a ping to check the address and token:

```json
{ "schema_version": 1, "ping": true }
```

A receiver authenticates it like any request and answers `2xx` without
storing anything. The answer decides what Home Assistant shows: `401` or `403`
means the token is refused, `404` or `405` that the address doesn't take
printer states, anything else that is not `2xx` an unexpected answer. The
webhook is only saved once the ping is answered with `2xx`.

## Print files

While a print is `running` or `pause`, the integration also sends the print's
cover image and 3mf file to the same URL, each as its own request, right after
a state that was delivered. A receiver therefore always has the state showing
the print running before a file for it arrives. Each file is sent once per
print; the cover again when it changes (a printer can correct it once the real
task name arrives). A file the receiver refused, or that could not be
delivered, goes out again after the next delivered state.

```
POST <webhook URL configured in Home Assistant>
Authorization: Bearer <token configured in Home Assistant>
Content-Type: multipart/form-data
Accept: application/json
```

| Field | Meaning |
|---|---|
| `schema_version` | Always `1`. |
| `kind` | `cover_image` or `model`. |
| `printer_serial` | The printer's serial number, as `printer.serial` in the state. |
| `task_name` | The print's task name, as `print.task_name`. |
| `start_time` | The print's start time, exactly as `print.start_time`: match on it with some margin, since it can shift once while a print is being prepared. |
| `file` | The file. A cover image is the plate's PNG preview (`cover.png`), or a JPEG from Bambu's cloud (`cover.jpg`). A 3mf keeps the name it has in the Bambu Lab integration's cache. |

Both come from the Bambu Lab integration, which downloads the 3mf of every
print from the printer to read it; this integration never connects to the
printer itself. The cover is its cover image entity. The 3mf is the newest file
in its cache named after the task and cached at most 10 minutes before the
print started; when there is none (no local access to the printer, or a file
whose name doesn't match the task) no 3mf is sent. Files over 100 MB are not
sent.

A receiver tells the two kinds of request apart by their content type: JSON
is a state, a multipart form is a file.
