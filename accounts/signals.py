from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User

from accounts.models import UserProfile
from market.models import Watchlist


@receiver(post_save, sender=User)
def create_user_defaults(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)
        Watchlist.objects.get_or_create(user=instance, name="Default")
