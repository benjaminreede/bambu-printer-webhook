"""What each printer's plug has cost, at the price of the moment it was used.

A dynamic tariff changes every quarter of an hour, so a print can't be paid
at one price. Each new reading of the plug's meter is paid at the price of
that moment instead, and added to a running total in euros per printer. Like
the meter, the total only goes up: what a print cost is the total at its end
minus the one at its start.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostMeter:
    """One plug's last reading and what it has cost up to that reading."""

    kwh: float | None = None
    cost: float = 0.0

    def read(self, kwh: float | None, price_per_kwh: float | None) -> bool:
        """Pay what was used since the last reading; whether anything changed.

        Without a price the last reading stays, so what is used meanwhile is
        paid once a price is known again. A meter that went back (reset or
        replaced) starts over from its new reading.
        """
        if kwh is None:
            return False
        if self.kwh is None or kwh < self.kwh:
            self.kwh = kwh
            return True
        if price_per_kwh is None or kwh == self.kwh:
            return False
        self.cost += (kwh - self.kwh) * price_per_kwh
        self.kwh = kwh
        return True
