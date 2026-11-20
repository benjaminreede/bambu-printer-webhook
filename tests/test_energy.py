"""The running cost of a plug's meter."""

from __future__ import annotations

import pytest

from custom_components.bambu_printer_webhook.energy import CostMeter


def test_the_first_reading_is_only_a_starting_point() -> None:
    meter = CostMeter()

    meter.read(100.0, 0.30)

    assert (meter.kwh, meter.cost) == (100.0, 0.0)


def test_what_is_used_without_a_price_is_paid_once_a_price_is_known_again() -> None:
    meter = CostMeter(kwh=100.0)

    meter.read(101.0, None)
    meter.read(102.0, 0.25)

    assert (meter.kwh, meter.cost) == (102.0, pytest.approx(0.5))


def test_a_meter_that_went_back_starts_over_without_cost() -> None:
    meter = CostMeter(kwh=100.0, cost=3.0)

    meter.read(0.2, 0.30)

    assert (meter.kwh, meter.cost) == (0.2, 3.0)
