import os
from collections import Counter

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

import django

django.setup()

from django.utils import timezone

from market.models import MLModelStatus, MLModelVersion, PredictionSnapshot, PriceHistory
from market.services.reliability_settlement import settlement_exclusion_reason

apply_changes = os.environ.get("APPLY_CHANGES") == "1"
reasons = Counter()
affected_ids = []

settled = PredictionSnapshot.objects.filter(
    settlement_status=PredictionSnapshot.SettlementStatus.SETTLED,
    stock__isnull=False,
    target_date__isnull=False,
).select_related("stock")

for snapshot in settled.iterator():
    target_bar = PriceHistory.objects.filter(stock=snapshot.stock, date=snapshot.target_date).first()
    if target_bar is None:
        reason = "target_bar_missing"
    else:
        reason = settlement_exclusion_reason(snapshot, target_bar)
    if not reason:
        continue
    reasons[reason] += 1
    affected_ids.append(snapshot.id)

print({"apply": apply_changes, "settled_scanned": settled.count(), "would_exclude": len(affected_ids), "reasons": dict(reasons)})

if apply_changes and affected_ids:
    PredictionSnapshot.objects.filter(id__in=affected_ids).update(
        settlement_status=PredictionSnapshot.SettlementStatus.EXCLUDED,
        exclusion_reason="historical_price_pair_failed_sanity_check",
        outcome_price=None,
        outcome_return=None,
        outcome_class=None,
        settled_at=timezone.now(),
    )

if apply_changes:
    deactivated = MLModelVersion.objects.filter(is_active=True).update(
        is_active=False,
        status=MLModelStatus.EXPERIMENTAL,
    )
    print({"excluded": len(affected_ids), "active_models_deactivated": deactivated})
