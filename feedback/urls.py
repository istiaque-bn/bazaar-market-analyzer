from django.urls import path

from feedback import views

urlpatterns = [
    path("submit/", views.submit, name="feedback_submit"),
    path("mine/", views.my_list, name="feedback_my_list"),
    path("<int:pk>/", views.detail, name="feedback_detail"),
    path("<int:pk>/follow-up/", views.follow_up, name="feedback_follow_up"),
    path("<int:pk>/withdraw/", views.withdraw_view, name="feedback_withdraw"),
    path("<int:pk>/dispute/", views.dispute_view, name="feedback_dispute"),
    # Staff + Admin triage
    path("triage/", views.triage_list, name="feedback_triage_list"),
    path("<int:pk>/status/", views.change_status, name="feedback_change_status"),
    path("<int:pk>/note/", views.add_note, name="feedback_add_note"),
    path("<int:pk>/assign/", views.assign_view, name="feedback_assign"),
    path("<int:pk>/assign-to-me/", views.assign_to_me, name="feedback_assign_to_me"),
    path("<int:pk>/response/", views.post_response_view, name="feedback_post_response"),
    # Admin only
    path("<int:pk>/priority/", views.set_priority, name="feedback_set_priority"),
    path("<int:pk>/duplicate/", views.mark_duplicate_view, name="feedback_mark_duplicate"),
    path("admin/dashboard/", views.admin_dashboard, name="feedback_admin_dashboard"),
    path("admin/export/", views.export, name="feedback_export"),
]
