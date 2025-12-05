# InsightFolio-BE

This repository contains a small Flask backend that now includes a Qlib-based scoring workflow. The service exposes authentication endpoints along with a scoring endpoint that can initialize Qlib, train a simple model, and return per-ticker scores.

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

- `QLIB_DATA_PATH` (default: `~/.qlib/qlib_data/cn_data`): Path to your Qlib data provider.
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
