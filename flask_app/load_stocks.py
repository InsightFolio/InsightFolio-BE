"""Backfill the market_data table with recent daily OHLCV from Yahoo Finance."""
from __future__ import annotations

import argparse
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import yfinance as yf
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app import create_app, db
from app.models import MarketData, Stock
from build_qlib_dataset import build_dataset

DEFAULT_SQLITE_PATH = Path(__file__).resolve().parents[1] / "app.db"
DEFAULT_SQLITE_URL = f"sqlite:///{DEFAULT_SQLITE_PATH}"


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


def _resolve_database_url(database_url_arg: str | None) -> str:
    """Pick a DB URL, defaulting to a local SQLite file to avoid remote writes."""
    if database_url_arg:
        return database_url_arg
    env_override = os.getenv("LOAD_STOCKS_DATABASE_URL")
    if env_override:
        return env_override
    return DEFAULT_SQLITE_URL


@contextmanager
def _session_scope(database_url: str) -> Iterable[Session]:
    """Context manager that yields a plain SQLAlchemy session for the given URL."""
    engine = create_engine(database_url)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _ensure_schema(session: Session) -> None:
    """Create tables in the target database if they do not exist yet."""
    bind = session.get_bind()
    if bind is None:
        return
    db.Model.metadata.create_all(bind=bind)


def _existing_datetimes(symbol: str, start_dt: datetime, session: Session | None = None) -> set[datetime]:
    session = session or db.session
    rows = session.execute(
        select(MarketData.datetime).where(
            MarketData.instrument == symbol,
            MarketData.datetime >= start_dt,
        )
    ).scalars()
    return {_to_utc(dt) for dt in rows}


def _fetch_stock_symbols(database_url: str) -> list[str]:
    """Read distinct stock symbols from the given database URL."""
    with _session_scope(database_url) as session:
        symbols = [sym for sym in session.execute(select(Stock.symbol)).scalars() if sym]
    return sorted({sym.upper() for sym in symbols})


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


def backfill_symbol(symbol: str, start_dt: datetime, end_dt: datetime, session: Session | None = None) -> int:
    session = session or db.session
    ticker = yf.Ticker(symbol)
    history_df = ticker.history(start=start_dt, end=end_dt, interval="1d", auto_adjust=False)
    if history_df.empty:
        return 0

    float_shares = _extract_float_shares(ticker)
    existing = _existing_datetimes(symbol, start_dt, session=session)
    records = _build_records(symbol, history_df, float_shares, existing)
    if records:
        session.bulk_save_objects(records)
    return len(records)


def load_symbols(symbols, start_dt: datetime, end_dt: datetime, session: Session | None = None) -> int:
    session = session or db.session
    inserted = 0
    for symbol in symbols:
        try:
            added = backfill_symbol(symbol.upper(), start_dt, end_dt, session=session)
            session.commit()
            inserted += added
            print(f"{symbol.upper()}: inserted {added} rows")
        except Exception as exc:  # pragma: no cover - convenience script
            session.rollback()
            print(f"{symbol.upper()}: failed ({exc})")
    return inserted


def load_all_symbols(start_dt: datetime, end_dt: datetime, session: Session | None = None) -> int:
    session = session or db.session
    market_symbols = {
        sym
        for sym in session.execute(select(MarketData.instrument).distinct()).scalars()
        if sym
    }
    stock_symbols = {sym for sym in session.execute(select(Stock.symbol)).scalars() if sym}
    symbols = sorted(market_symbols | stock_symbols)

    if not symbols:
        print("No symbols found to backfill.")
        return 0

    print(
        f"Backfilling {len(symbols)} instruments from market_data/stocks "
        "without excluding those that already have data."
    )
    return load_symbols(symbols, start_dt, end_dt, session=session)


def sync_remote_symbols_to_local(
    source_database_url: str | None,
    target_database_url: str = DEFAULT_SQLITE_URL,
    days: int = 365,
    provider_uri: str | Path | None = None,
    region: str | None = None,
    cache_dir: str | Path | None = None,
    skip_clean: bool = False,
    symbols: list[str] | None = None,
) -> dict:
    """
    Pull stock symbols (either provided or from a remote DB), backfill market_data into a local DB,
    and rebuild Qlib data.
    """
    target_database_url = str(target_database_url)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=max(days, 1))

    symbols_list = symbols or []
    symbols_list = sorted({s.upper() for s in symbols_list if s})

    symbol_source = "provided"
    if not symbols_list:
        if not source_database_url:
            raise ValueError("source_database_url is required when symbols are not provided")
        source_database_url = str(source_database_url)
        symbols_list = _fetch_stock_symbols(source_database_url)
        symbol_source = "database"

    if not symbols_list:
        return {
            "symbols": [],
            "symbols_checked": 0,
            "rows_inserted": 0,
            "dataset": None,
            "message": "No symbols found",
            "symbol_source": symbol_source,
        }

    with _session_scope(target_database_url) as session:
        _ensure_schema(session)
        rows_inserted = load_symbols(symbols_list, start_dt, end_dt, session=session)

    dataset_info = build_dataset(
        provider_uri=provider_uri,
        region=region,
        cache_dir=cache_dir,
        skip_clean=skip_clean,
        database_url=target_database_url,
    )

    return {
        "symbols": symbols_list,
        "symbols_checked": len(symbols_list),
        "rows_inserted": rows_inserted,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "dataset": dataset_info,
        "symbol_source": symbol_source,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill the market_data table with daily OHLCV from Yahoo Finance."
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
        help="Number of days of history to pull (default: 14; set to 365 for roughly 1 year).",
    )
    parser.add_argument(
        "--database-url",
        help=(
            f"Override the database URL (defaults to local SQLite at {DEFAULT_SQLITE_PATH}). "
            "Also reads LOAD_STOCKS_DATABASE_URL."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=max(args.days, 1))
    database_url = _resolve_database_url(args.database_url)

    app = create_app(register_routes=False, database_url=database_url)
    with app.app_context():
        db.create_all()
        if args.symbols:
            total = load_symbols(args.symbols, start_dt, end_dt)
        else:
            total = load_all_symbols(start_dt, end_dt)
    print(f"Done. Total rows inserted: {total}")
    target_db = database_url if database_url.startswith("sqlite") else "configured database URL"
    print(f"Data written to: {target_db}")


if __name__ == "__main__":
    main()
