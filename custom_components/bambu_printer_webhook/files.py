"""Find the 3mf file of the print a printer is running.

ha-bambulab downloads the 3mf of every print from the printer (over FTP) to
take its cover image and filament weights from it, and keeps it in its file
cache: ``<config>/www/media/ha-bambulab/<serial>/prints/``, or the same path
under the local media folder when ``www`` is not writable. The file is named
after the print, prefixed with its size (``12958658-Benchy.gcode.3mf``), and
touched when an existing copy is reused, so its modification time tells
whether it belongs to the current print.

This module only reads that cache; it never connects to the printer itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

CACHE_FOLDER = "ha-bambulab"

MODEL_FRESHNESS = timedelta(minutes=10)
"""How long before the print's start time its 3mf may have been cached."""

_SIZE_PREFIX = re.compile(r"^\d+-")
_EXTENSION = re.compile(r"(\.gcode)?\.3mf$", re.IGNORECASE)


@dataclass(frozen=True)
class ModelFile:
    """A 3mf in ha-bambulab's cache."""

    path: Path
    size: int
    modified: datetime


def cache_directories(config_path: Path, local_media: str | None, serial: str) -> list[Path]:
    """Where ha-bambulab may keep the files of this printer."""
    directories = [config_path / "www" / "media" / CACHE_FOLDER / serial]
    if local_media:
        directories.append(Path(local_media) / CACHE_FOLDER / serial)
    return directories


def task_key(task_name: str) -> str:
    """A task name reduced to what its cached file's name has in common with it."""
    return _EXTENSION.sub("", task_name.strip()).strip().casefold()


def file_key(filename: str) -> str:
    """A cached file's name without the size ha-bambulab puts in front of it."""
    return task_key(_SIZE_PREFIX.sub("", filename, count=1))


def find_model_file(directories: list[Path], task_name: str, started: datetime) -> ModelFile | None:
    """The newest cached 3mf named after ``task_name`` and cached for this print.

    Only a file whose name matches the print is taken: ha-bambulab falls back
    to the newest 3mf on the printer when it finds none by name, which can
    belong to an earlier print. Blocking: run it in the executor.
    """
    wanted = task_key(task_name)
    not_before = started - MODEL_FRESHNESS
    newest: ModelFile | None = None

    for directory in directories:
        prints = directory / "prints"
        if not prints.is_dir():
            continue
        for path in prints.rglob("*.3mf"):
            if file_key(path.name) != wanted:
                continue
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, tz=started.tzinfo)
            if modified < not_before:
                continue
            if newest is None or modified > newest.modified:
                newest = ModelFile(path, stat.st_size, modified)

    return newest
