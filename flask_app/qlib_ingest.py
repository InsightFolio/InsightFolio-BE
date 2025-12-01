"""
Minimal Yahoo Finance -> Qlib provider converter.

Input
-----
- CSV files in a directory (default: data/yahoo) with columns:
  Date, Open, High, Low, Close, Volume[, Adj Close]
  Filenames become symbols (e.g., AAPL.csv -> AAPL).

Output
------
Creates a Qlib provider directory with:
  calendars/day.txt
  instruments/custom.txt
  features/<SYMBOL>.parquet

Usage
-----
python -m flask_app.qlib_ingest \
  --input-dir data/yahoo \
  --output-dir /tmp/qlib_data/custom \
  --region US

You can also set env vars:
  YAHOO_CSV_DIR, QLIB_PROVIDER_URI_OUTPUT, QLIB_REGION_OVERRIDE
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

import numpy as np
import pandas as pd


REQUIRED_COLS = {"date", "open", "high", "low", "close", "volume"}


@dataclass
class InstrumentMeta:
    symbol: str
    start: str
    end: str


def _ensure_columns(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    df = df.rename(columns={col: col.lower() for col in df.columns})
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"{symbol}: missing columns {sorted(missing)}; found {list(df.columns)}")
    df = df[["date", "open", "high", "low", "close", "volume"]].copy()
    df["datetime"] = pd.to_datetime(df["date"], utc=True).dt.normalize()
    df = df.dropna(subset=["datetime", "open", "high", "low", "close"])
    df = df.sort_values("datetime").drop_duplicates(subset=["datetime"])
    df["$open"] = df["open"].astype(float)
    df["$high"] = df["high"].astype(float)
    df["$low"] = df["low"].astype(float)
    df["$close"] = df["close"].astype(float)
    df["$volume"] = df["volume"].fillna(0).astype(float)
    return df[["datetime", "$open", "$high", "$low", "$close", "$volume"]]


def _write_calendar(dates: Iterable[pd.Timestamp], calendar_path: Path) -> List[pd.Timestamp]:
    calendar_path.parent.mkdir(parents=True, exist_ok=True)
    calendar = sorted({d.normalize() for d in dates})
    lines = [d.strftime("%Y-%m-%d") for d in calendar]
    calendar_path.write_text("\n".join(lines))
    return calendar


def _write_instruments(instruments: List[InstrumentMeta], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{inst.symbol}\t{inst.start}\t{inst.end}" for inst in instruments]
    path.write_text("\n".join(lines))


def _write_feature_bin(values: np.ndarray, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # prepend start index (0) as per FileFeatureStorage encoding
    payload = np.concatenate(([0], values.astype("<f4")))
    payload.tofile(out_path)


def _build_feature_arrays(df: pd.DataFrame, calendar_index: dict[int, int], calendar_len: int) -> dict:
    arrays = {field: np.full(calendar_len, np.nan, dtype=np.float32) for field in ["$open", "$high", "$low", "$close", "$volume"]}
    for row in df.itertuples(index=False):
        idx = calendar_index[row.datetime]
        arrays["$open"][idx] = row._1  # $open
        arrays["$high"][idx] = row._2
        arrays["$low"][idx] = row._3
        arrays["$close"][idx] = row._4
        arrays["$volume"][idx] = row._5
    return arrays


def _write_features(df: pd.DataFrame, symbol: str, features_dir: Path, calendar_index: dict, calendar_len: int) -> InstrumentMeta:
    arrays = _build_feature_arrays(df, calendar_index, calendar_len)
    for field, values in arrays.items():
        out_path = features_dir / symbol.lower() / f"{field.lower()}.day.bin"
        _write_feature_bin(values, out_path)
    start = df["datetime"].min().strftime("%Y-%m-%d")
    end = df["datetime"].max().strftime("%Y-%m-%d")
    return InstrumentMeta(symbol=symbol, start=start, end=end)


def ingest(input_dir: Path, output_dir: Path, region: str) -> None:
    csv_files = sorted(input_dir.glob("*.csv"))
    if not csv_files:
        raise SystemExit(f"No CSV files found in {input_dir}")

    parsed = {}
    for csv_file in csv_files:
        symbol = csv_file.stem.upper()
        df = pd.read_csv(csv_file)
        parsed[symbol] = _ensure_columns(df, symbol)

    # Build calendar from all symbols
    all_dates: List[pd.Timestamp] = []
    for df in parsed.values():
        all_dates.extend(df["datetime"].tolist())
    calendar = _write_calendar(all_dates, output_dir / "calendars" / "day.txt")
    calendar_index = {dt: i for i, dt in enumerate(calendar)}

    instruments: List[InstrumentMeta] = []
    features_dir = output_dir / "features"
    for symbol, df in parsed.items():
        inst_meta = _write_features(df, symbol, features_dir, calendar_index, len(calendar))
        instruments.append(inst_meta)
        print(f"✓ wrote {symbol} ({len(df)} rows) -> {features_dir / symbol.lower()}")

    _write_instruments(instruments, output_dir / "instruments" / f"{region.lower()}.txt")

    # Prepare empty cache directories so the app can write to them later.
    (output_dir / "expression").mkdir(parents=True, exist_ok=True)
    (output_dir / "dataset").mkdir(parents=True, exist_ok=True)

    print("\nDone.")
    print(f"Provider root: {output_dir}")
    print(f"Calendar: {output_dir / 'calendars/day.txt'}")
    print(f"Instruments: {output_dir / 'instruments' / f'{region.lower()}.txt'}")
    print(f"Features: {features_dir}")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Yahoo CSVs to a Qlib provider directory.")
    parser.add_argument("--input-dir", default=os.getenv("YAHOO_CSV_DIR", "data/yahoo"), type=Path)
    parser.add_argument(
        "--output-dir",
        default=os.getenv("QLIB_PROVIDER_URI_OUTPUT", "./qlib_data/custom"),
        type=Path,
    )
    parser.add_argument("--region", default=os.getenv("QLIB_REGION_OVERRIDE", "US"))
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    ingest(args.input_dir, args.output_dir, args.region.upper())


if __name__ == "__main__":
    main()
