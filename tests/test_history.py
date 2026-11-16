"""Reading a printer's storage listing and the 3mf files on it."""

from __future__ import annotations

import base64
import io
import zipfile
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.bambu_printer_webhook.history import (
    StoredFile,
    parse_list_line,
    parse_mlsd_time,
    prints_in,
    read_gcode_head,
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
            '<metadata key="weight" value="42.75"/>'
            '<object identify_id="1" name="Gewei" skipped="false"/>'
            '<object identify_id="2" name="Gewei" skipped="false"/>'
            '<object identify_id="3" name="Gewei" skipped="true"/></plate>'
            "</config>",
        )
        archive.writestr(f"Metadata/plate_{plate}.gcode", "G28")
        archive.writestr("Metadata/plate_1.png", b"plate-1")
        archive.writestr("Metadata/plate_2.png", b"plate-2")
    return buffer.getvalue()


def test_the_plate_a_3mf_prints_gives_its_weight_time_objects_and_picture() -> None:
    info = read_model_info(_sliced_3mf(plate=2))

    assert (info.plate, info.objects) == (2, 2)
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

    assert recent == ("Benchy.gcode.3mf", False, 12958658, datetime(2026, 11, 14, 9, 31, tzinfo=UTC), True)
    assert older == ("Old box.gcode.3mf", False, 1048576, datetime(2025, 3, 2, tzinfo=UTC), False)
    assert folder is not None and folder[1] is True


def test_a_list_time_later_than_now_is_from_last_year() -> None:
    entry = parse_list_line("-rw-rw-rw-   1 root  root  10 Dec 24 18:00 Kerstbal.3mf", NOW)

    assert entry is not None and entry[3] == datetime(2025, 12, 24, 18, 0, tzinfo=UTC)


def test_an_mlsd_time_is_in_utc() -> None:
    assert parse_mlsd_time("20261012143015.123") == datetime(2026, 10, 12, 14, 30, 15, tzinfo=UTC)
    assert parse_mlsd_time(None) is None


def test_a_stored_file_is_named_after_its_print() -> None:
    assert StoredFile("/cache/Christmas Moose.gcode.3mf", 10, None).task_name == "Christmas Moose"


def test_a_list_time_is_in_the_printers_own_time_zone() -> None:
    amsterdam = ZoneInfo("Europe/Amsterdam")

    entry = parse_list_line("-rw-rw-rw-   1 root  root  10 Nov 16 08:18 Moose.3mf", NOW, amsterdam)

    assert entry is not None and entry[3] == datetime(2026, 11, 16, 7, 18, tzinfo=UTC)


def test_each_other_plate_of_a_project_is_a_print_of_its_own() -> None:
    sent = datetime(2026, 11, 16, 8, 18, tzinfo=UTC)
    entries = [
        ("Christmas Moose Tea Light.3mf", False, 9, sent, True),
        ("Christmas Moose Tea Light_plate_1.gcode", False, 9, sent + timedelta(minutes=11), True),
        ("Christmas Moose Tea Light_plate_2.gcode", False, 9, datetime(2026, 11, 1, 9, 29, tzinfo=UTC), True),
        ("0_plate_1.gcode", False, 9, datetime(2026, 10, 31, 13, 50, tzinfo=UTC), True),
        ("1_Christmas Moose Tea Light.bbl", False, 9, sent, True),
    ]

    prints = prints_in("/cache/", entries)

    assert [(stored.path, stored.task_name, stored.is_plate) for stored in prints] == [
        ("/cache/Christmas Moose Tea Light.3mf", "Christmas Moose Tea Light", False),
        ("/cache/Christmas Moose Tea Light_plate_2.gcode", "Christmas Moose Tea Light", True),
    ]


def test_a_plates_g_code_header_gives_its_weight_time_and_largest_picture() -> None:
    small = base64.b64encode(b"\x89PNG small").decode()
    large = base64.b64encode(b"\x89PNG large picture").decode()
    head = (
        "; HEADER_BLOCK_START\n"
        "; model printing time: 1h 2m 3s; total estimated time: 1d 1h 0m 5s\n"
        "; total filament weight [g] : 10.25,2.50\n"
        "; model label id: 8,39,50,61\n"
        "; HEADER_BLOCK_END\n"
        "; THUMBNAIL_BLOCK_START\n"
        f"; thumbnail begin 50x50 {len(small)}\n; {small}\n; thumbnail end\n"
        f"; thumbnail begin 300x300 {len(large)}\n; {large[:8]}\n; {large[8:]}\n; thumbnail end\n"
        "; THUMBNAIL_BLOCK_END\n"
    ).encode()

    info = read_gcode_head(head)

    assert (info.weight_grams, info.print_seconds, info.thumbnail) == (12.75, 90005, b"\x89PNG large picture")
    assert info.objects == 4
