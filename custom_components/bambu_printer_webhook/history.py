"""Read the prints a printer still has on its storage, for a one-time import.

A printer keeps every file sent to it on its SD card or USB stick. Each 3mf
there stands for a print: its name, when it was sent, and from the file
itself the estimated weight and print time and a picture of the plate.

The storage is read over implicit FTPS (port 990) with the printer's LAN
access code, the way Bambu Studio and ha-bambulab do; both come from
ha-bambulab's own setup of the printer. Nothing on the printer is changed.
Everything here blocks: run it in the executor.
"""

from __future__ import annotations

import ftplib
import io
import re
import ssl
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.etree import ElementTree

FTP_PORT = 990
FTP_USER = "bblp"
FTP_TIMEOUT_SECONDS = 30

SKIPPED_FOLDERS = frozenset({"timelapse", "ipcam", "logger", "recorder", "verify_job"})
"""Folders the printer fills itself, without prints sent to it."""

_EXTENSION = re.compile(r"(\.gcode)?\.3mf$", re.IGNORECASE)
_PLATE_GCODE = re.compile(r"^Metadata/plate_(\d+)\.gcode$")
_MONTHS = {
    name: number
    for number, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}
_LIST_LINE = re.compile(
    r"^(?P<kind>[\-dl])[rwxst\-]{9}\s+\d+\s+\S+\s+\S+\s+(?P<size>\d+)\s+"
    r"(?P<month>\w{3})\s+(?P<day>\d{1,2})\s+(?P<timeyear>\d{1,2}:\d{2}|\d{4})\s+(?P<name>.+)$"
)


@dataclass(frozen=True)
class StoredFile:
    """A 3mf on the printer's storage."""

    path: str
    size: int
    modified: datetime | None

    @property
    def task_name(self) -> str:
        """The print's name: the file's, without folder and extension."""
        return _EXTENSION.sub("", self.path.rsplit("/", 1)[-1]).strip()

    @property
    def key(self) -> list[str | int]:
        """What identifies this file across imports."""
        return [self.path, self.size, self.modified.isoformat() if self.modified else ""]


@dataclass(frozen=True)
class ModelInfo:
    """What a sliced 3mf says about the plate it prints."""

    weight_grams: float | None
    print_seconds: int | None
    thumbnail: bytes | None


class _ImplicitFTPS(ftplib.FTP_TLS):
    """FTPS that is TLS from the first byte, as Bambu printers use."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value


class PrinterStorage:
    """One connection to a printer's storage."""

    def __init__(self, host: str, access_code: str) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # the printer's certificate is self-signed
        self._ftp = _ImplicitFTPS(context=context)
        self._ftp.connect(host=host, port=FTP_PORT, timeout=FTP_TIMEOUT_SECONDS)
        self._ftp.login(user=FTP_USER, passwd=access_code)
        self._ftp.prot_p()

    def close(self) -> None:
        try:
            self._ftp.quit()
        except ftplib.all_errors:
            self._ftp.close()

    def stored_files(self, now: datetime) -> list[StoredFile]:
        """Every 3mf in the storage's root and the folders directly under it, oldest first."""
        found: list[StoredFile] = []
        folders: list[str] = []
        for name, is_folder, size, modified in self._entries("/", now):
            if is_folder:
                if name.casefold() not in SKIPPED_FOLDERS:
                    folders.append(name)
            elif _EXTENSION.search(name):
                found.append(StoredFile(f"/{name}", size, modified))
        for folder in folders:
            try:
                entries = self._entries(f"/{folder}", now)
            except ftplib.all_errors:
                continue
            for name, is_folder, size, modified in entries:
                if not is_folder and _EXTENSION.search(name):
                    found.append(StoredFile(f"/{folder}/{name}", size, modified))
        oldest = datetime.min.replace(tzinfo=UTC)
        return sorted(found, key=lambda stored: (stored.modified or oldest, stored.path))

    def read(self, path: str) -> bytes:
        buffer = io.BytesIO()
        self._ftp.retrbinary(f"RETR {path}", buffer.write)
        return buffer.getvalue()

    def _entries(self, path: str, now: datetime) -> list[tuple[str, bool, int, datetime | None]]:
        """(name, is folder, size, modified) per entry: MLSD for exact times, else LIST."""
        try:
            entries = []
            for name, facts in self._ftp.mlsd(path, facts=["type", "size", "modify"]):
                if name in (".", ".."):
                    continue
                entries.append(
                    (
                        name,
                        facts.get("type") == "dir",
                        int(facts.get("size") or 0),
                        parse_mlsd_time(facts.get("modify")),
                    )
                )
            return entries
        except ftplib.error_perm:
            lines: list[str] = []
            self._ftp.retrlines(f"LIST {path}", lines.append)
            return [entry for line in lines if (entry := parse_list_line(line, now)) is not None]


def parse_mlsd_time(value: str | None) -> datetime | None:
    """An MLSD ``modify`` fact (``20261012143015``), which is in UTC."""
    if not value or len(value) < 14:
        return None
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_list_line(line: str, now: datetime) -> tuple[str, bool, int, datetime | None] | None:
    """One ``ls -l`` style LIST line. A recent file shows its time, an older one
    only its year: such a file gets midnight of its day."""
    match = _LIST_LINE.match(line.strip())
    if match is None:
        return None
    month = _MONTHS.get(match["month"].casefold())
    if month is None:
        return None
    day = int(match["day"])
    try:
        if ":" in match["timeyear"]:
            hour, minute = (int(part) for part in match["timeyear"].split(":"))
            modified = datetime(now.year, month, day, hour, minute, tzinfo=UTC)
            if modified > now:
                modified = modified.replace(year=now.year - 1)
        else:
            modified = datetime(int(match["timeyear"]), month, day, tzinfo=UTC)
    except ValueError:
        modified = None
    return match["name"], match["kind"] == "d", int(match["size"]), modified


def read_model_info(content: bytes) -> ModelInfo:
    """The plate this 3mf prints: its estimated weight and time, and its picture.

    A sliced 3mf holds the G-code of the plate it prints; its estimates are in
    Metadata/slice_info.config. Anything unreadable is left out.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return ModelInfo(None, None, None)

    with archive:
        names = set(archive.namelist())
        sliced = sorted(int(match[1]) for name in names if (match := _PLATE_GCODE.match(name)))
        plate = sliced[0] if sliced else 1

        weight: float | None = None
        seconds: int | None = None
        if "Metadata/slice_info.config" in names:
            try:
                root = ElementTree.fromstring(archive.read("Metadata/slice_info.config"))
            except ElementTree.ParseError:
                root = None
            for element in root.iter("plate") if root is not None else []:
                metadata = {item.get("key"): item.get("value") for item in element.iter("metadata")}
                if str(metadata.get("index")) != str(plate):
                    continue
                weight = _float(metadata.get("weight"))
                prediction = _float(metadata.get("prediction"))
                seconds = int(prediction) if prediction is not None else None

        picture = f"Metadata/plate_{plate}.png"
        thumbnail = archive.read(picture) if picture in names else None

    return ModelInfo(weight, seconds, thumbnail)


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None
