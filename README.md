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

- after a change, once the printer's entities have been quiet for 30 seconds
  (inserting a spool produces a burst of intermediate readings; only the
  settled state goes out);
- every 5 minutes regardless, as a heartbeat;
- once, 30 seconds after Home Assistant starts.

While a print runs, its cover image and 3mf file follow to the same URL as a
multipart form, once per print. They come from the Bambu Lab integration's own
cover image and file cache; this integration never connects to the printer.

Each request is a `POST` with the token as a bearer token in the
`Authorization` header. A failed request is logged and not retried: the next
change or heartbeat sends the state again.

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

Both can be changed later through **Reconfigure** on the integration. Adding the
integration more than once sends every printer to more than one webhook.

## Development

```
pip install -r requirements_test.txt
pytest
```

## Releasing

Set the new version in `custom_components/bambu_printer_webhook/manifest.json`
and in `VERSION` in `const.py`, merge that into `main`, then push a tag for it:

```
git tag v0.2.0 && git push origin v0.2.0
```

The Release workflow checks that the tag matches both versions and publishes
a GitHub release, which HACS offers as an update.
