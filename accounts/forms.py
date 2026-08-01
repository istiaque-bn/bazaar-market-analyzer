from django import forms

from market.models import Exchange


class ProfileForm(forms.Form):
    email = forms.EmailField(required=False)
    telegram_chat_id = forms.CharField(required=False, max_length=64)
    min_score_alert = forms.FloatField(
        required=False,
        min_value=0,
        max_value=100,
        error_messages={"invalid": "Min score alert must be a number."},
    )
    preferred_exchanges = forms.CharField(required=False, max_length=16)
    email_alerts = forms.BooleanField(required=False)
    telegram_alerts = forms.BooleanField(required=False)

    def clean_min_score_alert(self):
        value = self.cleaned_data.get("min_score_alert")
        return 40.0 if value is None else value

    def clean_preferred_exchanges(self):
        raw = self.cleaned_data.get("preferred_exchanges") or ""
        tokens = [t.strip().upper() for t in raw.split(",") if t.strip()]
        valid = set(Exchange.values)
        invalid = [t for t in tokens if t not in valid]
        if invalid:
            raise forms.ValidationError(
                f"Unknown exchange(s): {', '.join(invalid)}. Choose from {', '.join(sorted(valid))}."
            )
        return ",".join(tokens) if tokens else "DSE,CSE"
