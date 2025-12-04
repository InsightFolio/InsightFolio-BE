"""Build a fresh Qlib dataset from the market_data table."""
from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import select

from app import create_app, db
from app.models import MarketData
import qlib
from qlib.config import REG_CN, REG_US
from qlib.data.storage.file_storage import FileCalendarStorage, FileFeatureStorage, FileInstrumentStorage

FIELD_MAP = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "vwap": "vwap",
    "amount": "amount",
    "factor": "factor",
    "turnover": "turnover",
    "float_shares": "floatshares",
}
DEFAULT_FREQ = "day"


def _init_qlib(provider_uri: Dict[str, str], region: str, cache_dir: Path | str) -> None:
    cache_dir = Path(cache_dir).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    qlib.init(
        provider_uri=provider_uri,
        region=REG_CN if region.upper() == "CN" else REG_US,
        redis_port=None,
        expression_cache_dir=str(cache_dir / "expression"),
        dataset_cache_dir=str(cache_dir / "dataset"),
    )


def _normalize_provider_uri(
    provider_uri: str | Path | Dict[str, str] | None,
    freq: str,
) -> Tuple[Dict[str, str], Path]:
    """
    Ensure provider URI is a mapping and return the primary directory for the given freq.
    """
    if isinstance(provider_uri, dict):
        normalized = {k: str(Path(v).expanduser().resolve()) for k, v in provider_uri.items()}
        if not normalized:
            raise ValueError("provider_uri mapping is empty")
        primary_dir = Path(normalized.get(freq) or next(iter(normalized.values())))
    else:
        primary_dir = Path(provider_uri or os.getenv("QLIB_DATA_PATH", Path("flask_app/qlib_data/my_data")))
        primary_dir = primary_dir.expanduser().resolve()
        normalized = {freq: str(primary_dir)}

    primary_dir.mkdir(parents=True, exist_ok=True)
    return normalized, primary_dir


def _reset_provider_dir(base: Path) -> None:
    """Clear old Qlib artifacts while preserving the provider root."""
    for name in ("calendars", "features", "instruments", "features_cache", "dataset_cache"):
        path = base / name
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True, exist_ok=True)


def _load_market_data() -> pd.DataFrame:
    query = select(
        MarketData.instrument,
        MarketData.datetime,
        MarketData.open,
        MarketData.high,
        MarketData.low,
        MarketData.close,
        MarketData.volume,
        MarketData.vwap,
        MarketData.amount,
        MarketData.factor,
        MarketData.turnover,
        MarketData.float_shares.label("float_shares"),
    ).order_by(MarketData.instrument, MarketData.datetime)

    rows = db.session.execute(query).all()
    if not rows:
        raise SystemExit("No rows found in market_data; aborting dataset build.")

    df = pd.DataFrame(
        rows,
        columns=[
            "instrument",
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "vwap",
            "amount",
            "factor",
            "turnover",
            "float_shares",
        ],
    )
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df["date"] = df["datetime"].dt.date
    df["instrument"] = df["instrument"].str.upper()
    df.sort_values(["instrument", "datetime"], inplace=True)

    for col in FIELD_MAP:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _write_calendar(calendar: Iterable[str], provider_uri: Dict[str, str], freq: str) -> None:
    cal_storage = FileCalendarStorage(freq=freq, future=False, provider_uri=provider_uri)
    cal_storage.extend(calendar)


def _write_instruments(inst_ranges: pd.DataFrame, provider_uri: Dict[str, str], freq: str) -> None:
    inst_storage = FileInstrumentStorage(market="all", freq=freq, provider_uri=provider_uri)
    payload = {
        inst: [(pd.Timestamp(row["start"]), pd.Timestamp(row["end"]))]
        for inst, row in inst_ranges.iterrows()
    }
    inst_storage.update(payload)


def _write_features(df: pd.DataFrame, calendar: List, provider_uri: Dict[str, str], freq: str) -> None:
    cal_index = {d: idx for idx, d in enumerate(calendar)}

    for inst, inst_df in df.groupby("instrument"):
        # Collapse to one row per date per instrument (latest in the day wins).
        by_date = inst_df.sort_values("datetime").groupby("date").last()

        for src_col, dest_field in FIELD_MAP.items():
            values = np.full(len(calendar), np.nan, dtype=np.float32)
            for date, value in by_date[src_col].items():
                if date not in cal_index:
                    continue
                values[cal_index[date]] = np.nan if pd.isna(value) else float(value)

            valid_idx = np.flatnonzero(~np.isnan(values))
            if valid_idx.size == 0:
                continue
            start, end = int(valid_idx[0]), int(valid_idx[-1])

            storage = FileFeatureStorage(
                instrument=inst,
                field=dest_field,
                freq=freq,
                provider_uri=provider_uri,
            )
            storage.uri.parent.mkdir(parents=True, exist_ok=True)
            storage.write(values[start : end + 1], index=start)


def build_dataset(
    provider_uri: str | Path | Dict[str, str] | None = None,
    freq: str = DEFAULT_FREQ,
    region: str | None = None,
    cache_dir: str | Path | None = None,
    skip_clean: bool = False,
    database_url: str | None = None,
) -> dict:
    """
    Build a Qlib dataset from the market_data table and return a summary dict.
    """
    region_value = (region or os.getenv("QLIB_REGION", "US")).upper()
    provider_uri_map, provider_dir = _normalize_provider_uri(provider_uri, freq=freq)
    cache_dir_value = cache_dir or os.getenv("QLIB_CACHE_DIR", "/tmp/qlib_cache")

    app = create_app(register_routes=False, database_url=database_url)
    with app.app_context():
        df = _load_market_data()
        calendar_dates = sorted(df["date"].unique())
        calendar_strings = [d.isoformat() for d in calendar_dates]
        inst_ranges = df.groupby("instrument")["date"].agg(start="min", end="max")

        _init_qlib(provider_uri_map, region=region_value, cache_dir=cache_dir_value)

        if not skip_clean:
            _reset_provider_dir(provider_dir)
        else:
            for name in ("calendars", "features", "instruments"):
                (provider_dir / name).mkdir(parents=True, exist_ok=True)

        _write_calendar(calendar_strings, provider_uri_map, freq=freq)
        _write_instruments(inst_ranges, provider_uri_map, freq=freq)
        _write_features(df, calendar_dates, provider_uri_map, freq=freq)

    return {
        "provider_dir": str(provider_dir),
        "freq": freq,
        "region": region_value,
        "calendar_days": len(calendar_dates),
        "instruments": len(inst_ranges),
        "start_date": calendar_dates[0].isoformat() if calendar_dates else None,
        "end_date": calendar_dates[-1].isoformat() if calendar_dates else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Qlib dataset from market_data rows.")
    parser.add_argument(
        "--provider-uri",
        default=os.getenv("QLIB_DATA_PATH", Path("flask_app/qlib_data/my_data")),
        help="Target path for the Qlib dataset (defaults to env QLIB_DATA_PATH or repo path).",
    )
    parser.add_argument(
        "--freq",
        default=DEFAULT_FREQ,
        help="Frequency key for the provider directory (default: day).",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("QLIB_REGION", "US"),
        help="Qlib region (CN or US). Defaults to env QLIB_REGION or US.",
    )
    parser.add_argument(
        "--cache-dir",
        default=os.getenv("QLIB_CACHE_DIR", "/tmp/qlib_cache"),
        help="Cache directory for expression/dataset caches (default: env QLIB_CACHE_DIR or /tmp/qlib_cache).",
    )
    parser.add_argument(
        "--skip-clean",
        action="store_true",
        help="Skip deleting existing calendars/features/instruments before writing.",
    )
    parser.add_argument(
        "--database-url",
        help="Database URL to read market_data from (defaults to env DATABASE_URL or local SQLite).",
    )
    args = parser.parse_args()

    result = build_dataset(
        provider_uri=args.provider_uri,
        freq=args.freq,
        region=args.region,
        cache_dir=args.cache_dir,
        skip_clean=args.skip_clean,
        database_url=args.database_url,
    )

    print(
        f"Wrote {result['calendar_days']} trading days for {result['instruments']} "
        f"instruments to {result['provider_dir']}"
    )
    if result["start_date"] and result["end_date"]:
        print(f"Date span: {result['start_date']} -> {result['end_date']}")


if __name__ == "__main__":
    main()
