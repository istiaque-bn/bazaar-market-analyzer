# Bazaar — DSE & CSE Market Analyzer

Django platform that analyses **Dhaka (DSE)** and **Chittagong (CSE)** equities using ~1 year of history, then surfaces:

- Potential shares (ranked scores)
- Pattern detection (RSI, MACD, MA crosses, volume, Bollinger)
- Predictive estimates: *mature in ~X days*, *peak in ~Y days* (historical analogues — **not guarantees**)
- Safe-buy / sell suggestions
- Telegram / email / in-app digests
- Backtests + lightweight ML refinement
- REST API for mobile / integrations

## Quick start

```bash
cd /Users/istiaque/Desktop/Test/Trial
source .venv/bin/activate
cp .env.example .env
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser  # optional
python manage.py run_market_pipeline --all
python manage.py runserver
```

Open http://127.0.0.1:8000/

## Phases covered

| Phase | Status |
|-------|--------|
| 1. MVP: fetch/store ~1y + screener | Done |
| 2. Indicators, patterns, Telegram alerts | Done |
| 3. Backtests + maturity/peak estimates | Done |
| 4. CSE + accounts + dashboard | Done |
| 5. ML blend + REST API | Done |

## Commands

```bash
# Seed synthetic 1y data for 15 symbols × DSE/CSE and analyze
python manage.py run_market_pipeline --demo --analyze

# Try live DSE/CSE fetch (falls back gracefully if offline)
python manage.py run_market_pipeline --fetch --analyze

# Full pipeline
python manage.py run_market_pipeline --all
```

## Celery (optional)

Requires Redis:

```bash
celery -A config worker -l info
celery -A config beat -l info
```

Without Redis, use the dashboard **Seed demo + analyze** buttons or management commands (synchronous).

## API

- `GET /api/screener/` — potential / safe buys / sells
- `GET /api/stocks/`
- `GET /api/stocks/DSE/GP/`
- `GET /api/analysis/`
- `GET /api/backtests/`
- `POST /api/auth/register/` `{username,password,email}`
- `POST /api/auth/login/` `{username,password}` → token
- `GET /api/alerts/` (auth)

## Notifications

Set in `.env`:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

User-level Telegram chat IDs can also be set under **Profile**.

## Disclaimer

Bazaar produces **probabilistic** estimates from historical patterns. It is educational software, not licensed investment advice. Bangladesh market conditions (liquidity, manipulation, regulation) can invalidate any model quickly.
