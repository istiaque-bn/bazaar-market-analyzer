from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from accounts.forms import ProfileForm
from accounts.models import UserProfile


class SignUpForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")


@require_http_methods(["GET", "POST"])
def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = SignUpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("dashboard")
    return render(request, "registration/signup.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def profile(request):
    profile_obj, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        form = ProfileForm(request.POST)
        if not form.is_valid():
            for field_errors in form.errors.values():
                for error in field_errors:
                    messages.error(request, error)
            return render(request, "accounts/profile.html", {"profile": profile_obj})
        data = form.cleaned_data
        profile_obj.telegram_chat_id = data["telegram_chat_id"]
        profile_obj.email_alerts = data["email_alerts"]
        profile_obj.telegram_alerts = data["telegram_alerts"]
        profile_obj.min_score_alert = data["min_score_alert"]
        profile_obj.preferred_exchanges = data["preferred_exchanges"]
        profile_obj.save()
        if "email" in request.POST:
            request.user.email = data["email"]
            request.user.save(update_fields=["email"])
        return redirect("profile")
    return render(request, "accounts/profile.html", {"profile": profile_obj})
