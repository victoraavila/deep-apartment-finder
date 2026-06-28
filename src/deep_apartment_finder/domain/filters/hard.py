"""Hard filters for Sprint 1.

These are *hard* filters: a listing failing any of them is dropped. Soft
(scoring) criteria arrive in Sprint 2.

Source of truth: docs/SPRINT1.md lines 45-56.
"""

from __future__ import annotations

from dataclasses import dataclass

from deep_apartment_finder.domain.apartment import Apartment


@dataclass(frozen=True, slots=True)
class HardFilters:
    """Immutable hard filter set for a search.

    A listing must satisfy every filter. Filters with `None` are not applied.
    """

    city: str = "Zaragoza"
    min_rooms: int | None = 2
    min_bathrooms: int | None = 2
    min_size_m2: float | None = 50.0
    max_price_eur: float | None = 1200.0

    def passes(self, apartment: Apartment) -> bool:
        if self.min_rooms is not None and (
            apartment.rooms is None or apartment.rooms < self.min_rooms
        ):
            return False
        if self.min_bathrooms is not None and (
            apartment.bathrooms is None or apartment.bathrooms < self.min_bathrooms
        ):
            return False
        if self.min_size_m2 is not None and (
            apartment.size_m2 is None or float(apartment.size_m2) < self.min_size_m2
        ):
            return False
        if self.max_price_eur is not None and (
            apartment.price_eur is None or float(apartment.price_eur) > self.max_price_eur
        ):
            return False
        return True
