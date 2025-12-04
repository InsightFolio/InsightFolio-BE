import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import pandas as pd
import qlib
from qlib.config import REG_CN, REG_US
from qlib.contrib.data.handler import Alpha158
from qlib.contrib.model.gbdt import LGBModel
from qlib.data import D
from qlib.data.dataset import DatasetH


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


_QLIB_STATE = _QlibState()


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

    # 👇 default to your custom provider
    provider_uri = overrides.get("provider_uri") or os.getenv(
        "QLIB_DATA_PATH", os.path.expanduser("~/.qlib/qlib_data/my_data")
    )

    # Region string; we map it to REG_CN / REG_US in ensure_qlib_initialized
    region = (overrides.get("region") or os.getenv("QLIB_REGION", "US")).upper()

    # Symbols must be present in your custom dataset ("A", "ABC", etc.)
    symbols = _parse_symbols(overrides.get("symbols") or os.getenv("QLIB_SYMBOLS"))

    # Time window
    start_date = overrides.get("start_date") or os.getenv("QLIB_START_DATE", "2024-01-01")
    end_date = overrides.get("end_date") or os.getenv("QLIB_END_DATE", "2024-12-31")
    train_end_date = overrides.get("train_end_date") or os.getenv("QLIB_TRAIN_END_DATE", "2024-10-31")

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


def ensure_qlib_initialized(config: QlibConfig) -> None:
    if _QLIB_STATE.initialized:
        return

    # Map region string -> actual region config
    region_conf = REG_CN if config.region == "CN" else REG_US

    expression_cache = os.path.join(config.cache_dir, "expression")
    dataset_cache = os.path.join(config.cache_dir, "dataset")
    os.makedirs(expression_cache, exist_ok=True)
    os.makedirs(dataset_cache, exist_ok=True)

    qlib.init(
        provider_uri=os.path.expanduser(config.provider_uri),
        region=region_conf,
        redis_port=None,
        expression_cache_dir=expression_cache,
        dataset_cache_dir=dataset_cache,
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
    ensure_qlib_initialized(config)
    symbols = _resolve_symbols(config)
    dataset = build_dataset(config, symbols)
    model = train_model(dataset)
    predictions = model.predict(dataset, segment="test")
    results = format_scores(predictions)
    return {"config": asdict(config), "results": results}


def _resolve_symbols(config: QlibConfig) -> List[str]:
    """
    Use configured symbols when provided; otherwise, discover instruments from the provider.
    """
    if config.symbols:
        return config.symbols

    instruments = D.list_instruments(
        instruments="all",
        start_time=config.start_date,
        end_time=config.end_date,
        freq=config.freq if hasattr(config, "freq") else "day",
        as_list=True,
    )
    if not instruments:
        raise ValueError(
            "No instruments found in provider for the requested date range. "
            "Ensure QLIB_DATA_PATH is correct and the date window matches your data."
        )
    return [str(instr) for instr in instruments]
