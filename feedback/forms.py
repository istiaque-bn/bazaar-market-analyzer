"""Feedback forms — every form here is a plain forms.Form (never a
ModelForm bound to Feedback), the same mass-assignment defense used by
accounts/forms.py's account-creation forms: a form that never declares
`status`/`admin_priority`/`assigned_to`/`internal_notes` as a field
cannot be used to set them, regardless of what a manipulated POST body
contains — feedback/services.py enforces who's allowed to change what
for the fields that ARE exposed, on their own dedicated small forms.
"""
from __future__ import annotations

from django import forms

from feedback.models import FeedbackCategory, FeedbackPriority, FeedbackStatus


class FeedbackSubmitForm(forms.Form):
    category = forms.ChoiceField(choices=FeedbackCategory.choices)
    title = forms.CharField(max_length=140, min_length=5)
    description = forms.CharField(widget=forms.Textarea, max_length=5000, min_length=10)
    reporter_priority = forms.ChoiceField(choices=FeedbackPriority.choices, initial=FeedbackPriority.NORMAL)
    page_path = forms.CharField(max_length=255, required=False)
    steps_to_reproduce = forms.CharField(widget=forms.Textarea, max_length=3000, required=False)
    expected_behavior = forms.CharField(widget=forms.Textarea, max_length=2000, required=False)
    actual_behavior = forms.CharField(widget=forms.Textarea, max_length=2000, required=False)
    contact_allowed = forms.BooleanField(required=False, initial=True)
    # Which stock/analysis this is about, if any — used only as a lookup
    # key for feedback.services.capture_diagnostic_metadata; the actual
    # diagnostic fields it captures are never taken from this form.
    meta_exchange = forms.CharField(max_length=3, required=False)
    meta_trading_code = forms.CharField(max_length=32, required=False)

    def clean_title(self):
        return self.cleaned_data["title"].strip()

    def clean_description(self):
        return self.cleaned_data["description"].strip()

    def clean_meta_exchange(self):
        return self.cleaned_data.get("meta_exchange", "").strip().upper()

    def clean_meta_trading_code(self):
        return self.cleaned_data.get("meta_trading_code", "").strip().upper()


class FollowUpForm(forms.Form):
    text = forms.CharField(widget=forms.Textarea, max_length=2000, min_length=3, label="Follow-up")

    def clean_text(self):
        return self.cleaned_data["text"].strip()


class StatusChangeForm(forms.Form):
    status = forms.ChoiceField(choices=FeedbackStatus.choices)
    note = forms.CharField(widget=forms.Textarea, max_length=1000, required=False)


class InternalNoteForm(forms.Form):
    note = forms.CharField(widget=forms.Textarea, max_length=2000, min_length=1)


class AdminResponseForm(forms.Form):
    response = forms.CharField(widget=forms.Textarea, max_length=3000, min_length=1, label="Public response")


class AdminPriorityForm(forms.Form):
    admin_priority = forms.ChoiceField(choices=FeedbackPriority.choices)


class AssignForm(forms.Form):
    assignee_id = forms.IntegerField()


class DuplicateForm(forms.Form):
    original_reference = forms.CharField(max_length=16, label="Original reference number (e.g. FB-000042)")

    def clean_original_reference(self):
        return self.cleaned_data["original_reference"].strip().upper()
