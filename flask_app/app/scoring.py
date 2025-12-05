import os
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import pandas as pd
import qlib
from flask import current_app, has_app_context
from qlib.config import REG_CN, REG_US
from qlib.contrib.data.handler import Alpha158
from qlib.contrib.model.gbdt import LGBModel
from qlib.data import D
from qlib.data.dataset import DatasetH
from sqlalchemy import func

from . import db
from .models import MarketData


@dataclass
class QlibConfig:
    """Configuration used to drive the Qlib scoring workflow."""

    provider_uri: str
    region: str          # "CN" or "US" (mapped to REG_CN / REG_US)
    symbols: Optional[List[str]]   # list of instruments to score; if empty we auto-discover from provider
    start_date: str
    end_date: str
    train_end_date: str
    cache_dir: str


class _QlibState:
    def __init__(self) -> None:
        self.initialized: bool = False
        self.dataset_ready: bool = False


_QLIB_STATE = _QlibState()


def _provider_has_data(provider_uri: str, freq: str = "day") -> bool:
    """
    Light check to see if the provider directory looks populated.
    """
    base = Path(os.path.expanduser(provider_uri))
    calendar = base / "calendars" / f"{freq}.txt"
    instruments = base / "instruments" / "all.txt"
    features_dir = base / "features"
    if not calendar.exists() or not instruments.exists() or not features_dir.exists():
        return False
    return any(features_dir.rglob("*.bin"))


def ensure_dataset_available(config: QlibConfig) -> None:
    """
    If the provider is empty, build a fresh dataset on the fly so scoring can run automatically.
    """
    if _QLIB_STATE.dataset_ready or _provider_has_data(config.provider_uri):
        _QLIB_STATE.dataset_ready = True
        return

    try:
        from flask_app import build_qlib_dataset as dataset_builder
    except ImportError:  # pragma: no cover - runtime safety
        import build_qlib_dataset as dataset_builder  # type: ignore

    dataset_builder.build_dataset(
        provider_uri=config.provider_uri,
        freq="day",
        region=config.region,
        cache_dir=config.cache_dir,
        skip_clean=False,
        database_url=os.getenv("DATABASE_URL"),
    )
    _QLIB_STATE.dataset_ready = True


def _parse_symbols(raw_symbols: Optional[Union[str, List[str]]]) -> List[str]:
    """
    Accept:
      - None       -> empty list (will trigger auto-discovery from provider)
      - "A,B,C"    -> ["A", "B", "C"]
      - ["A", "B"] -> ["A", "B"]
    """
    if raw_symbols is None:
        return []
    if isinstance(raw_symbols, str):
        return [symbol.strip() for symbol in raw_symbols.split(",") if symbol.strip()]
    return raw_symbols


def _normalize_dates(config: QlibConfig) -> tuple[datetime.date, datetime.date, datetime.date]:
    start = pd.to_datetime(config.start_date).date()
    end = pd.to_datetime(config.end_date).date()
    train_end = pd.to_datetime(config.train_end_date).date()

    if end < start:
        raise ValueError("QLIB_END_DATE must be on or after QLIB_START_DATE.")

    # Keep the train/valid split inside the available window.
    train_end = min(train_end, end)
    if train_end < start:
        train_end = start

    return start, end, train_end


def load_config_from_env(overrides: Optional[Dict[str, Any]] = None) -> QlibConfig:
    overrides = overrides or {}

    provider_uri = overrides.get("provider_uri") or "flask_app/qlib_data/my_data"

    # Region string; we map it to REG_CN / REG_US in ensure_qlib_initialized
    region = (overrides.get("region") or "US").upper()

    # Symbols must be present in your custom dataset ("A", "ABC", etc.)
    symbols = _parse_symbols(overrides.get("symbols") or os.getenv("QLIB_SYMBOLS"))

    # Time window defaults derive from the DB when available.
    db_dates = _date_defaults_from_db()
    start_default, end_default, train_end_default = (
        db_dates if db_dates else ("2024-01-01", "2024-12-31", "2024-10-31")
    )

    start_date = overrides.get("start_date") or start_default
    end_date = overrides.get("end_date") or end_default
    train_end_date = overrides.get("train_end_date") or train_end_default

    # Local cache for Qlib’s expression/dataset caches
    cache_dir = overrides.get("cache_dir") or os.getenv("QLIB_CACHE_DIR", "/tmp/qlib_cache")

    return QlibConfig(
        provider_uri=provider_uri,
        region=region,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        train_end_date=train_end_date,
        cache_dir=cache_dir,
    )


def _date_defaults_from_db() -> Optional[Tuple[str, str, str]]:
    """
    Pull start/end/train_end defaults from the market_data table.
    Returns ISO date strings or None when unavailable.
    """
    if not has_app_context():
        return None
    try:
        min_dt, max_dt = db.session.query(
            func.min(MarketData.datetime), func.max(MarketData.datetime)
        ).one()
    except Exception:
        if current_app and current_app.logger:  # pragma: no cover - defensive guard
            current_app.logger.exception("Failed to derive date defaults from market_data")
        return None

    if not min_dt or not max_dt:
        return None

    start_date = min_dt.date()
    end_date = max_dt.date()
    train_end_date = max(start_date, end_date - timedelta(days=20))

    return start_date.isoformat(), end_date.isoformat(), train_end_date.isoformat()


def ensure_qlib_initialized(config: QlibConfig) -> None:
    if _QLIB_STATE.initialized:
        return

    # Map region string -> actual region config
    region_conf = REG_CN if config.region == "CN" else REG_US

    expression_cache = os.path.join(config.cache_dir, "expression")
    dataset_cache = os.path.join(config.cache_dir, "dataset")
    os.makedirs(expression_cache, exist_ok=True)
    os.makedirs(dataset_cache, exist_ok=True)

    _disable_qlib_recorder()

    provider_uri_abs = str(Path(config.provider_uri).expanduser().resolve())
    config.provider_uri = provider_uri_abs  # normalize so downstream uses an absolute path

    qlib.init(
        provider_uri=provider_uri_abs,
        region=region_conf,
        redis_port=None,
        expression_cache_dir=expression_cache,
        dataset_cache_dir=dataset_cache,
        custom_conf={"exp_manager": None},  # disable MLflow/recorder to avoid hangs
    )
    _QLIB_STATE.initialized = True


def build_dataset(config: QlibConfig, symbols: List[str]) -> DatasetH:
    """
    Build an Alpha158-based dataset over your chosen instruments.
    Assumes your provider has at least OHLCV (which you do).
    """
    start_dt, end_dt, train_end_dt = _normalize_dates(config)
    start_str, end_str, train_end_str = (
        start_dt.isoformat(),
        end_dt.isoformat(),
        train_end_dt.isoformat(),
    )

    handler = Alpha158(
        instruments=symbols,
        start_time=start_str,
        end_time=end_str,
        fit_start_time=start_str,
        fit_end_time=train_end_str,
    )

    segments = {
        "train": (start_str, train_end_str),
        "valid": (train_end_str, end_str),
        "test": (train_end_str, end_str),
    }
    return DatasetH(handler=handler, segments=segments)


def train_model(dataset: DatasetH) -> LGBModel:
    try:
        model = LGBModel(loss="mse", verbose=-1)
    except NotImplementedError as exc:
        # LightGBM backend missing; provide a clear message for the API surface.
        raise RuntimeError(
            "LightGBM is not available. Install lightgbm (and libomp on macOS) to enable the scorer."
        ) from exc
    # Suppress Qlib recorder metrics to avoid requiring R/C registration.
    if hasattr(model, "log_metrics"):
        model.log_metrics = lambda *args, **kwargs: None
    if hasattr(model, "_log_metrics"):
        model._log_metrics = lambda *args, **kwargs: None
    model.fit(dataset)
    return model


def _extract_predictions(
    predictions: Union[pd.Series, pd.DataFrame]
) -> Iterable[Tuple[Union[str, Tuple[str, datetime]], float]]:
    if isinstance(predictions, pd.Series):
        return predictions.items()
    if isinstance(predictions, pd.DataFrame):
        first_col = predictions.columns[0]
        return predictions[first_col].items()
    raise TypeError("Predictions must be a pandas Series or DataFrame")


def _serialize_timestamp(value: Any) -> str:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def format_scores(
    predictions: Union[pd.Series, pd.DataFrame]
) -> List[Dict[str, Union[str, float]]]:
    scores: List[Dict[str, Union[str, float]]] = []
    for index, score in _extract_predictions(predictions):
        if isinstance(index, tuple) and len(index) >= 2:
            first, second = index[0], index[1]
            # Qlib sometimes returns (timestamp, symbol) instead of (symbol, timestamp).
            if isinstance(first, (pd.Timestamp, datetime)):
                timestamp, symbol = first, second
            elif isinstance(second, (pd.Timestamp, datetime)):
                symbol, timestamp = first, second
            else:
                symbol, timestamp = first, second
        else:
            symbol, timestamp = index, datetime.utcnow()
        scores.append(
            {
                "symbol": str(symbol),
                "score": float(score),
                "timestamp": _serialize_timestamp(timestamp),
            }
        )
    return scores


def run_scoring_workflow(config: Optional[QlibConfig] = None) -> Dict[str, Any]:
    config = config or load_config_from_env()
    debug_log: list[str] = []
    def log(msg: str) -> None:
        debug_log.append(msg)
        print(msg, flush=True)

    log("run_scoring_workflow: ensure dataset")
    ensure_dataset_available(config)
    log("run_scoring_workflow: ensure qlib init")
    ensure_qlib_initialized(config)
    symbols = _resolve_symbols(config)
    # Cap auto-discovered symbols to keep runs fast unless user explicitly requested symbols.
    if not config.symbols and len(symbols) > 50:
        log(f"run_scoring_workflow: trimming symbols from {len(symbols)} to 50 for speed")
        symbols = symbols[:50]
    log(f"run_scoring_workflow: symbols resolved ({len(symbols)})")
    dataset = build_dataset(config, symbols)
    log("run_scoring_workflow: dataset built")
    model = train_model(dataset)
    log("run_scoring_workflow: predicting on test segment")
    predict_start = time.perf_counter()
    predictions = model.predict(dataset, segment="test")
    predict_ms = (time.perf_counter() - predict_start) * 1000
    log(f"run_scoring_workflow: prediction finished in {predict_ms:.1f} ms; rows={_prediction_len(predictions)}")
    # Trim extremely large prediction outputs to keep formatting fast.
    predictions_trimmed = _trim_predictions(predictions, limit=1000, log=log)
    log("run_scoring_workflow: formatting scores")
    results = format_scores(predictions_trimmed)
    log(f"run_scoring_workflow: formatted {len(results)} scores")
    return {"config": asdict(config), "results": results, "log": debug_log}


def _resolve_symbols(config: QlibConfig) -> List[str]:
    """
    Use configured symbols when provided; otherwise, discover instruments from the provider.
    """
    if config.symbols:
        return config.symbols

    instruments: List[str] | None = None
    try:
        instruments = D.list_instruments(
            instruments="all",
            start_time=config.start_date,
            end_time=config.end_date,
            freq=getattr(config, "freq", "day"),
            as_list=True,
        )
    except Exception as exc:
        if current_app and current_app.logger:  # pragma: no cover - defensive guard
            current_app.logger.exception("Failed to list instruments from provider", extra={"error": str(exc)})

    if instruments:
        return [str(instr) for instr in instruments]

    fallback = _fallback_symbols_from_db(config)
    if fallback:
        return fallback

    raise ValueError(
        "No instruments found in provider or database for the requested date range. "
        "Ensure the Qlib dataset exists or pass symbols explicitly."
    )


def _fallback_symbols_from_db(config: QlibConfig) -> List[str]:
    """As a fallback, derive symbols from market_data within the requested window."""
    if not has_app_context():
        return []
    try:
        start_dt = pd.to_datetime(config.start_date)
        end_dt = pd.to_datetime(config.end_date) + pd.Timedelta(days=1)
        rows = (
            db.session.query(MarketData.instrument)
            .filter(MarketData.datetime >= start_dt, MarketData.datetime < end_dt)
            .distinct()
            .all()
        )
        return sorted({str(row[0]).upper() for row in rows if row and row[0]})
    except Exception:
        if current_app and current_app.logger:  # pragma: no cover - defensive guard
            current_app.logger.exception("Fallback symbol discovery from DB failed")
        return []


def _disable_qlib_recorder() -> None:
    """
    Patch Qlib's recorder registration to a no-op to avoid MLflow hangs.
    """
    try:
        from qlib.workflow import R
        from qlib.config import C
        import qlib.workflow.utils as wf_utils
        C.register = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        R.register = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        R.end_exp = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        R.log_metrics = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        wf_utils.experiment_exit_handler = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    except Exception:
        pass


def _log_info(message: str) -> None:
    if has_app_context() and current_app:
        try:
            current_app.logger.info(message)
            return
        except Exception:
            pass
    print(message, flush=True)


def _prediction_len(predictions: Union[pd.Series, pd.DataFrame]) -> int:
    if isinstance(predictions, pd.Series):
        return len(predictions)
    if isinstance(predictions, pd.DataFrame):
        return len(predictions)
    return 0


def _trim_predictions(predictions: Union[pd.Series, pd.DataFrame], limit: int, log) -> Union[pd.Series, pd.DataFrame]:
    """Limit prediction output size to avoid long formatting/serialization."""
    try:
        if isinstance(predictions, pd.Series) and len(predictions) > limit:
            log(f"run_scoring_workflow: trimming predictions Series from {len(predictions)} to {limit}")
            return predictions.head(limit)
        if isinstance(predictions, pd.DataFrame) and len(predictions) > limit:
            log(f"run_scoring_workflow: trimming predictions DataFrame from {len(predictions)} to {limit}")
            return predictions.head(limit)
    except Exception:
        pass
    return predictions
