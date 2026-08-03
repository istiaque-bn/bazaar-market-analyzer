"""Forms for user-facing mutation endpoints outside the accounts app."""
from __future__ import annotations

from decimal import Decimal

from django import forms

from market.models import Portfolio, Stock, TransactionType


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
    allow_fractional = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stock"].queryset = Stock.objects.filter(is_active=True).order_by("trading_code")

    def clean_fees(self):
        return self.cleaned_data.get("fees") or Decimal("0")

    def clean_notes(self):
        # Django's template auto-escaping handles output; this just keeps
        # stray control characters/whitespace out of stored notes.
        return (self.cleaned_data.get("notes") or "").strip()

    def clean(self):
        cleaned = super().clean()
        quantity = cleaned.get("quantity")
        if quantity is not None and not cleaned.get("allow_fractional"):
            # DSE/CSE only trade whole shares — the model itself stays a
            # DecimalField (see market.models.PortfolioTransaction) so a
            # future fractional-share venue wouldn't need a schema change,
            # but the standard form enforces the real-world constraint.
            if quantity != quantity.to_integral_value():
                self.add_error("quantity", "DSE/CSE shares must be a whole number.")
        return cleaned
