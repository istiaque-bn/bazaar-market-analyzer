# Generated manually for the staff-maintained public event calendar.
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("market", "0023_portfoliotransaction_investor_journal")]

    operations = [
        migrations.CreateModel(
            name="MarketEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(choices=[("announcement", "Corporate announcement"), ("ipo", "IPO"), ("record_date", "Record date"), ("agm", "AGM"), ("egm", "EGM"), ("market", "Market event")], default="market", max_length=20)),
                ("title", models.CharField(max_length=240)), ("event_date", models.DateField(db_index=True)),
                ("details", models.TextField(blank=True)), ("source_url", models.URLField(blank=True)), ("is_public", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)), ("updated_at", models.DateTimeField(auto_now=True)),
                ("stock", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="market_events", to="market.stock")),
            ], options={"ordering": ["event_date", "title"]},
        ),
        migrations.AddIndex(model_name="marketevent", index=models.Index(fields=["event_date", "is_public"], name="market_mark_event_d_d762e1_idx")),
    ]
