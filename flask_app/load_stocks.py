"""Backfill the market_data table with recent daily OHLCV from Yahoo Finance."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
from sqlalchemy import select, func

from app import create_app, db
from app.models import MarketData, Stock


def _to_utc(dt: datetime) -> datetime:
    """Normalize datetimes to timezone-aware UTC."""
    if dt.tzinfo:
        return dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=timezone.utc)


def _extract_float_shares(ticker: yf.Ticker) -> int | None:
    """Try to pull outstanding share count from fast_info/info."""
    float_shares = None
    fast_info = getattr(ticker, "fast_info", None)
    if fast_info:
        if hasattr(fast_info, "get"):
            float_shares = fast_info.get("sharesOutstanding") or fast_info.get("shares_outstanding")
        else:
            float_shares = (
                getattr(fast_info, "sharesOutstanding", None)
                or getattr(fast_info, "shares_outstanding", None)
            )
    if float_shares is None:
        info = getattr(ticker, "info", {}) or {}
        float_shares = info.get("sharesOutstanding") or info.get("floatShares")
    return float_shares


def _existing_datetimes(symbol: str, start_dt: datetime) -> set[datetime]:
    rows = db.session.execute(
        select(MarketData.datetime).where(
            MarketData.instrument == symbol,
            MarketData.datetime >= start_dt,
        )
    ).scalars()
    return {_to_utc(dt) for dt in rows}


def _build_records(symbol: str, history_df, float_shares: int | None, existing: set[datetime]) -> list[MarketData]:
    new_rows: list[MarketData] = []
    for idx, row in history_df.iterrows():
        dt = _to_utc(idx.to_pydatetime())
        if dt in existing:
            continue
        if any(pd.isna(row[field]) for field in ("Open", "High", "Low", "Close", "Volume")):
            continue

        open_price = float(row["Open"])
        high = float(row["High"])
        low = float(row["Low"])
        close = float(row["Close"])
        volume = int(row["Volume"])

        vwap = (high + low + close) / 3.0
        amount = close * volume
        turnover = (volume / float_shares) if float_shares else None

        new_rows.append(
            MarketData(
                datetime=dt,
                instrument=symbol,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                vwap=vwap,
                amount=amount,
                factor=1.0,
                turnover=turnover,
                float_shares=float_shares,
            )
        )
    return new_rows


def backfill_symbol(symbol: str, start_dt: datetime, end_dt: datetime) -> int:
    ticker = yf.Ticker(symbol)
    history_df = ticker.history(start=start_dt, end=end_dt, interval="1d", auto_adjust=False)
    if history_df.empty:
        return 0

    float_shares = _extract_float_shares(ticker)
    existing = _existing_datetimes(symbol, start_dt)
    records = _build_records(symbol, history_df, float_shares, existing)
    if records:
        db.session.bulk_save_objects(records)
    return len(records)


def load_symbols(symbols, start_dt: datetime, end_dt: datetime) -> int:
    inserted = 0
    for symbol in symbols:
        try:
            added = backfill_symbol(symbol.upper(), start_dt, end_dt)
            db.session.commit()
            inserted += added
            print(f"{symbol.upper()}: inserted {added} rows")
        except Exception as exc:  # pragma: no cover - convenience script
            db.session.rollback()
            print(f"{symbol.upper()}: failed ({exc})")
    return inserted


def load_all_symbols(start_dt: datetime, end_dt: datetime) -> int:
    sparse_symbols = db.session.execute(
        select(MarketData.instrument)
        .group_by(MarketData.instrument)
        .having(func.count(MarketData.instrument) <= 1)
    ).scalars().all()

    if sparse_symbols:
        print(f"Backfilling instruments with <=1 row in market_data: {', '.join(sparse_symbols)}")
        return load_symbols(sparse_symbols, start_dt, end_dt)

    symbols = db.session.execute(select(Stock.symbol)).scalars().all()
    if symbols:
        print("No sparse market_data instruments found; backfilling symbols from stocks table.")
        return load_symbols(symbols, start_dt, end_dt)

    print("No symbols found to backfill.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill the market_data table with 3 weeks of OHLCV from Yahoo Finance."
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        help="Space-separated list of ticker symbols (defaults to all symbols in the stocks table).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Number of days of history to pull (default: 14).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=max(args.days, 1))

    app = create_app(register_routes=False)
    with app.app_context():
        if args.symbols:
            total = load_symbols(args.symbols, start_dt, end_dt)
        else:
            total = load_all_symbols(start_dt, end_dt)
    print(f"Done. Total rows inserted: {total}")


if __name__ == "__main__":
    main()
