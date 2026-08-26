from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("market", "0028_alter_marketevent_event_time"),
    ]

    operations = [
        migrations.AddField(
            model_name="predictionsnapshot",
            name="candidate_model_version_tag",
            field=models.CharField(
                blank=True,
                help_text="Next-close candidate version considered at forecast time, even if a safer method was served.",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="predictionsnapshot",
            name="served_method",
            field=models.CharField(
                default="unknown",
                help_text="Immutable method actually delivered: ml_blended / analogue / naive_fallback / unknown.",
                max_length=32,
            ),
        ),
    ]
