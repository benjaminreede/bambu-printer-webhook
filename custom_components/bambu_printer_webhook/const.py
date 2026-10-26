"""Constants for Bambu Printer Webhook."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "bambu_printer_webhook"
NAME: Final = "Bambu Printer Webhook"
VERSION: Final = "0.1.0"

SCHEMA_VERSION: Final = 1

BAMBU_DOMAIN: Final = "bambu_lab"
"""The domain of the ha-bambulab integration whose entities are read."""

CONF_WEBHOOK_URL: Final = "webhook_url"
CONF_TOKEN: Final = "token"

SETTLE_DELAY: Final = timedelta(seconds=30)
"""How long a printer's entities must be quiet before its state is sent.

Inserting a spool was seen to leave up to 23 seconds between the new spool
reported unmeasured and its real reading; 30 seconds clears that.
"""

HEARTBEAT_INTERVAL: Final = timedelta(minutes=5)
"""How often a printer's state is sent regardless of changes."""

STARTUP_DELAY: Final = timedelta(seconds=30)
"""How long after setup the first state is sent, so ha-bambulab has connected."""

REDISCOVERY_DELAY: Final = timedelta(seconds=10)
"""How long registry changes must be quiet before printers are looked up again."""

REQUEST_TIMEOUT_SECONDS: Final = 15

FILE_REQUEST_TIMEOUT_SECONDS: Final = 120
"""A 3mf can be tens of megabytes."""

MAX_MODEL_BYTES: Final = 100 * 1024 * 1024
"""Larger 3mf files are not sent."""

FILE_STATUSES: Final = frozenset({"running", "pause"})
"""Statuses in which the print's cover image and 3mf are sent."""

FILE_COVER_IMAGE: Final = "cover_image"
FILE_MODEL: Final = "model"

PRINT_STATUSES: Final = frozenset(
    {"idle", "init", "prepare", "slicing", "running", "pause", "finish", "failed", "offline", "unknown"}
)

PRINTER_SENSOR_KEYS: Final = {
    "status": "print_status",
    "task_name": "subtask_name",
    "progress_percent": "print_progress",
    "current_layer": "current_layer",
    "total_layers": "total_layers",
    "start_time": "start_time",
    "end_time": "end_time",
    "weight_grams": "print_weight",
}
"""Schema field to ha-bambulab entity key, found by unique id ``{serial}_{key}``."""

ONLINE_KEY: Final = "online"

COVER_IMAGE_KEY: Final = "cover_image"
"""The image entity with the current print's cover, unique id ``{serial}_cover_image``."""
