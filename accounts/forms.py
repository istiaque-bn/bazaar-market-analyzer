from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User

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


class BazaarAuthenticationForm(AuthenticationForm):
    """Same validation as Django's stock AuthenticationForm (already
    generic-error / anti-enumeration / inactive-blocking safe — see
    ModelBackend.user_can_authenticate) — only widget attributes differ,
    for the redesigned login page (autocomplete hints a password manager
    can act on, placeholder text, no framework)."""

    username = forms.CharField(
        widget=forms.TextInput(
            attrs={"autofocus": True, "autocomplete": "username", "class": "auth-input", "placeholder": "Username"}
        )
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={"autocomplete": "current-password", "class": "auth-input", "placeholder": "Password"}
        )
    )


class _BaseAccountCreateForm(forms.Form):
    """Shared identity fields for account creation. Deliberately a plain
    Form (not a ModelForm bound to User) — the view never round-trips
    arbitrary POST data into User.objects.create_user(); only these
    explicitly declared+cleaned fields are ever read, so a manipulated
    extra field in the payload (is_staff, is_superuser, groups, role,
    user_permissions, ...) is simply never looked at, regardless of
    which concrete subclass/actor is submitting."""

    username = forms.CharField(max_length=150, min_length=3)
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    is_active = forms.BooleanField(required=False, initial=True)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    def clean_email(self):
        # Email-uniqueness policy: a blank email is always allowed (kept
        # for legacy/local accounts that never supplied one) but a
        # *non-blank* email must be unique — password-reset-by-email and
        # any future "log in with email" path would otherwise be
        # ambiguous about which account it refers to.
        email = self.cleaned_data.get("email", "").strip()
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with that email already exists.")
        return email


class AdminCreateAccountForm(_BaseAccountCreateForm):
    """Admin creating an account. Deliberately has no `role` field — the
    target role (Staff vs. User) comes from which URL the Admin used
    ("Create Staff" vs. "Create User", each a distinct urls.py entry
    with a server-fixed `target_role`), never from posted form data.
    Admin accounts are never created here (see docs: only
    `manage.py createsuperuser`)."""


class StaffCreateAccountForm(_BaseAccountCreateForm):
    """Staff creating an account: same fields, no role field — the view
    fixes the new account's role to User unconditionally regardless of
    which form class handled the request."""


class ForcedPasswordChangeForm(forms.Form):
    """First-login password change after an Admin/Staff-assigned
    temporary password. Deliberately does not ask for the old/temporary
    password (the user just proved possession of it by logging in) —
    only the new password, twice, run through the same validators as
    every other password in this project."""

    new_password1 = forms.CharField(
        label="New password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )
    new_password2 = forms.CharField(
        label="Confirm new password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"})
    )

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_new_password2(self):
        p1 = self.cleaned_data.get("new_password1")
        p2 = self.cleaned_data.get("new_password2")
        if p1 and p2 and p1 != p2:
            raise forms.ValidationError("The two password fields didn't match.")
        from django.contrib.auth.password_validation import validate_password

        validate_password(p2, self.user)
        return p2
