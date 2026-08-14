"""Forms for user-facing mutation endpoints outside the accounts app."""
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.db.models import Q
from django.utils import timezone

from market.models import Portfolio, PortfolioGoal, ResearchNote, Stock, TransactionType
from market.services.exchange_config import enabled_exchanges


class PortfolioForm(forms.ModelForm):
    class Meta:
        model = Portfolio
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={"maxlength": 64, "autofocus": True})}

    def __init__(self, *args, user=None, **kwargs):
        self._user = user
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Portfolio name is required.")
        qs = Portfolio.objects.filter(user=self._user, name__iexact=name)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if self._user is not None and qs.exists():
            raise forms.ValidationError("You already have a portfolio with this name.")
        return name


class PortfolioGoalForm(forms.ModelForm):
    class Meta:
        model = PortfolioGoal
        fields = ["target_value", "target_date", "max_single_position_pct"]
        widgets = {
            "target_value": forms.NumberInput(attrs={"min": "0", "step": "0.01", "placeholder": "e.g. 250000"}),
            "target_date": forms.DateInput(attrs={"type": "date"}),
            "max_single_position_pct": forms.NumberInput(attrs={"min": "1", "max": "100", "step": "1"}),
        }

    def clean_max_single_position_pct(self):
        value = self.cleaned_data["max_single_position_pct"]
        if not 1 <= value <= 100:
            raise forms.ValidationError("Set a concentration limit between 1% and 100%.")
        return value


class ResearchNoteForm(forms.ModelForm):
    class Meta:
        model = ResearchNote
        fields = ["title", "body", "target_price"]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "Your investment thesis", "maxlength": 140}),
            "body": forms.Textarea(attrs={"rows": 5, "placeholder": "What supports or challenges this idea?"}),
            "target_price": forms.NumberInput(attrs={"min": "0", "step": "0.01", "placeholder": "Optional"}),
        }


def _stock_label(stock: Stock) -> str:
    name = f" — {stock.company_name}" if stock.company_name else ""
    return f"{stock.trading_code} ({stock.exchange}){name}"


class StockChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return _stock_label(obj)


class TransactionForm(forms.Form):
    """Shared by the full "add transaction" flow and, with fewer visible
    fields, the simplified "add holding" flow (see views.portfolio_add_holding).
    Kept as a plain Form (not a ModelForm) because transaction_type and the
    stock/portfolio scoping need view-level control that's simpler to
    reason about explicitly than fighting a ModelForm's field set."""

    stock = StockChoiceField(queryset=Stock.objects.none(), empty_label="Select a stock…")
    transaction_type = forms.ChoiceField(choices=TransactionType.choices, initial=TransactionType.BUY)
    quantity = forms.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.0001"))
    price_per_share = forms.DecimalField(max_digits=14, decimal_places=4, min_value=Decimal("0"))
    fees = forms.DecimalField(max_digits=12, decimal_places=4, min_value=Decimal("0"), required=False, initial=Decimal("0"))
    transaction_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2, "maxlength": 2000}))
    thesis = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3, "maxlength": 4000, "placeholder": "Why do you own or trade this share?"}))
    target_price = forms.DecimalField(required=False, max_digits=14, decimal_places=4, min_value=Decimal("0"), widget=forms.NumberInput(attrs={"min": "0", "step": "0.01", "placeholder": "Optional BDT target"}))
    invalidation = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2, "maxlength": 2000, "placeholder": "What would prove this idea wrong or make you exit?"}))
    post_trade_review = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3, "maxlength": 4000, "placeholder": "After closing or reducing: what happened and what did you learn?"}))
    allow_fractional = forms.BooleanField(required=False, widget=forms.HiddenInput)


    def __init__(self, *args, portfolio: Portfolio | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        # New selections are limited to currently-enabled exchanges — a
        # disabled exchange (e.g. CSE) never appears as a choice for a
        # brand-new position. A stock the portfolio has *already*
        # transacted in stays selectable even if its exchange has since
        # been disabled, so a corrective SELL/close-out against an
        # existing holding remains possible; validate_transaction() is
        # still the actual enforcement point (rejects a BUY either way),
        # this is just keeping the dropdown honest about what's realistic.
        qs = Stock.objects.filter(is_active=True, exchange__in=enabled_exchanges())
        if portfolio is not None:
            held_ids = list(portfolio.transactions.values_list("stock_id", flat=True).distinct())
            if held_ids:
                qs = Stock.objects.filter(is_active=True).filter(
                    Q(exchange__in=enabled_exchanges()) | Q(id__in=held_ids)
                )
        self.fields["stock"].queryset = qs.order_by("trading_code")

    def clean_fees(self):
        return self.cleaned_data.get("fees") or Decimal("0")

    def clean_notes(self):
        # Django's template auto-escaping handles output; this just keeps
        # stray control characters/whitespace out of stored notes.
        return (self.cleaned_data.get("notes") or "").strip()

    def clean(self):
        cleaned = super().clean()
        for name in ("thesis", "invalidation", "post_trade_review"):
            cleaned[name] = (cleaned.get(name) or "").strip()
        quantity = cleaned.get("quantity")
        if quantity is not None and not cleaned.get("allow_fractional"):
            if quantity != quantity.to_integral_value():
                self.add_error("quantity", "DSE/CSE shares must be a whole number.")
        return cleaned


class PortfolioCSVImportForm(forms.Form):
    csv_file = forms.FileField()

    def clean_csv_file(self):
        uploaded = self.cleaned_data["csv_file"]
        if uploaded.size > 1_000_000:
            raise forms.ValidationError("CSV file must be 1 MB or smaller.")
        if not uploaded.name.lower().endswith(".csv"):
            raise forms.ValidationError("Upload a .csv file exported from your broker.")
        return uploaded


class AdminReminderForm(forms.Form):
    remind_on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    action = forms.CharField(
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Probable action to take"}),
    )
    telegram_enabled = forms.BooleanField(required=False, initial=True)
    email_enabled = forms.BooleanField(required=False, initial=False)

    def clean_remind_on(self):
        remind_on = self.cleaned_data["remind_on"]
        if remind_on < timezone.localdate():
            raise forms.ValidationError("Choose today or a future date.")
        return remind_on

    def clean_action(self):
        action = (self.cleaned_data["action"] or "").strip()
        if not action:
            raise forms.ValidationError("Please describe the action to take.")
        return action
