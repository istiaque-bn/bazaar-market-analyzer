from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase

from market.models import Exchange, PriceHistory, Stock
from market.services.indicators import prices_to_df


class PriceHistoryOrderingTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="TEST", company_name="Test Co")

    def test_default_ordering_is_newest_first(self):
        base = date(2026, 1, 1)
        for i in range(5):
            PriceHistory.objects.create(
                stock=self.stock, date=base + timedelta(days=i), open=10, high=11, low=9, close=10.5, volume=100
            )
        dates = list(self.stock.prices.all().values_list("date", flat=True))
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_prices_to_df_is_ascending_regardless_of_query_order(self):
        base = date(2026, 1, 1)
        for i in (3, 1, 4, 0, 2):
            PriceHistory.objects.create(
                stock=self.stock,
                date=base + timedelta(days=i),
                open=10 + i,
                high=11 + i,
                low=9 + i,
                close=10.5 + i,
                volume=100,
            )
        df = prices_to_df(self.stock.prices.all())
        self.assertEqual(len(df), 5)
        self.assertTrue(df["date"].is_monotonic_increasing)
        self.assertEqual(df.iloc[0]["close"], 10.5)  # day 0
        self.assertEqual(df.iloc[-1]["close"], 14.5)  # day 4

    def test_prices_to_df_empty_queryset_returns_empty_df(self):
        df = prices_to_df(self.stock.prices.all())
        self.assertTrue(df.empty)

    def test_prices_to_df_numeric_columns_are_floats(self):
        PriceHistory.objects.create(
            stock=self.stock, date=date(2026, 1, 1), open=10, high=11, low=9, close=10.5, volume=100
        )
        df = prices_to_df(self.stock.prices.all())
        for col in ("open", "high", "low", "close"):
            self.assertTrue(df[col].dtype.kind == "f", f"{col} should be float, got {df[col].dtype}")


class PriceHistoryUniquenessTests(TestCase):
    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="TEST2", company_name="Test Co 2")

    def test_duplicate_stock_date_rejected(self):
        d = date(2026, 1, 5)
        PriceHistory.objects.create(stock=self.stock, date=d, open=10, high=11, low=9, close=10.5, volume=100)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PriceHistory.objects.create(stock=self.stock, date=d, open=99, high=99, low=99, close=99, volume=1)
        # First row must survive untouched.
        self.assertEqual(PriceHistory.objects.filter(stock=self.stock, date=d).count(), 1)
        self.assertEqual(PriceHistory.objects.get(stock=self.stock, date=d).close, 10.5)

    def test_same_date_allowed_across_different_stocks(self):
        other = Stock.objects.create(exchange=Exchange.CSE, trading_code="TEST2", company_name="Other")
        d = date(2026, 1, 5)
        PriceHistory.objects.create(stock=self.stock, date=d, open=10, high=11, low=9, close=10.5, volume=100)
        # Same trading_code+date is fine on a different exchange/stock row.
        PriceHistory.objects.create(stock=other, date=d, open=20, high=21, low=19, close=20.5, volume=200)
        self.assertEqual(PriceHistory.objects.filter(date=d).count(), 2)


class PriceHistoryValueValidationTests(TestCase):
    """Documents current (permissive) validation behavior at the model
    level — flagged in the report as a product decision, not silently
    changed here."""

    def setUp(self):
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="TEST3", company_name="Test Co 3")

    def test_negative_close_is_currently_accepted_no_validator(self):
        row = PriceHistory.objects.create(
            stock=self.stock, date=date(2026, 1, 1), open=-5, high=-4, low=-6, close=-5.5, volume=100
        )
        row.full_clean(exclude=["stock"])  # does not raise: no MinValueValidator on price fields
        self.assertEqual(PriceHistory.objects.get(pk=row.pk).close, -5.5)

    def test_negative_volume_is_currently_accepted_no_validator(self):
        row = PriceHistory.objects.create(
            stock=self.stock, date=date(2026, 1, 1), open=10, high=11, low=9, close=10.5, volume=-100
        )
        self.assertEqual(PriceHistory.objects.get(pk=row.pk).volume, -100)
