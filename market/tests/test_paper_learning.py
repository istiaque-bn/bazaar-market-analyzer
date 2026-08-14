from datetime import date

from django.test import TestCase

from market.models import Exchange, PaperLearningFeedback, PaperPosition, PaperTradingAccount, Stock
from market.services.paper_learning import paper_evidence_report, paper_learning_report


class PaperLearningReportTests(TestCase):
    def _feedback(self, number, confidence, net_return):
        account = PaperTradingAccount.objects.get_or_create(name="Learning test account")[0]
        stock = Stock.objects.create(
            exchange=Exchange.DSE, trading_code=f"LEARN{number}", company_name=f"Learning {number}", last_price=100,
        )
        position = PaperPosition.objects.create(
            account=account, stock=stock, quantity=1, entry_price=100, opened_on=date(2026, 1, 1), is_open=False,
        )
        return PaperLearningFeedback.objects.create(
            position=position, stock=stock, signal_date=date(2026, 1, 1), outcome_date=date(2026, 1, 5),
            predicted_confidence=confidence, gross_return_pct=net_return, net_return_pct=net_return,
            profitable_after_costs=net_return > 0, holding_sessions=3, exit_reason="holding_period",
        )

    def test_waits_for_enough_completed_trades(self):
        for number in range(8):
            self._feedback(number, 0.8, 1.0)

        report = paper_learning_report()

        self.assertFalse(report["ready_for_review"])
        self.assertIn("8/30", report["recommendation"])

    def test_identifies_best_confidence_range_after_sufficient_evidence(self):
        for number in range(10):
            self._feedback(number, 0.85, 1.5)
        for number in range(10, 30):
            self._feedback(number, 0.72, -0.5)

        report = paper_learning_report()

        self.assertTrue(report["ready_for_review"])
        self.assertIn("80% and above", report["recommendation"])

    def test_evidence_report_is_scoped_to_one_paper_account_and_never_calls_it_an_edge(self):
        for number in range(3):
            self._feedback(number, 0.8, 1.0)
        other = PaperTradingAccount.objects.create(name="Other evidence account")
        stock = Stock.objects.create(exchange=Exchange.DSE, trading_code="OTHER", last_price=100)
        position = PaperPosition.objects.create(account=other, stock=stock, quantity=1, entry_price=100, opened_on=date(2026, 1, 1), is_open=False)
        PaperLearningFeedback.objects.create(position=position, stock=stock, signal_date=date(2026, 1, 1), outcome_date=date(2026, 1, 5), gross_return_pct=50, net_return_pct=50, profitable_after_costs=True, holding_sessions=3, exit_reason="target")

        account = PaperTradingAccount.objects.get(name="Learning test account")
        report = paper_evidence_report(account)

        self.assertEqual(report["completed_trades"], 3)
        self.assertIn("Insufficient", report["evidence_status"])
