from datetime import date, timedelta

from django.test import TestCase

from market.models import Exchange, ShadowForecast, Stock
from market.services.shadow_model import NAME, render_shadow_report_text, shadow_report


def _mk_row(stock, *, target_date, last_close, predicted_close, actual_close):
    return ShadowForecast.objects.create(
        stock=stock,
        as_of=target_date - timedelta(days=1),
        target_date=target_date,
        last_close=last_close,
        predicted_close=predicted_close,
        predicted_return=(predicted_close - last_close) / last_close,
        candidate_name=NAME,
        actual_close=actual_close,
        settled_at=None,
    )


class ShadowReportMetricsTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="SHDW", company_name="Shadow Co")

    def test_no_rows_reports_zero_and_no_trend(self):
        result = shadow_report()
        self.assertEqual(result, {"n": 0, "trend": None})

    def test_skill_and_direction_computed_from_settled_rows(self):
        today = date.today()
        # actual always 102 vs last_close 100 (naive error = 2); model
        # error = 1 -> skill = 1 - 1/2 = 0.5. Direction is correctly "up".
        for i in range(5):
            _mk_row(self.stock, target_date=today - timedelta(days=i), last_close=100, predicted_close=101, actual_close=102)
        result = shadow_report()
        self.assertEqual(result["n"], 5)
        self.assertAlmostEqual(result["skill"], 0.5)
        self.assertEqual(result["direction"], 1.0)

    def test_below_trend_threshold_reports_no_trend(self):
        today = date.today()
        for i in range(10):
            _mk_row(self.stock, target_date=today - timedelta(days=i), last_close=100, predicted_close=101, actual_close=102)
        result = shadow_report()
        self.assertIsNone(result["trend"])

    def test_recent_half_better_than_prior_half_is_improving(self):
        today = date.today()
        # Most recent 20 rows: accurate (skill 0.5). Older 20 rows (well
        # separated in time): inaccurate (skill -1). order_by("-target_date")
        # puts the accurate, more-recent rows first -> recent_skill > prior_skill.
        for i in range(20):
            _mk_row(self.stock, target_date=today - timedelta(days=i), last_close=100, predicted_close=101, actual_close=102)
        for i in range(20):
            _mk_row(self.stock, target_date=today - timedelta(days=40 + i), last_close=100, predicted_close=106, actual_close=102)
        result = shadow_report()
        self.assertEqual(result["n"], 40)
        self.assertEqual(result["trend"], "improving")

    def test_recent_half_worse_than_prior_half_is_declining(self):
        today = date.today()
        for i in range(20):
            _mk_row(self.stock, target_date=today - timedelta(days=i), last_close=100, predicted_close=106, actual_close=102)
        for i in range(20):
            _mk_row(self.stock, target_date=today - timedelta(days=40 + i), last_close=100, predicted_close=101, actual_close=102)
        result = shadow_report()
        self.assertEqual(result["trend"], "declining")

    def test_similar_halves_are_stable(self):
        today = date.today()
        for i in range(40):
            _mk_row(self.stock, target_date=today - timedelta(days=i), last_close=100, predicted_close=101, actual_close=102)
        result = shadow_report()
        self.assertEqual(result["trend"], "stable")


class ShadowReportTextTests(TestCase):
    def test_no_data_is_plain_language_not_a_bare_number(self):
        text = render_shadow_report_text({"n": 0, "trend": None})
        self.assertIn("No completed price checks yet", text)
        self.assertIn("never affects real forecasts", text)
        # Never a bare-numbers dump: no raw metric labels leak into the text.
        self.assertNotIn("MAE", text)
        self.assertNotIn("Skill vs naive", text)

    def test_positive_skill_is_framed_as_better_than_naive(self):
        text = render_shadow_report_text({"n": 100, "mae": 0.8, "naive_mae": 1.0, "skill": 0.2, "direction": 0.55, "trend": "improving"})
        self.assertIn("closer to the real price", text)
        self.assertIn("Trend: Improving", text)
        self.assertIn("55 times out of 100", text)
        self.assertNotIn("MAE", text)

    def test_negative_skill_is_framed_as_worse_than_naive(self):
        text = render_shadow_report_text({"n": 100, "mae": 1.2, "naive_mae": 1.0, "skill": -0.1955, "direction": 0.33, "trend": "declining"})
        self.assertIn("further from the real price", text)
        self.assertIn("doing worse than doing nothing", text)
        self.assertIn("Trend: Declining", text)

    def test_unknown_trend_falls_back_gracefully(self):
        text = render_shadow_report_text({"n": 10, "mae": 1.0, "naive_mae": 1.0, "skill": 0.0, "direction": None, "trend": None})
        self.assertIn("Not enough history yet", text)
