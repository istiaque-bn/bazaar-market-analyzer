from django.contrib import admin
from django.urls import include, path
from django.conf.urls.i18n import set_language

urlpatterns = [
    path("i18n/setlang/", set_language, name="set_language"),
    path("admin/", admin.site.urls),
    path("api/", include("api.urls")),
    path("accounts/", include("accounts.urls")),
    path("feedback/", include("feedback.urls")),
    path("", include("market.urls")),
]
