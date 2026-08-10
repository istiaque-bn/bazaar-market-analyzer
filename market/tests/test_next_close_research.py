import pandas as pd
from django.test import TestCase

from market.models import Exchange, Stock
from market.services.next_close_research import split_final_holdout, sector_data_is_usable


class NextCloseResearchSafetyTests(TestCase):
    def test_final_holdout_is_strictly_later_than_research_data(self):
        panel = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=200, freq="D"), "x": range(200)})
        research, holdout = split_final_holdout(panel, calendar_days=60)
        self.assertFalse(research.empty)
        self.assertFalse(holdout.empty)
        self.assertLess(research["date"].max(), holdout["date"].min())

    def test_blank_sector_data_is_not_accepted_as_a_sector_feature(self):
        for number in range(10):
            Stock.objects.create(exchange=Exchange.DSE, trading_code=f"SEC{number}", company_name="Sector Test", sector="")
        self.assertFalse(sector_data_is_usable(Exchange.DSE))
