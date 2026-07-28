from celery import shared_task


@shared_task(name="market.tasks.fetch_all_market_data")
def fetch_all_market_data():
    from market.services.analyzer import fetch_all

    return fetch_all(use_demo_if_empty=False)


@shared_task(name="market.tasks.run_full_analysis")
def run_full_analysis_task(train_ml: bool = True):
    from market.services.analyzer import run_full_analysis

    return run_full_analysis(train_ml=train_ml)


@shared_task(name="market.tasks.append_daily_bars")
def append_daily_bars():
    """Scheduled job: append/update today's OHLC at 10:05 and 14:05."""
    from market.services.daily_append import run_scheduled_append

    return run_scheduled_append()


@shared_task(name="market.tasks.analyze_and_notify")
def analyze_and_notify():
    result = run_full_analysis_task(train_ml=True)
    from notifications.tasks import send_daily_digest

    send_daily_digest.delay()
    return result
