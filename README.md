# Bambu Printer Webhook

A Home Assistant integration that sends the state of your Bambu Lab printers to
a webhook of your choice: print status and progress, what is loaded in each
AMS slot, and each print's cover image and 3mf file. Use it to keep a filament inventory, a print log or a dashboard of
your own up to date, without writing automations.

It reads the entities of the [Bambu Lab integration](https://github.com/greghesp/ha-bambulab)
(`ha-bambulab`), so that has to be set up first. Every printer it knows about is
picked up, including extra AMS units and printers added later.

## What it sends

One JSON document per printer with its complete state, described in
[docs/schema-v1.md](docs/schema-v1.md). It is sent:

- after a significant change (the printer's status, going on- or offline, a
  new print, a spool inserted or taken out), once the printer's entities have
  been quiet for 30 seconds (inserting a spool produces a burst of
  intermediate readings; only the settled state goes out);
- every 5 minutes regardless, with whatever else changed (progress, layers,
  a spool's percentage). The interval can be changed through **Configure** on
  the integration, from 60 to 3600 seconds;
- once, 30 seconds after Home Assistant starts.

While a print runs, its cover image and 3mf file follow to the same URL as a
multipart form, once per print. They come from the Bambu Lab integration's own
cover image and file cache.

Once per printer, the prints still on its storage are sent too, so the
receiver can add the prints from before the integration was set up. For that
the integration reads the printer's storage over its local FTPS, with the
address and access code from the Bambu Lab integration, and only while it
isn't printing. Nothing on the printer is changed.

When a printer's plug has an energy meter, choose it per printer under
**Configure**, with the electricity price (a sensor with your tariff, or a
fixed price): the state then carries the meter's reading and what the plug has
cost so far, each kWh paid at the price of the moment it was used, so the
receiver can work out what a print cost in electricity, with a dynamic tariff
too.

Each request is a `POST` with the token as a bearer token in the
`Authorization` header. A failed request is logged and not retried: the next
change or heartbeat sends the state again.

## Entities

The integration adds a device **Bambu Printer Webhook** with two diagnostic
entities, so you can see in Home Assistant whether sending works (and automate
on it):

- **Last delivered**: when the webhook last accepted a state or file;
- **Delivery**: a problem sensor, on when the last request failed, with the
  reason (`HTTP 401`, a connection error) in its `last_error` attribute.

They can't tell when Home Assistant itself is down: only the receiver can
notice states stop arriving.

## Installation

Through [HACS](https://hacs.xyz/):

1. HACS, menu in the top right, **Custom repositories**.
2. Add `https://github.com/benjaminreede/bambu-printer-webhook` with type
   **Integration**.
3. Install **Bambu Printer Webhook** and restart Home Assistant.

Or by hand: copy `custom_components/bambu_printer_webhook` into the
`custom_components` folder of your Home Assistant configuration and restart.

## Configuration

**Settings, Devices & services, Add integration, Bambu Printer Webhook**, then
enter:

- **Webhook URL**: the full `http://` or `https://` address that receives the
  state;
- **Token**: whatever your receiver expects as bearer token.

A test message is sent first, so a wrong address or token shows up right
there instead of in the log. Both can be changed later through **Reconfigure**
on the integration. Adding the
integration more than once sends every printer to more than one webhook.

## Development

```
pip install -r requirements_test.txt
pytest
```

## Releasing

Set the new version in `custom_components/bambu_printer_webhook/manifest.json`
and in `VERSION` in `const.py`, and merge that into `main`. The Release
workflow then publishes a GitHub release for it (`v` plus the version), which
HACS offers as an update. It can also be started by hand from the Actions tab,
to release the current version when that hasn't happened yet.
