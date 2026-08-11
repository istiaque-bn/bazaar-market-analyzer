from django.contrib.auth.views import LogoutView
from django.urls import path

from accounts import views

urlpatterns = [
    path("signup/", views.signup_disabled, name="signup"),
    path("login/", views.BazaarLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("password-change/", views.force_password_change, name="force_password_change"),
    path("profile/", views.profile, name="profile"),
    # --- Role panels ---
    path("panel/admin/", views.admin_panel, name="admin_panel"),
    path("panel/staff/", views.staff_panel, name="staff_panel"),
    path("panel/user/", views.user_panel, name="user_panel"),
    # --- Account management ---
    path("manage/", views.account_list, name="account_list"),
    path("manage/create/user/", views.account_create, {"target_role": "user"}, name="account_create_user"),
    path("manage/create/staff/", views.account_create, {"target_role": "staff"}, name="account_create_staff"),
    path("manage/<int:user_id>/", views.account_detail, name="account_detail"),
    path("manage/<int:user_id>/activate/", views.account_activate, name="account_activate"),
    path("manage/<int:user_id>/deactivate/", views.account_deactivate, name="account_deactivate"),
    path("manage/<int:user_id>/delete/", views.account_delete, name="account_delete"),
    path("manage/<int:user_id>/promote/", views.account_promote, name="account_promote"),
    path("manage/<int:user_id>/demote/", views.account_demote, name="account_demote"),
    path("manage/<int:user_id>/reset-password/", views.account_reset_password, name="account_reset_password"),
]
