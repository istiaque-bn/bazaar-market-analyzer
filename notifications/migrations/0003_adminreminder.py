from django.conf import settings
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies=[("notifications","0002_mldailyreportdelivery")]
    operations=[migrations.CreateModel(name="AdminReminder",fields=[("id",models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name="ID")),("remind_on",models.DateField(db_index=True)),("action",models.TextField(max_length=1000)),("telegram_enabled",models.BooleanField(default=True)),("email_enabled",models.BooleanField(default=False)),("delivered_at",models.DateTimeField(blank=True,null=True)),("created_at",models.DateTimeField(auto_now_add=True)),("admin",models.ForeignKey(on_delete=models.deletion.CASCADE,related_name="admin_reminders",to=settings.AUTH_USER_MODEL))],options={"ordering":["remind_on","id"]})]
