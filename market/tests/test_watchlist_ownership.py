from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from market.models import Exchange, Stock, Watchlist


class WatchlistOwnershipTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username="alice_wl", password="Correct-Horse-Battery-Staple-42")
        self.bob = User.objects.create_user(username="bob_wl", password="Correct-Horse-Battery-Staple-42")
        self.stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="WLX", company_name="Watchlist Co")

    def test_anonymous_cannot_view_watchlist(self):
        response = self.client.get(reverse("watchlist"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_anonymous_cannot_toggle_watchlist(self):
        response = self.client.post(reverse("toggle_watchlist", args=["DSE", "WLX"]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_adding_to_own_watchlist_does_not_affect_other_users(self):
        self.client.login(username="alice_wl", password="Correct-Horse-Battery-Staple-42")
        self.client.post(reverse("toggle_watchlist", args=["DSE", "WLX"]))

        alice_wl = Watchlist.objects.get(user=self.alice, name="Default")
        self.assertTrue(alice_wl.stocks.filter(id=self.stock.id).exists())

        bob_wl, _ = Watchlist.objects.get_or_create(user=self.bob, name="Default")
        self.assertFalse(bob_wl.stocks.filter(id=self.stock.id).exists())

    def test_watchlist_view_only_shows_own_stocks(self):
        alice_wl, _ = Watchlist.objects.get_or_create(user=self.alice, name="Default")
        alice_wl.stocks.add(self.stock)
        other_stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="OTHERX", company_name="Other")
        bob_wl, _ = Watchlist.objects.get_or_create(user=self.bob, name="Default")
        bob_wl.stocks.add(other_stock)

        self.client.login(username="alice_wl", password="Correct-Horse-Battery-Staple-42")
        response = self.client.get(reverse("watchlist"))
        codes = {s.trading_code for s, _ in response.context["rows"]}
        self.assertEqual(codes, {"WLX"})

    def test_toggle_twice_adds_then_removes(self):
        self.client.login(username="alice_wl", password="Correct-Horse-Battery-Staple-42")
        url = reverse("toggle_watchlist", args=["DSE", "WLX"])
        self.client.post(url)
        wl = Watchlist.objects.get(user=self.alice, name="Default")
        self.assertTrue(wl.stocks.filter(id=self.stock.id).exists())
        self.client.post(url)
        wl.refresh_from_db()
        self.assertFalse(wl.stocks.filter(id=self.stock.id).exists())
