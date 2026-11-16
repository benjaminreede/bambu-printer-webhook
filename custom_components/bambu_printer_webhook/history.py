"""Read the prints a printer still has on its storage, for a one-time import.

A printer keeps the files sent to it on its SD card or USB stick, in
``cache``: per project the 3mf last sent (holding the one plate it was sent
for), and per plate it printed the G-code, named after the project with
``_plate_N``. Sending a project again overwrites both. So each 3mf stands
for a print, and so does each G-code of another plate of the same project:
its name (the project's), when it was sent, and from the file itself the
estimated weight and print time and a picture of the plate.

The storage is read over implicit FTPS (port 990) with the printer's LAN
access code, the way Bambu Studio and ha-bambulab do; both come from
ha-bambulab's own setup of the printer. Nothing on the printer is changed.
Everything here blocks: run it in the executor.
"""

from __future__ import annotations

import base64
import ftplib
import io
import re
import ssl
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from xml.etree import ElementTree

FTP_PORT = 990
FTP_USER = "bblp"
FTP_TIMEOUT_SECONDS = 30

SKIPPED_FOLDERS = frozenset({"timelapse", "ipcam", "logger", "recorder", "verify_job"})
"""Folders the printer fills itself, without prints sent to it."""

HEAD_BYTES = 512 * 1024
"""How much of a G-code holds its header and pictures: never the whole file,
which can be well over 100 MB."""

OWN_PLATE_WINDOW = timedelta(hours=1)
"""How soon after its 3mf the G-code of the plate in it was written."""

_EXTENSION = re.compile(r"(\.gcode)?\.3mf$", re.IGNORECASE)
_PLATE_FILE = re.compile(r"^(?P<project>.+)_plate_(?P<plate>\d+)\.gcode$", re.IGNORECASE)
_HEADER_WEIGHT = re.compile(r"^; total filament weight \[g\] : (?P<grams>[\d.,]+)", re.MULTILINE)
_HEADER_TIME = re.compile(
    r"^; (?:model printing time: [^;\n]*; )?total estimated time: (?P<time>[^;\n]+)", re.MULTILINE
)
_DURATION_PART = re.compile(r"(\d+)\s*([dhms])")
_THUMBNAIL = re.compile(
    r"^; thumbnail(?:_png)? begin (?P<width>\d+)x(?P<height>\d+) \d+\n"
    r"(?P<data>(?:; [^\n]*\n)+?); thumbnail(?:_png)? end",
    re.MULTILINE,
)
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
    exact_time: bool = True
    """False when the storage only gave the day the file was written."""
    project: str | None = None
    """For the G-code of a plate: the name of the 3mf it was printed from."""

    @property
    def task_name(self) -> str:
        """The print's name: its project's, else the file's without folder and extension."""
        return self.project or _EXTENSION.sub("", self.path.rsplit("/", 1)[-1]).strip()

    @property
    def is_plate(self) -> bool:
        """Whether this is the G-code of a plate rather than a 3mf."""
        return self.project is not None

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
    """One connection to a printer's storage.

    ``timezone`` is the one the printer keeps its file times in when it only
    lists them the way ``ls -l`` does: its own local time, taken to be Home
    Assistant's.
    """

    def __init__(self, host: str, access_code: str, timezone: tzinfo = UTC) -> None:
        self._host = host
        self._access_code = access_code
        self._timezone = timezone
        self._ftp = self._connect()

    def _connect(self) -> _ImplicitFTPS:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # the printer's certificate is self-signed
        ftp = _ImplicitFTPS(context=context)
        ftp.connect(host=self._host, port=FTP_PORT, timeout=FTP_TIMEOUT_SECONDS)
        ftp.login(user=FTP_USER, passwd=self._access_code)
        ftp.prot_p()
        return ftp

    def close(self) -> None:
        try:
            self._ftp.quit()
        except ftplib.all_errors:
            self._ftp.close()

    def stored_files(self, now: datetime, since: datetime | None = None) -> list[StoredFile]:
        """Every print in the storage's root and the folders directly under it,
        oldest first: each 3mf, and each G-code of another plate of a project
        with a 3mf next to it. Only those written since ``since``, when given."""
        found: list[StoredFile] = []
        folders: list[str] = []
        root = self._entries("/", now)
        found += prints_in("/", root)
        folders = [name for name, is_folder, _, _, _ in root if is_folder and name.casefold() not in SKIPPED_FOLDERS]
        for folder in folders:
            try:
                entries = self._entries(f"/{folder}", now)
            except ftplib.all_errors:
                continue
            found += prints_in(f"/{folder}/", entries)
        if since is not None:
            found = [stored for stored in found if stored.modified is not None and stored.modified >= since]
        oldest = datetime.min.replace(tzinfo=UTC)
        return sorted(found, key=lambda stored: (stored.modified or oldest, stored.path))

    def read(self, path: str) -> bytes:
        buffer = io.BytesIO()
        self._ftp.retrbinary(f"RETR {path}", buffer.write)
        return buffer.getvalue()

    def read_head(self, path: str, limit: int = HEAD_BYTES) -> bytes:
        """The start of a file, without fetching the rest.

        Breaking a download off leaves the printer's answer to it unclear, so
        the connection is made again afterwards.
        """
        received = bytearray()
        connection = self._ftp.transfercmd(f"RETR {path}")
        try:
            while len(received) < limit:
                data = connection.recv(min(64 * 1024, limit - len(received)))
                if not data:
                    break
                received += data
        finally:
            connection.close()
        self.close()
        self._ftp = self._connect()
        return bytes(received)

    def _entries(self, path: str, now: datetime) -> list[tuple[str, bool, int, datetime | None, bool]]:
        """(name, is folder, size, modified, exact time) per entry: MLSD for exact times, else LIST."""
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
                        True,
                    )
                )
            return entries
        except ftplib.error_perm:
            lines: list[str] = []
            self._ftp.retrlines(f"LIST {path}", lines.append)
            return [entry for line in lines if (entry := parse_list_line(line, now, self._timezone)) is not None]


def prints_in(folder: str, entries: list[tuple[str, bool, int, datetime | None, bool]]) -> list[StoredFile]:
    """The prints among one folder's files: each 3mf, and each G-code of a
    plate of a project whose 3mf is there, except the one of the plate that
    3mf holds (written within the hour after it)."""
    files = [(name, size, modified, exact) for name, is_folder, size, modified, exact in entries if not is_folder]
    models = [
        StoredFile(f"{folder}{name}", size, modified, exact)
        for name, size, modified, exact in files
        if _EXTENSION.search(name)
    ]
    by_project = {model.task_name.casefold(): model for model in models}

    plates: list[StoredFile] = []
    for name, size, modified, exact in files:
        match = _PLATE_FILE.match(name)
        model = by_project.get(match["project"].strip().casefold()) if match else None
        if model is None:
            continue
        if (
            modified is not None
            and model.modified is not None
            and model.modified <= modified <= model.modified + OWN_PLATE_WINDOW
        ):
            continue
        plates.append(StoredFile(f"{folder}{name}", size, modified, exact, project=model.task_name))
    return models + plates


def parse_mlsd_time(value: str | None) -> datetime | None:
    """An MLSD ``modify`` fact (``20261012143015``), which is in UTC."""
    if not value or len(value) < 14:
        return None
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_list_line(
    line: str, now: datetime, timezone: tzinfo = UTC
) -> tuple[str, bool, int, datetime | None, bool] | None:
    """One ``ls -l`` style LIST line, its time in ``timezone``. A recent file
    shows its time, an older one only its year: such a file gets midnight of
    its day, and is marked as not exact."""
    match = _LIST_LINE.match(line.strip())
    if match is None:
        return None
    month = _MONTHS.get(match["month"].casefold())
    if month is None:
        return None
    day = int(match["day"])
    exact = ":" in match["timeyear"]
    try:
        if exact:
            hour, minute = (int(part) for part in match["timeyear"].split(":"))
            modified = datetime(now.year, month, day, hour, minute, tzinfo=timezone)
            if modified > now:
                modified = modified.replace(year=now.year - 1)
        else:
            modified = datetime(int(match["timeyear"]), month, day, tzinfo=timezone)
    except ValueError:
        modified = None
    return match["name"], match["kind"] == "d", int(match["size"]), modified, exact


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


def read_gcode_head(content: bytes) -> ModelInfo:
    """What the header at the start of a plate's G-code says: the estimated
    weight and time, and the largest picture of the plate in it."""
    text = content.decode("utf-8", errors="replace")

    weight_match = _HEADER_WEIGHT.search(text)
    # One figure per filament, comma separated, when the plate used several.
    weight = (
        round(sum(part for value in weight_match["grams"].split(",") if (part := _float(value)) is not None), 2)
        if weight_match
        else None
    )

    time_match = _HEADER_TIME.search(text)
    seconds = parse_duration(time_match["time"]) if time_match else None

    thumbnail: bytes | None = None
    largest = 0
    for match in _THUMBNAIL.finditer(text):
        area = int(match["width"]) * int(match["height"])
        if area <= largest:
            continue
        data = "".join(line[2:] for line in match["data"].splitlines())
        try:
            picture = base64.b64decode(data, validate=False)
        except ValueError:
            continue
        if picture.startswith(b"\x89PNG"):
            thumbnail, largest = picture, area

    return ModelInfo(weight, seconds, thumbnail)


def parse_duration(value: str) -> int | None:
    """A slicer's duration such as ``1d 2h 3m 4s`` in seconds."""
    factors = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    parts = _DURATION_PART.findall(value)
    return sum(int(amount) * factors[unit] for amount, unit in parts) if parts else None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None
