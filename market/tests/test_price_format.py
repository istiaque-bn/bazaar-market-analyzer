"""
Real DSE/CSE trades only ever land on 0.10-taka tick increments (every
observed close ends in X.Y0). round_to_tick() snaps a raw model output to
that same grid before it's shown as a predicted price.
"""
from django.test import SimpleTestCase

from market.services.price_format import round_to_tick


class RoundToTickTests(SimpleTestCase):
    def test_rounds_down_to_nearest_tick(self):
        self.assertEqual(round_to_tick(16.83), 16.8)

    def test_rounds_up_to_nearest_tick(self):
        self.assertEqual(round_to_tick(16.87), 16.9)

    def test_already_on_tick_is_unchanged(self):
        self.assertEqual(round_to_tick(16.80), 16.8)

    def test_none_passes_through(self):
        self.assertIsNone(round_to_tick(None))

    def test_result_has_no_floating_point_artifacts(self):
        for raw in (16.83, 3.456, 259.27, 0.05, 1234.567):
            result = round_to_tick(raw)
            cents = round(result * 100)
            self.assertEqual(cents % 10, 0, f"{raw} -> {result} is not tick-aligned")
