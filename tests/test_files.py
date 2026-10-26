"""Matching a print's task name to the 3mf ha-bambulab cached for it."""

from __future__ import annotations

import pytest

from custom_components.bambu_printer_webhook.files import file_key, task_key


@pytest.mark.parametrize(
    ("filename", "task_name"),
    [
        (
            "54651902-Crochet Christmas Reindeer – Cozy Holiday Decor.3mf",
            "Crochet Christmas Reindeer – Cozy Holiday Decor",
        ),
        (
            "4301309-FC Bayern Logo 0.16mm layer, 2 walls, 15% infill.3mf",
            "FC Bayern Logo 0.16mm layer, 2 walls, 15% infill",
        ),
        ("12958658-Benchy.gcode.3mf", "benchy"),
        ("12958658-Benchy.3mf", "Benchy.3mf"),
        # A task name that starts with digits and a dash keeps them: only
        # the file has a size in front.
        ("2048-2026-Kerstbal.3mf", "2026-Kerstbal"),
    ],
)
def test_a_cached_file_matches_its_task(filename: str, task_name: str) -> None:
    assert file_key(filename) == task_key(task_name)


def test_another_print_does_not_match() -> None:
    assert file_key("4301309-FC Bayern Logo.3mf") != task_key("Knitted Reindeer - Christmas")
