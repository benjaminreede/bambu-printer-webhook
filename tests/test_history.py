"""Reading a printer's storage listing and the 3mf files on it."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime

from custom_components.bambu_printer_webhook.history import (
    StoredFile,
    parse_list_line,
    parse_mlsd_time,
    read_model_info,
)

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _sliced_3mf(plate: int) -> bytes:
    """A 3mf that prints ``plate`` of a project with two plates."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "Metadata/slice_info.config",
            "<config>"
            '<plate><metadata key="index" value="1"/><metadata key="prediction" value="600"/>'
            '<metadata key="weight" value="3.10"/></plate>'
            '<plate><metadata key="index" value="2"/><metadata key="prediction" value="5400"/>'
            '<metadata key="weight" value="42.75"/></plate>'
            "</config>",
        )
        archive.writestr(f"Metadata/plate_{plate}.gcode", "G28")
        archive.writestr("Metadata/plate_1.png", b"plate-1")
        archive.writestr("Metadata/plate_2.png", b"plate-2")
    return buffer.getvalue()


def test_the_plate_a_3mf_prints_gives_its_weight_time_and_picture() -> None:
    info = read_model_info(_sliced_3mf(plate=2))

    assert info.weight_grams == 42.75
    assert info.print_seconds == 5400
    assert info.thumbnail == b"plate-2"


def test_a_file_that_is_no_3mf_gives_nothing() -> None:
    info = read_model_info(b"not a zip")

    assert (info.weight_grams, info.print_seconds, info.thumbnail) == (None, None, None)


def test_a_recent_list_line_has_its_time_and_an_older_one_its_day() -> None:
    recent = parse_list_line("-rw-rw-rw-   1 root  root  12958658 Nov 14 09:31 Benchy.gcode.3mf", NOW)
    older = parse_list_line("-rw-rw-rw-   1 root  root  1048576 Mar  2  2025 Old box.gcode.3mf", NOW)
    folder = parse_list_line("drwxrwxrwx   1 root  root  0 Nov 14 09:31 cache", NOW)

    assert recent == ("Benchy.gcode.3mf", False, 12958658, datetime(2026, 11, 14, 9, 31, tzinfo=UTC))
    assert older == ("Old box.gcode.3mf", False, 1048576, datetime(2025, 3, 2, tzinfo=UTC))
    assert folder is not None and folder[1] is True


def test_a_list_time_later_than_now_is_from_last_year() -> None:
    entry = parse_list_line("-rw-rw-rw-   1 root  root  10 Dec 24 18:00 Kerstbal.3mf", NOW)

    assert entry is not None and entry[3] == datetime(2025, 12, 24, 18, 0, tzinfo=UTC)


def test_an_mlsd_time_is_in_utc() -> None:
    assert parse_mlsd_time("20261012143015.123") == datetime(2026, 10, 12, 14, 30, 15, tzinfo=UTC)
    assert parse_mlsd_time(None) is None


def test_a_stored_file_is_named_after_its_print() -> None:
    assert StoredFile("/cache/Christmas Moose.gcode.3mf", 10, None).task_name == "Christmas Moose"
