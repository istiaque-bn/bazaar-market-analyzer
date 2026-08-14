from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_userprofile_email_verified_at")]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="digest_watchlist_only",
            field=models.BooleanField(default=False),
        ),
    ]
