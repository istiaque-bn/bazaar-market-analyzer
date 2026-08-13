from django import forms

from notifications.models import AlertRule, AlertRuleType


class AlertRuleForm(forms.ModelForm):
    class Meta:
        model = AlertRule
        fields = [
            "stock",
            "rule_type",
            "target_price",
            "threshold_pct",
            "min_confidence",
            "telegram_enabled",
            "in_app_enabled",
        ]
        widgets = {
            "target_price": forms.NumberInput(attrs={"min": "0", "step": "0.01", "placeholder": "e.g. 125.50"}),
            "threshold_pct": forms.NumberInput(attrs={"min": "0.01", "step": "0.01", "placeholder": "e.g. 3"}),
            "min_confidence": forms.NumberInput(attrs={"min": "0", "max": "100", "step": "1", "placeholder": "e.g. 70"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["stock"].queryset = self.fields["stock"].queryset.filter(is_active=True).order_by("exchange", "trading_code")
        self.fields["stock"].label_from_instance = lambda stock: f"{stock.trading_code} ({stock.exchange})"

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("rule_type")
        if kind == AlertRuleType.TARGET_PRICE and cleaned.get("target_price") is None:
            self.add_error("target_price", "Enter the price that should trigger this alert.")
        elif kind == AlertRuleType.PERCENT_MOVE and cleaned.get("threshold_pct") is None:
            self.add_error("threshold_pct", "Enter the absolute daily move percentage.")
        elif kind == AlertRuleType.CONFIDENCE_CHANGE:
            confidence = cleaned.get("min_confidence")
            if confidence is None:
                self.add_error("min_confidence", "Enter the minimum confidence percentage.")
            elif not 0 <= confidence <= 100:
                self.add_error("min_confidence", "Confidence must be between 0% and 100%.")
        return cleaned
