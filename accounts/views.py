from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

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
        try:
            min_score_alert = float(request.POST.get("min_score_alert") or 40)
        except ValueError:
            messages.error(request, "Min score alert must be a number.")
            return render(request, "accounts/profile.html", {"profile": profile_obj})
        profile_obj.telegram_chat_id = request.POST.get("telegram_chat_id", "").strip()
        profile_obj.email_alerts = request.POST.get("email_alerts") == "on"
        profile_obj.telegram_alerts = request.POST.get("telegram_alerts") == "on"
        profile_obj.min_score_alert = min_score_alert
        profile_obj.preferred_exchanges = request.POST.get("preferred_exchanges") or "DSE,CSE"
        profile_obj.save()
        request.user.email = request.POST.get("email", request.user.email)
        request.user.save(update_fields=["email"])
        return redirect("profile")
    return render(request, "accounts/profile.html", {"profile": profile_obj})
