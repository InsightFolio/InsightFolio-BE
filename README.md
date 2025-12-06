# InsightFolio-BE

This repository contains a small Flask backend that now includes a Qlib-based scoring workflow. The service exposes authentication endpoints along with a scoring endpoint that can initialize Qlib, train a simple model, and return per-ticker scores.

## Quick start

1. Install deps  
   `pip install -r requirements.txt`

2. Configure environment (below are some examples):

```
export DATABASE_URL=sqlite:///app.db
export JWT_SECRET_KEY=dev-secret
# Optional Qlib overrides
export QLIB_DATA_PATH=~/.qlib/qlib_data/cn_data
export QLIB_REGION=US
```

3. Create tables and run the dev server

```
cd flask_app
python run.py
```

## Creating datasets and seeding data

- **Add stocks**: POST to `/stocks` or call `upsert_stock` from `app/services.py`.
- **Backfill + rebuild via API** (preferred when running the server):  
  POST `/load-stocks` to pull symbols from your remote DB, fetch ~1 year of OHLCV from Yahoo, write to the local DB, and rebuild the Qlib dataset (old dataset is cleared unless `skip_clean` is true).
  ```bash
  curl -X POST http://localhost:5000/load-stocks \
    -H "Content-Type: application/json" \
    -d '{
          "source_database_url": "mysql+pymysql://user:pass@host:port/db",  # defaults to env DATABASE_URL
          "target_database_url": "sqlite:///path/to/app.db",                # defaults to local app.db
          "days": 365,
          "provider_uri": "flask_app/qlib_data/my_data",                    # defaults to QLIB_DATA_PATH
          "region": "US",
          "cache_dir": "/tmp/qlib_cache",
          "skip_clean": false
        }'
  ```
  Response includes symbols checked, rows inserted, and dataset summary.
- **Backfill via script** (manual runs):  
  - All symbols already present in `market_data`/`stocks`:  
    `cd flask_app && python load_stocks.py`
  - Specific tickers:  
    `cd flask_app && python load_stocks.py --symbols AAPL MSFT --days 21`  
    Defaults to local `app.db`; override with `--database-url` or `LOAD_STOCKS_DATABASE_URL`.
- **Build a Qlib dataset from `market_data`** (writes to `QLIB_DATA_PATH`):  
  1) Ensure `DATABASE_URL` and `JWT_SECRET_KEY` are exported (or present in `.env`).  
  2) Run: `cd flask_app && python build_qlib_dataset.py --region US --cache-dir /tmp/qlib_cache`  
     - Override target: `--provider-uri flask_app/qlib_data/my_data` (default comes from `QLIB_DATA_PATH`).  
     - Preserve existing artifacts: add `--skip-clean`.  
  3) After the build, point scoring to the new data by setting `QLIB_DATA_PATH` to the provider dir.
- **Seed sample user/stock/transaction** (dev only):  
  `cd flask_app && python -c "from app.seed import seed_database; seed_database()"`

## Scoring API (Qlib)

Endpoint: `POST /score`  
Body: optional `config` overrides. Example:

```json
{
  "config": {
    "symbols": ["AAPL", "MSFT"],
    "region": "US",
    "start_date": "2019-01-01",
    "end_date": "2020-12-31",
    "train_end_date": "2018-12-31"
  }
}
```

The service initializes Qlib, trains a lightweight model, and returns per-symbol scores with timestamps.

## Other useful endpoints

- Auth: `/signup`, `/login`
- Stocks: `/stocks` (POST), `/stocks/<symbol>` (GET), `/search` (POST), `/popular` (GET), `/stockbyscore` (GET)
- Market data: `/market-data/<symbol>` (GET latest), `/market-data/<symbol>/populate` (POST today’s OHLCV)
- Transactions (JWT required): `/transactions` (GET/POST), `/transactions/execute` (POST)
- Accounts/Holdings (JWT required): `/accounts` (GET/POST/PUT), `/holdings` (GET/POST/DELETE)

## Running tests

From repo root:  
`pytest`

## Project structure

- `flask_app/app/__init__.py`: Flask application factory and extension wiring.
- `flask_app/app/routes.py`: Blueprint with authentication and scoring endpoints.
- `flask_app/app/scoring.py`: Qlib helpers for configuration, dataset setup, model training, and score formatting.
- `flask_app/app/models.py`: SQLAlchemy models.
- `flask_app/run.py`: App entrypoint.
- `flask_app/tests/`: Pytest suite covering scoring utilities and the scoring endpoint.

## Qlib scoring workflow

The scoring workflow uses Qlib's `Alpha158` data handler and a LightGBM model to predict next-period returns and format per-symbol scores. Configuration can be provided with environment variables or via the `/score` endpoint payload.

### Environment variables

- `QLIB_DATA_PATH` (default: `~/.qlib/qlib_data/cn_data`): Path to your Qlib data provider (set this to the directory you pass to `build_qlib_dataset.py`).
- `QLIB_REGION` (default: `CN`): Region passed to `qlib.init` (`CN` or `US`).
- `QLIB_SYMBOLS` (default: `AAPL,MSFT,GOOG`): Universe of tickers.
- `QLIB_START_DATE` (default: `2010-01-01`): Start date for the dataset.
- `QLIB_END_DATE` (default: `2020-12-31`): End date for the dataset and test segment.
- `QLIB_TRAIN_END_DATE` (default: `2018-12-31`): Train/validation split boundary.
- `QLIB_CACHE_DIR` (default: `/tmp/qlib_cache`): Local cache for expression and dataset caches.

### Running locally

1. Create and activate a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Export required app configuration:

   ```bash
   export DATABASE_URL=sqlite:///app.db
   export JWT_SECRET_KEY=dev-secret
   # Optional Qlib overrides
   export QLIB_DATA_PATH=~/.qlib/qlib_data/cn_data
   ```

4. Start the Flask development server:

   ```bash
   cd flask_app
   python run.py
   ```

### Triggering a score run

Send a POST request to `/score` with optional config overrides:

```bash
curl -X POST http://localhost:5000/score \
  -H "Content-Type: application/json" \
  -d '{
        "config": {
          "symbols": ["AAPL", "MSFT"],
          "region": "US",
          "start_date": "2019-01-01",
          "end_date": "2020-12-31"
        }
      }'
```

The response includes the configuration used and a list of per-symbol scores with timestamps:

```json
{
  "config": {"symbols": ["AAPL", "MSFT"], ...},
  "results": [
    {"symbol": "AAPL", "score": 0.12, "timestamp": "2020-12-31T00:00:00"}
  ]
}
```

### Testing

Run the pytest suite from the repository root:

```bash
pytest
```

Tests mock Qlib components so they can execute without downloading market data.
