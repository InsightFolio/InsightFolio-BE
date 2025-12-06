import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from pymongo import MongoClient, UpdateOne, DeleteMany
from sqlalchemy import select, func, create_engine
from sqlalchemy.orm import sessionmaker
import yfinance as yf

# Prefer absolute import when app is run from repo root; fall back to local module when running inside flask_app.
try:
    from flask_app import load_stocks
except ImportError:  # pragma: no cover - runtime safety
    import load_stocks  # type: ignore

from . import db, jwt
from .models import User, Stock, Account, Holding, Transaction, Score, MarketData
from .mongo_models import Stock as MongoStock, MarketData as MongoMarketData
from .scoring import QlibConfig, load_config_from_env, run_scoring_workflow
from .seed import seed_database
from .services import (
    upsert_user,
    upsert_stock,
    add_transaction,
    search_stocks,
    process_transaction,
    create_market_data_from_yahoo,
    calculate_portfolio_history,
)

routes_bp = Blueprint('routes', __name__)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "marketdb")
try:
    _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000) if MONGO_URI else None
    _mongo_db = _mongo_client[MONGO_DB_NAME] if _mongo_client else None
    _mongo_stocks_col = _mongo_db["stocks"] if _mongo_db else None
    _mongo_market_data_col = _mongo_db["marketData"] if _mongo_db else None
    _mongo_scores_col = _mongo_db["scores"] if _mongo_db else None
except Exception:
    _mongo_client = None
    _mongo_db = None
    _mongo_stocks_col = None
    _mongo_market_data_col = None
    _mongo_scores_col = None


def _first_present(data, *keys, default=None):
    """Return the first present key from the payload or the provided default."""
    for key in keys:
        if key in data:
            return data[key]
    return default


def _required(data, *keys, type_=None):
    """
    Fetch a required field, supporting multiple aliases.
    Raises ValueError when missing or of the wrong type.
    """
    sentinel = object()
    value = _first_present(data, *keys, default=sentinel)
    if value is sentinel:
        raise ValueError(f"Missing required field: '{keys[0]}'")
    if type_ is not None and not isinstance(value, type_):
        if type_ is float and isinstance(value, int):
            value = float(value)
        else:
            raise ValueError(f"Field '{keys[0]}' must be of type {type_.__name__}")
    return value


def parse_iso_datetime(value, field_name):
    """
    Accepts ISO 8601 strings or datetime objects and normalizes to UTC-aware datetimes.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00").replace(" ", "T")
        try:
            dt = datetime.fromisoformat(normalized)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    raise ValueError(f"Invalid datetime format for {field_name}: {value!r}")


def _wants_mongo(payload: dict) -> bool:
    backend = (request.args.get("backend") or "").lower()
    if backend == "mongo":
        return True
    return bool(payload.get("use_mongo") or payload.get("useMongo"))


def decimal_to_float(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _score_to_doc(score: Score, symbol: str) -> dict:
    return {
        "scoreId": score.score_id,
        "stockId": score.stock_id,
        "symbol": symbol,
        "score": decimal_to_float(score.score),
        "quantity": int(score.quantity) if score.quantity is not None else None,
        "volatility": decimal_to_float(score.volatility) if score.volatility is not None else None,
        "growth": decimal_to_float(score.growth) if score.growth is not None else None,
        "price1M": decimal_to_float(score.price_1m) if score.price_1m is not None else None,
        "price2M": decimal_to_float(score.price_2m) if score.price_2m is not None else None,
        "price3M": decimal_to_float(score.price_3m) if score.price_3m is not None else None,
        "price4M": decimal_to_float(score.price_4m) if score.price_4m is not None else None,
        "price5M": decimal_to_float(score.price_5m) if score.price_5m is not None else None,
        "price6M": decimal_to_float(score.price_6m) if score.price_6m is not None else None,
        "createdAt": datetime.utcnow(),
    }


def _persist_scores_to_db(
    results: list[dict],
    database_url: str,
    symbols: list[str] | None = None,
) -> dict:
    """
    Store scoring results into the Score table on the given database URL.
    """
    symbol_filter = {s.upper() for s in symbols} if symbols else None
    inserted = 0
    missing_symbols: set[str] = set()
    invalid = 0

    if not results:
        return {
            "inserted": 0,
            "missing_symbols": [],
            "invalid": 0,
            "target_db": database_url,
        }

    with _session_scope_for_sync(database_url) as session:
        for entry in results:
            symbol_raw = entry.get("symbol")
            raw_score = entry.get("score")
            if symbol_raw is None or raw_score is None:
                invalid += 1
                continue

            symbol = str(symbol_raw).upper()
            if symbol_filter and symbol not in symbol_filter:
                continue

            try:
                score_value = Decimal(str(raw_score))
            except (InvalidOperation, TypeError, ValueError):
                invalid += 1
                continue

            stock = session.execute(select(Stock).where(Stock.symbol == symbol)).scalar_one_or_none()
            if stock is None:
                missing_symbols.add(symbol)
                continue

            score_obj = Score(stock_id=stock.stock_id, score=score_value)

            # Optional fields
            if entry.get("quantity") is not None:
                try:
                    score_obj.quantity = int(entry["quantity"])
                except (TypeError, ValueError):
                    pass
            if entry.get("volatility") is not None:
                try:
                    score_obj.volatility = Decimal(str(entry["volatility"]))
                except (InvalidOperation, TypeError, ValueError):
                    pass
            if entry.get("growth") is not None:
                try:
                    score_obj.growth = Decimal(str(entry["growth"]))
                except (InvalidOperation, TypeError, ValueError):
                    pass
            for key, attr in [
                ("price1M", "price_1m"),
                ("price2M", "price_2m"),
                ("price3M", "price_3m"),
                ("price4M", "price_4m"),
                ("price5M", "price_5m"),
                ("price6M", "price_6m"),
            ]:
                if entry.get(key) is not None:
                    try:
                        setattr(score_obj, attr, Decimal(str(entry[key])))
                    except (InvalidOperation, TypeError, ValueError):
                        pass

            session.add(score_obj)
            inserted += 1

        try:
            session.commit()
        except Exception:
            session.rollback()
            raise

    return {
        "inserted": inserted,
        "missing_symbols": sorted(missing_symbols),
        "invalid": invalid,
        "target_db": database_url,
    }


@contextmanager
def _session_scope_for_sync(database_url: str):
    """
    Light-weight session scope with short connect timeout for remote DBs.
    """
    engine_kwargs = {"pool_pre_ping": True}
    if database_url.startswith("mysql"):
        engine_kwargs["connect_args"] = {"connect_timeout": 5}
    engine = create_engine(database_url, **engine_kwargs)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _sync_db_to_mongo(
    stock_db_url: str,
    market_db_url: str,
    score_db_url: str | None = None,
    symbols: list[str] | None = None,
    limit_market: int | None = None,
    skip_stocks: bool = False,
    skip_scores: bool = False,
    skip_market: bool = False,
    since: datetime | None = None,
) -> dict:
    """
    Copy stocks, scores, and market_data from SQL databases into MongoDB collections.
    Stock metadata is read from stock_db_url (remote prod), scores can be read from
    score_db_url (defaults to stock_db_url), and market data is read from market_db_url
    (local backfilled SQLite unless overridden).
    """
    if not skip_stocks and _mongo_stocks_col is None:
        raise RuntimeError("MongoDB not configured; set MONGO_URI")
    if not skip_scores and _mongo_scores_col is None:
        raise RuntimeError("MongoDB not configured; set MONGO_URI")
    if not skip_market and _mongo_market_data_col is None:
        raise RuntimeError("MongoDB not configured; set MONGO_URI")

    synced_stocks = 0
    synced_scores = 0
    synced_market_data = 0
    symbol_filter = {s.upper() for s in symbols} if symbols else None
    score_db_url = score_db_url or stock_db_url

    if not skip_stocks:
        with _session_scope_for_sync(stock_db_url) as session:
            stock_query = select(Stock).execution_options(stream_results=True)
            if symbol_filter:
                stock_query = stock_query.where(Stock.symbol.in_(symbol_filter))
            stock_stream = session.execute(stock_query).scalars().yield_per(500)

            stock_ops: list[UpdateOne] = []
            for stock in stock_stream:
                doc = {
                    "_id": stock.symbol,
                    "stockId": stock.stock_id,
                    "company": stock.company,
                    "sector": stock.sector,
                    "subSector": stock.sub_sector,
                    "country": stock.country,
                    "price": float(stock.price) if stock.price is not None else None,
                    "quantity": int(stock.quantity) if stock.quantity is not None else None,
                    "lastUpdated": stock.last_updated,
                }
                stock_ops.append(UpdateOne({"_id": stock.symbol}, {"$set": doc}, upsert=True))
                synced_stocks += 1
                if len(stock_ops) >= 500:
                    _mongo_stocks_col.bulk_write(stock_ops, ordered=False)
                    stock_ops.clear()
            if stock_ops:
                _mongo_stocks_col.bulk_write(stock_ops, ordered=False)

    if not skip_scores:
        with _session_scope_for_sync(score_db_url) as session:
            avg_subq = (
                select(Score.stock_id, func.avg(Score.score).label("avg_score"))
                .group_by(Score.stock_id)
                .subquery()
            )
            latest_score_subq = (
                select(Score.stock_id, func.max(Score.score_id).label("max_id"))
                .group_by(Score.stock_id)
                .subquery()
            )
            score_query = (
                select(Score, Stock.symbol, avg_subq.c.avg_score)
                .join(latest_score_subq, Score.score_id == latest_score_subq.c.max_id)
                .join(Stock, Score.stock_id == Stock.stock_id)
                .join(avg_subq, Score.stock_id == avg_subq.c.stock_id)
                .execution_options(stream_results=True)
            )
            if symbol_filter:
                score_query = score_query.where(Stock.symbol.in_(symbol_filter))
            score_stream = session.execute(score_query).yield_per(500)

            for score_obj, symbol, avg_score in score_stream:
                if symbol is None:
                    raise ValueError(f"No Stock found for StockID={score_obj.stock_id}")
                doc = _score_to_doc(score_obj, symbol)
                if avg_score is not None:
                    doc["score"] = float(avg_score)
                _mongo_scores_col.delete_many({"stockId": score_obj.stock_id})
                _mongo_scores_col.replace_one(
                    {"stockId": score_obj.stock_id},
                    doc,
                    upsert=True,
                )
                synced_scores += 1

    if not skip_market:
        with _session_scope_for_sync(market_db_url) as session:
            market_query = select(MarketData).execution_options(stream_results=True)
            if symbol_filter:
                market_query = market_query.where(MarketData.instrument.in_(symbol_filter))
            if since is not None:
                market_query = market_query.where(MarketData.datetime >= since)
            market_stream = session.execute(market_query).scalars().yield_per(1000)

            market_buffer: dict[tuple[str, datetime], dict] = {}

            def flush_market_buffer() -> None:
                nonlocal market_buffer
                if not market_buffer:
                    return
                ops: list = []
                for (instrument, dt), doc in market_buffer.items():
                    ops.append(DeleteMany({"instrument": instrument, "dateTime": dt}))
                    ops.append(
                        UpdateOne(
                            {"instrument": instrument, "dateTime": dt},
                            {"$set": doc},
                            upsert=True,
                        )
                    )
                _mongo_market_data_col.bulk_write(ops, ordered=True)
                market_buffer = {}

            for md in market_stream:
                doc = {
                    "instrument": md.instrument,
                    "dateTime": md.datetime,
                    "open": float(md.open) if md.open is not None else None,
                    "high": float(md.high) if md.high is not None else None,
                    "low": float(md.low) if md.low is not None else None,
                    "close": float(md.close) if md.close is not None else None,
                    "volume": int(md.volume) if md.volume is not None else None,
                    "vwap": float(md.vwap) if md.vwap is not None else None,
                    "amount": float(md.amount) if md.amount is not None else None,
                    "factor": float(md.factor) if md.factor is not None else None,
                    "turnover": float(md.turnover) if md.turnover is not None else None,
                    "floatShares": int(md.float_shares) if md.float_shares is not None else None,
                    "createdAt": md.created_at,
                }
                market_buffer[(md.instrument, md.datetime)] = doc
                synced_market_data += 1
                if limit_market is not None and synced_market_data >= limit_market:
                    break
                if len(market_buffer) >= 500:
                    flush_market_buffer()
            flush_market_buffer()

    return {
        "stocks_synced": synced_stocks,
        "scores_synced": synced_scores,
        "market_data_synced": synced_market_data,
        "backend": "mongo",
        "stock_db": stock_db_url,
        "market_db": market_db_url,
        "score_db": score_db_url,
        "symbols_filtered": sorted(symbol_filter) if symbol_filter else None,
        "limit_market": limit_market,
        "skip_stocks": skip_stocks,
        "skip_scores": skip_scores,
        "skip_market": skip_market,
        "since": since.isoformat() if since else None,
    }


def _average_scores_by_symbol(results: list[dict]) -> list[dict]:
    """
    Collapse multiple scores per symbol into a single entry with average score.
    Keeps the latest timestamp (max) for reference.
    """
    if not results:
        return []
    bucket: dict[str, list[dict]] = {}
    for entry in results:
        symbol = str(entry.get("symbol") or "").upper()
        if not symbol:
            continue
        bucket.setdefault(symbol, []).append(entry)

    averaged: list[dict] = []
    for symbol, rows in bucket.items():
        scores = [float(r.get("score", 0)) for r in rows if r.get("score") is not None]
        if not scores:
            continue
        avg_score = sum(scores) / len(scores)
        latest_ts = max((r.get("timestamp") for r in rows if r.get("timestamp")), default=None)
        averaged.append(
            {
                "symbol": symbol,
                "score": avg_score,
                "timestamp": latest_ts,
            }
        )
    return averaged


def _merge_score_metadata_from_db(results: list[dict], database_url: str) -> None:
    """
    For each symbol in results, pull latest Score row from the target DB and
    merge quantity/volatility/growth/priceXM fields into the entry (if present).
    If growth is missing, compute it from market_data (close_latest - open_earliest).
    """
    if not results:
        return
    symbols = [r.get("symbol") for r in results if r.get("symbol")]
    if not symbols:
        return

    with _session_scope_for_sync(database_url) as session:
        latest_score_subq = (
            select(Score.stock_id, func.max(Score.score_id).label("max_id"))
            .join(Stock, Score.stock_id == Stock.stock_id)
            .where(Stock.symbol.in_(symbols))
            .group_by(Score.stock_id)
            .subquery()
        )
        rows = session.execute(
            select(Score, Stock.symbol, Stock.quantity)
            .join(Stock, Score.stock_id == Stock.stock_id)
            .join(latest_score_subq, Score.score_id == latest_score_subq.c.max_id)
        ).all()
    meta_map = {sym: (score, qty) for score, sym, qty in rows if sym}
    growth_map = _compute_growth_map_from_market_data(symbols, database_url)
    price_months_map = _compute_price_months_from_market_data(symbols, database_url)

    for entry in results:
        sym = str(entry.get("symbol") or "").upper()
        mapped = meta_map.get(sym)
        if not mapped:
            continue
        score_obj, stock_qty = mapped
        # quantity: prefer score.quantity, fallback to stock.quantity
        if entry.get("quantity") is None:
            entry["quantity"] = score_obj.quantity if score_obj.quantity is not None else stock_qty
        entry.setdefault("quantity", score_obj.quantity)
        entry.setdefault("volatility", decimal_to_float(score_obj.volatility))
        growth_val = entry.get("growth")
        if growth_val is None:
            growth_val = decimal_to_float(score_obj.growth)
        if growth_val is None:
            growth_val = growth_map.get(sym)
        if growth_val is not None:
            try:
                entry["growth"] = int(round(float(growth_val)))
            except (TypeError, ValueError):
                entry["growth"] = growth_val
        entry.setdefault("price1M", decimal_to_float(score_obj.price_1m))
        entry.setdefault("price2M", decimal_to_float(score_obj.price_2m))
        entry.setdefault("price3M", decimal_to_float(score_obj.price_3m))
        entry.setdefault("price4M", decimal_to_float(score_obj.price_4m))
        entry.setdefault("price5M", decimal_to_float(score_obj.price_5m))
        entry.setdefault("price6M", decimal_to_float(score_obj.price_6m))
        if sym in price_months_map:
            for k, v in price_months_map[sym].items():
                entry[k] = v


def _compute_growth_map_from_market_data(symbols: list[str], database_url: str) -> dict[str, float]:
    """
    Compute growth per symbol as (latest close - earliest open) from market_data.
    """
    if not symbols:
        return {}
    result: dict[str, float] = {}
    with _session_scope_for_sync(database_url) as session:
        for sym in symbols:
            symbol = str(sym).upper()
            earliest = session.execute(
                select(MarketData).where(MarketData.instrument == symbol).order_by(MarketData.datetime.asc()).limit(1)
            ).scalar_one_or_none()
            latest = session.execute(
                select(MarketData).where(MarketData.instrument == symbol).order_by(MarketData.datetime.desc()).limit(1)
            ).scalar_one_or_none()
            if earliest is None or latest is None:
                continue
            if earliest.open is None or latest.close is None:
                continue
            try:
                growth_val = float(latest.close) - float(earliest.open)
                result[symbol] = growth_val
            except (TypeError, ValueError):
                continue
    return result


def _compute_price_months_from_market_data(symbols: list[str], database_url: str) -> dict[str, dict[str, float]]:
    """
    For each symbol, find the earliest available trading day in each of the latest 6 months
    and map its VWAP (or close if VWAP is missing) to price1M..price6M.
    """
    if not symbols:
        return {}
    result: dict[str, dict[str, float]] = {}
    with _session_scope_for_sync(database_url) as session:
        for sym in symbols:
            symbol = str(sym).upper()
            # Pull a recent window (roughly 13 months at daily bars) to cover missing first-of-month days.
            rows = (
                session.execute(
                    select(MarketData)
                    .where(MarketData.instrument == symbol)
                    .order_by(MarketData.datetime.desc())
                    .limit(400)
                )
                .scalars()
                .all()
            )
            if not rows:
                continue
            # Iterate oldest -> newest so the first seen per (year, month) is the earliest day available.
            month_earliest: dict[tuple[int, int], float | None] = {}
            for md in reversed(rows):
                dt = md.datetime
                key = (dt.year, dt.month)
                if key in month_earliest:
                    continue
                price_val = None
                if md.vwap is not None:
                    try:
                        price_val = float(md.vwap)
                    except (TypeError, ValueError):
                        price_val = None
                if price_val is None and md.close is not None:
                    try:
                        price_val = float(md.close)
                    except (TypeError, ValueError):
                        price_val = None
                month_earliest[key] = price_val

            # Take the latest six months by (year, month) descending.
            prices = {}
            for idx, key in enumerate(sorted(month_earliest.keys(), reverse=True)[:6], start=1):
                prices[f"price{idx}M"] = month_earliest.get(key)
            result[symbol] = prices
    return result


def _ensure_stocks_in_target_db(symbols: list[str], target_db_url: str) -> dict:
    """
    Ensure the given symbols exist in the Stock table of target_db_url.
    If Mongo stocks collection is available, copy metadata from there; otherwise insert bare symbols.
    """
    if not symbols:
        return {"checked": 0, "inserted": 0, "note": "no symbols provided"}
    inserted = 0
    checked = 0
    if _mongo_stocks_col is None:
        note = "Mongo stocks not available; skipped insert"
        return {"checked": len(symbols), "inserted": 0, "note": note}

    with _session_scope_for_sync(target_db_url) as session:
        for sym in symbols:
            checked += 1
            symbol = str(sym).upper()
            existing = session.execute(select(Stock).where(Stock.symbol == symbol)).scalar_one_or_none()
            if existing:
                continue
            doc = _mongo_stocks_col.find_one({"_id": symbol}) or {}
            stock = Stock(
                symbol=symbol,
                company=doc.get("company") or symbol,
                sector=doc.get("sector"),
                sub_sector=doc.get("subSector") or doc.get("sub_sector"),
                country=doc.get("country"),
                price=Decimal(str(doc["price"])) if doc.get("price") is not None else Decimal("0"),
                quantity=int(doc.get("quantity") or 0),
                last_updated=doc.get("lastUpdated") or datetime.now(timezone.utc),
            )
            session.add(stock)
            inserted += 1
        session.commit()

    return {"checked": checked, "inserted": inserted, "note": "stocks ensured in target db"}


def _dedupe_market_data(symbols: list[str] | None = None) -> dict:
    """
    Remove duplicate marketData docs that share the same (instrument, dateTime).
    Keeps one document per pair and deletes the rest.
    """
    if _mongo_market_data_col is None:
        return {"duplicate_groups": 0, "deleted": 0, "backend": "mongo"}

    match_stage = None
    if symbols:
        match_stage = {"instrument": {"$in": [s.upper() for s in symbols]}}

    pipeline = []
    if match_stage:
        pipeline.append({"$match": match_stage})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": {"instrument": "$instrument", "dateTime": "$dateTime"},
                    "ids": {"$addToSet": "$_id"},
                    "count": {"$sum": 1},
                }
            },
            {"$match": {"count": {"$gt": 1}}},
        ]
    )

    duplicate_groups = 0
    deleted = 0
    cursor = _mongo_market_data_col.aggregate(pipeline, allowDiskUse=True)
    for group in cursor:
        ids = group.get("ids") or []
        if not ids:
            continue
        duplicate_groups += 1
        keeper = ids[0]
        to_delete = [i for i in ids if i != keeper]
        if to_delete:
            res = _mongo_market_data_col.delete_many({"_id": {"$in": to_delete}})
            deleted += res.deleted_count

    return {"duplicate_groups": duplicate_groups, "deleted": deleted, "backend": "mongo"}


def _dedupe_collection_by_fields(collection, fields: list[str], partition_field: str | None = None) -> dict:
    """
    Remove duplicate documents in the given collection by grouping on the provided fields.
    Keeps one document per group and deletes the rest.
    """
    from pymongo.errors import OperationFailure

    if collection is None:
        return {"duplicate_groups": 0, "deleted": 0, "backend": "mongo", "error": "collection missing"}

    match_stage = {"$and": [{field: {"$exists": True}} for field in fields]} if fields else None
    pipeline = []
    if match_stage:
        pipeline.append({"$match": match_stage})
    pipeline.extend(
        [
            {
                "$group": {
                    "_id": {field: f"${field}" for field in fields},
                    "ids": {"$addToSet": "$_id"},
                    "count": {"$sum": 1},
                }
            },
            {"$match": {"count": {"$gt": 1}}},
        ]
    )

    duplicate_groups = 0
    deleted = 0
    try:
        cursor = collection.aggregate(pipeline, allowDiskUse=True)
        for group in cursor:
            ids = group.get("ids") or []
            if len(ids) <= 1:
                continue
            duplicate_groups += 1
            keeper = ids[0]
            to_delete = [i for i in ids if i != keeper]
            if to_delete:
                res = collection.delete_many({"_id": {"$in": to_delete}})
                deleted += res.deleted_count

        return {"duplicate_groups": duplicate_groups, "deleted": deleted, "backend": "mongo", "strategy": "aggregate"}
    except OperationFailure as exc:
        if exc.code == 292:  # memory limit; retry with streaming scan
            if partition_field:
                return _dedupe_collection_chunked(collection, fields, partition_field)
            fallback = _dedupe_collection_sorted_scan(collection, fields)
            fallback["strategy"] = "sorted_scan"
            fallback["note"] = "aggregate memory limit; used sorted_scan"
            return fallback
        raise


def _dedupe_collection_sorted_scan(collection, fields: list[str], filter_query: dict | None = None) -> dict:
    """
    Fallback deduper that streams sorted docs to avoid $group memory limits.
    """
    from pymongo.errors import OperationFailure

    if collection is None:
        return {"duplicate_groups": 0, "deleted": 0, "backend": "mongo", "error": "collection missing"}

    projection = {field: 1 for field in fields}
    projection["_id"] = 1
    sort_spec = [(field, 1) for field in fields] + [("_id", 1)]

    # Ensure an index exists so the sort does not spill to memory.
    try:
        index_name = collection.create_index(sort_spec, background=True)
    except Exception:  # pragma: no cover - defensive guard
        index_name = None

    try:
        cursor = collection.find(
            filter_query or {},
            projection=projection,
            sort=sort_spec,
            batch_size=2000,
            hint=index_name if index_name else None,
        )
    except OperationFailure as exc:
        return {"duplicate_groups": 0, "deleted": 0, "backend": "mongo", "error": str(exc), "strategy": "sorted_scan"}

    duplicate_groups = 0
    deleted = 0
    last_key = None
    to_delete: list = []
    for doc in cursor:
        key = tuple(doc.get(field) for field in fields)
        if key == last_key:
            to_delete.append(doc["_id"])
        else:
            if to_delete:
                res = collection.delete_many({"_id": {"$in": to_delete}})
                deleted += res.deleted_count
                duplicate_groups += 1
                to_delete = []
            last_key = key

    if to_delete:
        res = collection.delete_many({"_id": {"$in": to_delete}})
        deleted += res.deleted_count
        duplicate_groups += 1

    return {
        "duplicate_groups": duplicate_groups,
        "deleted": deleted,
        "backend": "mongo",
        "strategy": "sorted_scan",
        "index_used": index_name,
        "note": "sorted_scan used to minimize memory during dedupe",
    }


def _dedupe_collection_chunked(collection, fields: list[str], partition_field: str) -> dict:
    """
    Run sorted-scan dedupe per partition to keep memory usage minimal.
    """
    distinct_values = collection.distinct(partition_field)
    total_duplicate_groups = 0
    total_deleted = 0
    for value in distinct_values:
        filter_query = {partition_field: value}
        result = _dedupe_collection_sorted_scan(collection, fields, filter_query=filter_query)
        total_duplicate_groups += result.get("duplicate_groups", 0)
        total_deleted += result.get("deleted", 0)
    return {
        "duplicate_groups": total_duplicate_groups,
        "deleted": total_deleted,
        "backend": "mongo",
        "strategy": "sorted_scan_partitioned",
        "partition_field": partition_field,
        "note": "partitioned sorted_scan used to minimize memory during dedupe",
    }


@routes_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

cron_bp = Blueprint("cron", __name__)  # separate blueprint for cron routes

CRON_SECRET = os.getenv("CRON_SECRET")  # set in Vercel dashboard

@routes_bp.route('/signup', methods=['POST'])
def signup():
    data = request.get_json()
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email already registered'}), 400
    
    user = User(username=data['username'], email=data['email'])
    user.set_password(data['password'])
    db.session.add(user)
    db.session.flush()  # Get user_id before creating account
    
    # Auto-create account with default balance
    from decimal import Decimal
    account = Account(user_id=user.user_id, balance=Decimal('10000.00'))
    db.session.add(account)
    db.session.commit()
    
    return jsonify({'message': 'User created successfully'}), 201

@routes_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username_or_email = data.get('username')
    
    # Query for user by username OR email
    user = User.query.filter(
        db.or_(User.username == username_or_email, User.email == username_or_email)
    ).first()
    
    if user and user.check_password(data['password']):
        token = create_access_token(identity=str(user.user_id))
        
        # Get account balance
        account = Account.query.filter_by(user_id=user.user_id).first()
        
        # Calculate total holdings value (using MySQL Stock table as fallback)
        holdings = Holding.query.filter(Holding.user_id == user.user_id).all()
        total_holdings_value = 0.0
        
        for holding in holdings:
            stock_price = None
            
            # Try MongoDB first
            try:
                mongo_stock = MongoStock.objects(stock_id=holding.stock_id).first()
                if mongo_stock and mongo_stock.price:
                    stock_price = float(mongo_stock.price)
            except Exception:
                pass  # MongoDB failed, will try MySQL below
            
            # Fallback to MySQL if MongoDB didn't return a price
            if stock_price is None:
                sql_stock = Stock.query.get(holding.stock_id)
                if sql_stock and sql_stock.price:
                    stock_price = float(sql_stock.price)
            
            if stock_price:
                total_holdings_value += stock_price * holding.quantity
        
        return jsonify({
            'token': token,
            'user': {
                'user_id': user.user_id,
                'username': user.username,
                'email': user.email,
                'balance': float(account.balance) if account else 0.00,
                'holdings_value': total_holdings_value
            }
        }), 200
    return jsonify({'error': 'Invalid credentials'}), 401


@routes_bp.route('/score', methods=['POST'])
def score():
    payload = request.get_json(silent=True) or {}
    overrides = payload.get('config') if isinstance(payload, dict) else {}
    config: QlibConfig = load_config_from_env(overrides)

    try:
        response = run_scoring_workflow(config)
    except Exception as exc:  # pragma: no cover - defensive guard
        # Log server-side and return a useful error message to the client
        current_app.logger.exception("Scoring workflow failed")
        message = str(exc) or f"{exc.__class__.__name__} occurred"
        return jsonify({'error': message}), 500

    return jsonify(response), 200


@routes_bp.route('/users', methods=['POST'])
def create_or_update_user():
    data = request.get_json()
    u = upsert_user(
        username=data["username"],
        email=data["email"],
        password_plain=data["password"],
        balance=data.get("balance", 0.0),
        risk_averse=data.get("risk_averse", "no"),
    )
    db.session.commit()
    return jsonify({"user_id": u.user_id}), 201

@routes_bp.route('/stocks', methods=['POST'])
def create_or_update_stock():
    """
    Create or update a stock record using the posted JSON payload.

    Accepts both camelCase and snake_case keys for compatibility:
    {
      "symbol": "A",
      "stockId": 1,                       (optional)
      "company": "Agilent Technologies Inc.",
      "sector": "Healthcare",
      "subSector": "Diagnostics & Research",
      "country": "United States",
      "price": 151.25,
      "quantity": 283500427,
      "lastUpdated": "2025-11-23T20:33:04Z"  (optional, defaults to now)
      "use_mongo": true                     (optional; or ?backend=mongo)
    }
    """
    payload = request.get_json(force=True, silent=True) or {}
    try:
        symbol = _required(payload, "symbol", type_=str).upper()
        company = _required(payload, "company", type_=str)
        sector = _first_present(payload, "sector")
        sub_sector = _first_present(payload, "subSector", "sub_sector")
        country = _first_present(payload, "country")
        price = float(_required(payload, "price"))
        quantity = int(_required(payload, "quantity"))
        last_updated_raw = _first_present(payload, "lastUpdated", "last_updated")
        stock_id_value = _first_present(payload, "stockId", "stock_id")
        last_updated = (
            parse_iso_datetime(last_updated_raw, "lastUpdated")
            if last_updated_raw is not None
            else datetime.now(timezone.utc)
        )

        if _wants_mongo(payload):
            if _mongo_stocks_col is None:
                return jsonify({"ok": False, "error": "MongoDB not configured; set MONGO_URI"}), 400

            doc = {
                "_id": symbol,
                "company": company,
                "sector": sector,
                "subSector": sub_sector,
                "country": country,
                "price": price,
                "quantity": quantity,
                "lastUpdated": last_updated,
            }
            if stock_id_value is not None:
                doc["stockId"] = stock_id_value

            result = _mongo_stocks_col.update_one(
                {"_id": symbol},
                {"$set": doc},
                upsert=True,
            )

            return jsonify(
                {
                    "ok": True,
                    "backend": "mongo",
                    "symbol": symbol,
                    "upserted_id": str(result.upserted_id) if result.upserted_id else None,
                    "matched_count": result.matched_count,
                    "modified_count": result.modified_count,
                }
            ), 201 if result.upserted_id else 200

        stock = None
        if stock_id_value is not None:
            try:
                stock_id_int = int(stock_id_value)
            except (TypeError, ValueError):
                raise ValueError("Field 'stockId' must be an integer")
            stock = db.session.get(Stock, stock_id_int)
            if stock is None:
                raise ValueError(f"Stock with ID {stock_id_int} not found")

        if stock is None:
            stock = db.session.execute(
                select(Stock).where(Stock.symbol == symbol)
            ).scalar_one_or_none()

        created = False
        if stock is None:
            stock = Stock(symbol=symbol)
            db.session.add(stock)
            created = True

        stock.company = company
        stock.sector = sector
        stock.sub_sector = sub_sector
        stock.country = country
        stock.price = price
        stock.quantity = quantity
        stock.last_updated = last_updated

        db.session.commit()

        return jsonify(
            {
                "ok": True,
                "stock_id": stock.stock_id,
                "symbol": stock.symbol,
                "created": created,
                "last_updated": stock.last_updated.isoformat() if stock.last_updated else None,
            }
        ), 201 if created else 200

    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - defensive guard
        db.session.rollback()
        current_app.logger.exception("Stock create/update failed")
        return jsonify({"ok": False, "error": f"Server error: {exc}"}), 500

@routes_bp.route('/transactions', methods=['POST'])
def create_transaction():
    data = request.get_json()
    t = add_transaction(
        email=data["email"],
        symbol=data["symbol"],
        txn_type=data["transaction_type"],
        qty=int(data["quantity"]),
        price=float(data["price"]),
    )
    db.session.commit()
    return jsonify({"transaction_id": t.transaction_id}), 201


@routes_bp.route('/transactions/execute', methods=['POST'])
@jwt_required()
def execute_transaction():
    """
    Execute a complete transaction (buy/sell) for the authenticated user.
    Updates account balance and holdings automatically.
    Price is automatically fetched from the stock's current price.
    
    Request body:
    {
        "stock_id": 1,
        "transaction_type": "buy" or "sell",
        "quantity": 10
    }
    """
    user_id = get_jwt_identity()
    data = request.get_json()
    
    try:
        stock_id = data.get("stock_id")
        txn_type = data.get("transaction_type")
        qty = int(data.get("quantity", 0))
        
        if not stock_id or not txn_type:
            return jsonify({'error': 'stock_id and transaction_type are required'}), 400
        
        if qty <= 0:
            return jsonify({'error': 'quantity must be greater than 0'}), 400
        
        # Process the transaction (price is fetched from stock table)
        transaction = process_transaction(
            user_id=int(user_id),
            stock_id=stock_id,
            txn_type=txn_type,
            qty=qty
        )
        db.session.commit()
        
        # Get updated account balance
        account = Account.query.filter_by(user_id=user_id).first()
        
        return jsonify({
            'message': f'Transaction executed successfully',
            'transaction_id': transaction.transaction_id,
            'transaction_type': transaction.transaction_type,
            'quantity': transaction.quantity_transac,
            'price': float(transaction.price_transac),
            'total': float(transaction.price_transac * transaction.quantity_transac),
            'new_balance': float(account.balance) if account else None
        }), 201
        
    except ValueError as e:
        db.session.rollback()
        print(f"ValueError: {str(e)}")
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        import traceback
        print(f"Exception: {str(e)}")
        traceback.print_exc()
        return jsonify({'error': f'Transaction failed: {str(e)}'}), 500



@routes_bp.route('/transactions', methods=['GET'])
@jwt_required()
def get_user_transactions():
    """Get all transactions for the authenticated user, sorted by date (newest first)"""
    user_id = get_jwt_identity()
    
    # Query transactions from MySQL
    transactions = Transaction.query\
        .filter(Transaction.user_id == user_id)\
        .order_by(Transaction.date_transac.desc())\
        .all()
    
    transactions_data = []
    for transaction in transactions:
        # Fetch stock data from MongoDB
        stock = MongoStock.objects(stock_id=transaction.stock_id).first()
        
        transactions_data.append({
            'transaction_id': transaction.transaction_id,
            'user_id': transaction.user_id,
            'stock_id': transaction.stock_id,
            'stock_symbol': stock.symbol if stock else None,
            'stock_company': stock.company if stock else None,
            'transaction_type': transaction.transaction_type,
            'quantity': transaction.quantity_transac,
            'price': float(transaction.price_transac),
            'date': transaction.date_transac.isoformat() if transaction.date_transac else None
        })
    
    return jsonify(transactions_data), 200


@routes_bp.route('/transactions/<int:transaction_id>', methods=['GET'])
@jwt_required()
def get_transaction(transaction_id):
    """Get a specific transaction for the authenticated user"""
    user_id = get_jwt_identity()
    
    transaction = Transaction.query.filter_by(
        transaction_id=transaction_id,
        user_id=user_id
    ).first()
    
    if not transaction:
        return jsonify({'error': 'Transaction not found'}), 404
    
    # Fetch stock data from MongoDB
    stock = MongoStock.objects(stock_id=transaction.stock_id).first()
    
    return jsonify({
        'transaction_id': transaction.transaction_id,
        'user_id': transaction.user_id,
        'stock_id': transaction.stock_id,
        'stock_symbol': stock.symbol if stock else None,
        'stock_company': stock.company if stock else None,
        'transaction_type': transaction.transaction_type,
        'quantity': transaction.quantity_transac,
        'price': float(transaction.price_transac),
        'date': transaction.date_transac.isoformat() if transaction.date_transac else None
    }), 200
    
@routes_bp.route('/seed_database', methods=['POST'])
def seed_database_endpoint():
    result = seed_database()
    return result, 201

@routes_bp.route('/popular', methods=['GET'])
def popular_stocks():
    """
    Return the top popular stocks ranked by turnover (price_transac * quantity_transac).
    Falls back to highest-priced stocks when no turnover data exists.
    """
    try:
        limit = int(request.args.get("limit", 6))
    except ValueError:
        limit = 6
    limit = max(0, limit)
    if limit == 0:
        return jsonify([]), 200

    use_mongo = (
        _mongo_market_data_col is not None
        and _mongo_stocks_col is not None
        and (request.args.get("backend", "mongo").lower() != "sql")
    )
    if use_mongo:
        # Use latest marketData turnover per instrument from Mongo.
        pipeline = [
            {"$match": {"turnover": {"$ne": None}}},
            {"$sort": {"instrument": 1, "dateTime": -1}},
            {
                "$group": {
                    "_id": "$instrument",
                    "turnover": {"$first": "$turnover"},
                    "close": {"$first": "$close"},
                    "vwap": {"$first": "$vwap"},
                    "dateTime": {"$first": "$dateTime"},
                }
            },
            {"$sort": {"turnover": -1}},
            {"$limit": limit},
        ]
        turnover_docs = list(_mongo_market_data_col.aggregate(pipeline))
        symbols = [str(doc.get("_id") or "").upper() for doc in turnover_docs if doc.get("_id")]

        # Fetch stock metadata and growth (if any) from Mongo.
        stock_map = {}
        if symbols:
            stock_map = {
                str(doc["_id"]).upper(): doc
                for doc in _mongo_stocks_col.find(
                    {"_id": {"$in": symbols}},
                    {
                        "company": 1,
                        "sector": 1,
                        "subSector": 1,
                        "country": 1,
                        "price": 1,
                        "quantity": 1,
                        "lastUpdated": 1,
                        "stockId": 1,
                    },
                )
            }
        score_map = {}
        if _mongo_scores_col is not None and symbols:
            score_map = {
                str(doc.get("symbol") or "").upper(): doc
                for doc in _mongo_scores_col.find(
                    {"symbol": {"$in": symbols}},
                    {"growth": 1},
                )
            }

        response = []
        for doc in turnover_docs:
            symbol = str(doc.get("_id") or "").upper()
            stock_doc = stock_map.get(symbol, {})
            score_doc = score_map.get(symbol, {})

            price_val = decimal_to_float(stock_doc.get("price")) or decimal_to_float(doc.get("close")) or decimal_to_float(doc.get("vwap")) or 0.0
            change_pct = decimal_to_float(score_doc.get("growth")) or 0.0
            change_abs = price_val * (change_pct / 100.0) if change_pct else 0.0
            last_updated = stock_doc.get("lastUpdated")

            response.append(
                {
                    "stock_id": stock_doc.get("stockId"),
                    "symbol": symbol,
                    "company": stock_doc.get("company"),
                    "sector": stock_doc.get("sector"),
                    "sub_sector": stock_doc.get("subSector") or stock_doc.get("sub_sector"),
                    "country": stock_doc.get("country"),
                    "price": price_val,
                    "quantity": stock_doc.get("quantity"),
                    "last_updated": last_updated.isoformat() if hasattr(last_updated, "isoformat") else None,
                    "turnover": decimal_to_float(doc.get("turnover")) or 0.0,
                    "change": change_abs,
                    "change_pct": change_pct,
                }
            )

        # Fallback to top-priced Mongo stocks when no turnover data.
        if not response and _mongo_stocks_col is not None:
            fallback_docs = list(
                _mongo_stocks_col.find(
                    {},
                    {
                        "_id": 1,
                        "company": 1,
                        "sector": 1,
                        "subSector": 1,
                        "country": 1,
                        "price": 1,
                        "quantity": 1,
                        "lastUpdated": 1,
                        "stockId": 1,
                    },
                ).sort("price", -1).limit(limit)
            )
            for doc in fallback_docs:
                price_val = decimal_to_float(doc.get("price")) or 0.0
                response.append(
                    {
                        "stock_id": doc.get("stockId"),
                        "symbol": str(doc.get("_id") or "").upper(),
                        "company": doc.get("company"),
                        "sector": doc.get("sector"),
                        "sub_sector": doc.get("subSector") or doc.get("sub_sector"),
                        "country": doc.get("country"),
                        "price": price_val,
                        "quantity": doc.get("quantity"),
                        "last_updated": doc.get("lastUpdated").isoformat() if hasattr(doc.get("lastUpdated"), "isoformat") else None,
                        "turnover": 0.0,
                        "change": 0.0,
                        "change_pct": 0.0,
                    }
                )

        return jsonify(response), 200

    turnover_expr = func.sum(Transaction.price_transac * Transaction.quantity_transac)
    popular_query = (
        select(Stock, turnover_expr.label("turnover"))
        .join(Transaction, Stock.stock_id == Transaction.stock_id)
        .group_by(Stock.stock_id)
        .order_by(turnover_expr.desc())
        .limit(limit)
    )

    rows = db.session.execute(popular_query).all()
    growth_map = _get_growth_map([s.stock_id for s, _ in rows])
    response = []
    for stock, turnover in rows:
        change_pct = growth_map.get(stock.stock_id, 0.0)
        change_abs = float(stock.price or 0) * (change_pct / 100.0) if change_pct else 0.0
        response.append({
            'stock_id': stock.stock_id,
            'symbol': stock.symbol,
            'company': stock.company,
            'sector': stock.sector,
            'sub_sector': stock.sub_sector,
            'country': stock.country,
            'price': float(stock.price or 0),
            'quantity': stock.quantity,
            'last_updated': stock.last_updated.isoformat() if stock.last_updated else None,
            'turnover': float(turnover or 0),
            'change': change_abs,
            'change_pct': change_pct,
        })

    # Fallback: if no turnover data, return highest-priced stocks
    if not response:
        fallback = db.session.execute(
            select(Stock).order_by(Stock.price.desc()).limit(limit)
        ).scalars().all()
        growth_map = _get_growth_map([s.stock_id for s in fallback])
        response = [
            {
                'stock_id': stock.stock_id,
                'symbol': stock.symbol,
                'company': stock.company,
                'sector': stock.sector,
                'sub_sector': stock.sub_sector,
                'country': stock.country,
                'price': float(stock.price or 0),
                'quantity': stock.quantity,
                'last_updated': stock.last_updated.isoformat() if stock.last_updated else None,
                'turnover': 0.0,
                'change': float(stock.price or 0) * (growth_map.get(stock.stock_id, 0.0) / 100.0) if growth_map.get(stock.stock_id) else 0.0,
                'change_pct': growth_map.get(stock.stock_id, 0.0),
            }
            for stock in fallback
        ]

    return jsonify(response), 200

@routes_bp.route('/search', methods=['POST'])
def search_stocks_endpoint():
    """
    Search stocks based on text and optional filters.
    Request body:
    {
        "text": "",
        "filters": {
            "country": "" or ["USA", "Canada"],
            "min_price": 0,
            "max_price": 100,
            "sector": "" or ["Technology", "Healthcare"],
            "sub_sector": "" or ["Software", "Biotechnology"]
        }
    }
    """
    data = request.get_json()
    
    text = data.get('text', '')
    filters = data.get('filters', {})
    
    # These can be either strings or lists
    country = filters.get('country', None)
    min_price = float(filters.get('min_price', 0))
    max_price = float(filters.get('max_price', 0))
    sector = filters.get('sector', None)
    sub_sector = filters.get('sub_sector', None)
    
    results = search_stocks(
        text=text,
        country=country,
        min_price=min_price,
        max_price=max_price,
        sector=sector,
        sub_sector=sub_sector
    )
    
    return jsonify(_stocks_to_json(results)), 200

@routes_bp.route('/stockbyscore', methods=['GET'])
def stock_by_score():
    """
    Return the top scored stocks (latest score per stock) ordered by score desc.
    Optional ?limit query param (default 10).
    """
    try:
        limit = int(request.args.get("limit", 10))
    except ValueError:
        limit = 10
    limit = max(0, limit)
    if limit == 0:
        return jsonify([]), 200

    # Prefer MongoDB if configured; allow opting back to SQL with ?backend=sql
    use_mongo = _mongo_scores_col is not None and (request.args.get("backend", "mongo").lower() != "sql")
    if use_mongo:
        score_docs = list(
            _mongo_scores_col.find(
                {},
                {
                    "stockId": 1,
                    "symbol": 1,
                    "score": 1,
                    "quantity": 1,
                    "volatility": 1,
                    "growth": 1,
                    "price1M": 1,
                    "price2M": 1,
                    "price3M": 1,
                    "price4M": 1,
                    "price5M": 1,
                    "price6M": 1,
                },
            )
            .sort("score", -1)
            .limit(limit)
        )

        # Pull stock metadata (price/company/etc.) in one batch.
        symbols = [str(doc.get("symbol") or "").upper() for doc in score_docs if doc.get("symbol")]
        stock_map = {}
        if _mongo_stocks_col is not None and symbols:
            stock_map = {
                str(doc["_id"]).upper(): doc
                for doc in _mongo_stocks_col.find(
                    {"_id": {"$in": symbols}},
                    {"company": 1, "sector": 1, "subSector": 1, "country": 1, "price": 1, "quantity": 1, "lastUpdated": 1},
                )
            }

        response = []
        for doc in score_docs:
            symbol = str(doc.get("symbol") or "").upper()
            stock_doc = stock_map.get(symbol, {})
            price_val = decimal_to_float(stock_doc.get("price")) or 0.0
            change_pct = decimal_to_float(doc.get("growth")) or 0.0
            change_abs = price_val * (change_pct / 100.0) if change_pct else 0.0
            last_updated = stock_doc.get("lastUpdated")
            quantity_val = doc.get("quantity")
            if quantity_val is None:
                quantity_val = stock_doc.get("quantity")

            response.append(
                {
                    "stock_id": doc.get("stockId"),
                    "symbol": symbol,
                    "company": stock_doc.get("company"),
                    "sector": stock_doc.get("sector"),
                    "sub_sector": stock_doc.get("subSector") or stock_doc.get("sub_sector"),
                    "country": stock_doc.get("country"),
                    "price": price_val,
                    "quantity": quantity_val,
                    "last_updated": last_updated.isoformat() if hasattr(last_updated, "isoformat") else None,
                    "score": decimal_to_float(doc.get("score")),
                    "price_1m": decimal_to_float(doc.get("price1M")),
                    "price_2m": decimal_to_float(doc.get("price2M")),
                    "price_3m": decimal_to_float(doc.get("price3M")),
                    "price_4m": decimal_to_float(doc.get("price4M")),
                    "price_5m": decimal_to_float(doc.get("price5M")),
                    "price_6m": decimal_to_float(doc.get("price6M")),
                    "change": change_abs,
                    "change_pct": change_pct,
                }
            )

        return jsonify(response), 200

    latest_score_subq = (
        select(Score.stock_id, func.max(Score.score_id).label("score_id"))
        .group_by(Score.stock_id)
        .subquery()
    )

    rows = db.session.execute(
        select(Score, Stock)
        .join(latest_score_subq, Score.score_id == latest_score_subq.c.score_id)
        .join(Stock, Stock.stock_id == Score.stock_id)
        .order_by(Score.score.desc())
        .limit(limit)
    ).all()

    response = []
    for score_obj, stock in rows:
        change_pct = decimal_to_float(score_obj.growth) or 0.0
        change_abs = float(stock.price or 0) * (change_pct / 100.0) if change_pct else 0.0
        response.append({
            'stock_id': stock.stock_id,
            'symbol': stock.symbol,
            'company': stock.company,
            'sector': stock.sector,
            'sub_sector': stock.sub_sector,
            'country': stock.country,
            'price': float(stock.price or 0),
            'quantity': stock.quantity,
            'last_updated': stock.last_updated.isoformat() if stock.last_updated else None,
            'score': decimal_to_float(score_obj.score),
            'price_1m': decimal_to_float(score_obj.price_1m),
            'price_2m': decimal_to_float(score_obj.price_2m),
            'price_3m': decimal_to_float(score_obj.price_3m),
            'price_4m': decimal_to_float(score_obj.price_4m),
            'price_5m': decimal_to_float(score_obj.price_5m),
            'price_6m': decimal_to_float(score_obj.price_6m),
            'change': change_abs,
            'change_pct': change_pct,
        })

    return jsonify(response), 200

def _stocks_to_json(stocks):
    growth_map = _get_growth_map([s.stock_id for s in stocks])
    result = []
    for stock in stocks:
        change_pct = growth_map.get(stock.stock_id, 0.0)
        change_abs = float(stock.price or 0) * (change_pct / 100.0) if change_pct else 0.0
        result.append(
            {
                'stock_id': stock.stock_id,
                'symbol': stock.symbol,
                'company': stock.company,
                'sector': stock.sector,
                'sub_sector': stock.sub_sector,
                'country': stock.country,
                'price': float(stock.price),
                'quantity': stock.quantity,
                'last_updated': stock.last_updated.isoformat() if stock.last_updated else None,
                'change': change_abs,
                'change_pct': change_pct,
            }
        )
    return result

@routes_bp.route('/stocks/<symbol>', methods=['GET'])
def get_stock_by_symbol(symbol):
    """
    Get a single stock from the database by symbol.
    
    GET /stocks/AAPL
    
    Returns:
    {
        "stock_id": 123,
        "symbol": "AAPL",
        "company": "Apple Inc.",
        "sector": "Technology",
        "sub_sector": "Consumer Electronics",
        "country": "USA",
        "price": 150.25,
        "quantity": 15000000000,
        "last_updated": "2025-11-22T10:30:00"
    }
    """
    stock = db.session.execute(
        select(Stock).where(Stock.symbol == symbol.upper())
    ).scalar_one_or_none()
    
    if not stock:
        return jsonify({'error': f'Stock {symbol.upper()} not found'}), 404
    
    return jsonify({
        'stock_id': stock.stock_id,
        'symbol': stock.symbol,
        'company': stock.company,
        'sector': stock.sector,
        'sub_sector': stock.sub_sector,
        'country': stock.country,
        'price': float(stock.price),
        'quantity': stock.quantity,
        'last_updated': stock.last_updated.isoformat() if stock.last_updated else None
    }), 200


@routes_bp.route('/stocks/all/symbols', methods=['GET'])
def get_all_stock_symbols():
    """
    Get all stock symbols and company names from the database.
    
    GET /stocks/all/symbols
    
    Returns:
    [
        {"symbol": "AAPL", "company": "Apple Inc."},
        {"symbol": "MSFT", "company": "Microsoft Corporation"},
        ...
    ]
    """
    stocks = db.session.execute(
        select(Stock.symbol, Stock.company).order_by(Stock.symbol)
    ).all()
    
    return jsonify([
        {'symbol': symbol, 'company': company}
        for symbol, company in stocks
    ]), 200

@routes_bp.route('/yahoo/<symbol>', methods=['GET'])
def get_yahoo_finance_data(symbol):
    """
    Fetch real-time stock data from Yahoo Finance.
    
    GET /yahoo/AAPL
    
    Returns:
    {
        "symbol": "AAPL",
        "company": "Apple Inc.",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "price": 150.25,
        "quantity": 15000000000,
        "country": "United States"
    }
    """
    try:
        ticker = yf.Ticker(symbol.upper())
        info = ticker.info
        
        return jsonify({
            'symbol': symbol.upper(),
            'company': info.get('longName') or info.get('shortName'),
            'sector': info.get('sector'),
            'industry': info.get('industry'),
            'price': info.get('currentPrice') or info.get('regularMarketPrice'),
            'quantity': info.get('sharesOutstanding'),
            'country': info.get('country'),
            'market_cap': info.get('marketCap'),
            'pe_ratio': info.get('trailingPE'),
            'dividend_yield': info.get('dividendYield'),
            'week_52_high': info.get('fiftyTwoWeekHigh'),
            'week_52_low': info.get('fiftyTwoWeekLow')
        }), 200
        
    except Exception as e:
        return jsonify({
            'error': f'Failed to fetch data for {symbol}',
            'details': str(e)
        }), 404


@routes_bp.route('/market-data', methods=['POST'])
def create_market_data():
    """
    Create or update a market data record.

    Expected JSON body (camelCase or snake_case):
    {
      "instrument": "AAPL",
      "dateTime": "2025-11-23T20:59:20Z",
      "open": 144.47,
      "high": 151.76,
      "low": 144.47,
      "close": 151.25,
      "vwap": 149.16,
      "volume": 2468420,
      "amount": 368189527.20,
      "factor": 1.398,
      "turnover": 0.008737,
      "floatShares": 282519516,
      "createdAt": "2025-11-23T21:00:15Z"
    }
    """
    payload = request.get_json(force=True, silent=True) or {}
    try:
        instrument = _required(payload, "instrument", type_=str).upper()
        date_time = parse_iso_datetime(_required(payload, "dateTime", "datetime"), "dateTime")

        open_ = float(_required(payload, "open"))
        high = float(_required(payload, "high"))
        low = float(_required(payload, "low"))
        close = float(_required(payload, "close"))
        vwap_raw = _first_present(payload, "vwap")
        vwap = float(vwap_raw) if vwap_raw is not None else None

        volume = int(_required(payload, "volume"))
        amount_raw = _first_present(payload, "amount")
        amount = float(amount_raw) if amount_raw is not None else None
        factor_raw = _first_present(payload, "factor")
        factor = float(factor_raw) if factor_raw is not None else None
        turnover_raw = _first_present(payload, "turnover")
        turnover = float(turnover_raw) if turnover_raw is not None else None
        float_shares_raw = _first_present(payload, "floatShares", "float_shares")
        float_shares = int(float_shares_raw) if float_shares_raw is not None else None

        created_at_raw = _first_present(payload, "createdAt", "created_at")
        created_at = (
            parse_iso_datetime(created_at_raw, "createdAt")
            if created_at_raw is not None
            else datetime.now(timezone.utc)
        )

        if _wants_mongo(payload):
            if _mongo_market_data_col is None:
                return jsonify({"ok": False, "error": "MongoDB not configured; set MONGO_URI"}), 400

            doc = {
                "instrument": instrument,
                "dateTime": date_time,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "vwap": vwap,
                "volume": volume,
                "amount": amount,
                "factor": factor,
                "turnover": turnover,
                "floatShares": float_shares,
                "createdAt": created_at,
            }

            result = _mongo_market_data_col.update_one(
                {"instrument": instrument, "dateTime": date_time},
                {"$set": doc},
                upsert=True,
            )

            return jsonify(
                {
                    "ok": True,
                    "backend": "mongo",
                    "instrument": instrument,
                    "upserted_id": str(result.upserted_id) if result.upserted_id else None,
                    "matched_count": result.matched_count,
                    "modified_count": result.modified_count,
                }
            ), 201 if result.upserted_id else 200

        market_data = db.session.execute(
            select(MarketData).where(
                MarketData.instrument == instrument,
                MarketData.datetime == date_time
            )
        ).scalar_one_or_none()

        created = False
        if market_data is None:
            market_data = MarketData(instrument=instrument, datetime=date_time)
            db.session.add(market_data)
            created = True

        market_data.open = open_
        market_data.high = high
        market_data.low = low
        market_data.close = close
        market_data.volume = volume
        market_data.vwap = vwap
        market_data.amount = amount
        market_data.factor = factor
        market_data.turnover = turnover
        market_data.float_shares = float_shares
        market_data.created_at = created_at

        db.session.commit()

        return jsonify(
            {
                "ok": True,
                "id": market_data.id,
                "instrument": market_data.instrument,
                "created": created,
            }
        ), 201 if created else 200

    except ValueError as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - defensive guard
        db.session.rollback()
        current_app.logger.exception("Market data insert failed")
        return jsonify({"ok": False, "error": f"Server error: {exc}"}), 500


@routes_bp.route('/market-data/<symbol>', methods=['GET'])
def get_market_data(symbol):
    """
    Get the latest market data for a symbol from the database.
    
    GET /market-data/AAPL
    
    Returns:
    {
        "id": 123,
        "datetime": "2025-11-23T16:00:00",
        "instrument": "AAPL",
        "open": 265.88,
        "high": 273.315,
        "low": 265.82,
        "close": 271.49,
        "volume": 59030832,
        "vwap": 269.50,
        "amount": 15900000000.00,
        "factor": 1.0,
        "turnover": 0.004,
        "float_shares": 14776353000
    }
    """
    market_data = db.session.execute(
        select(MarketData)
        .where(MarketData.instrument == symbol.upper())
        .order_by(MarketData.datetime.desc())
        .limit(1)
    ).scalar_one_or_none()
    
    if not market_data:
        return jsonify({'error': f'No market data found for {symbol.upper()}'}), 404
    
    return jsonify({
        'id': market_data.id,
        'datetime': market_data.datetime.isoformat(),
        'instrument': market_data.instrument,
        'open': float(market_data.open),
        'high': float(market_data.high),
        'low': float(market_data.low),
        'close': float(market_data.close),
        'volume': market_data.volume,
        'vwap': float(market_data.vwap) if market_data.vwap else None,
        'amount': float(market_data.amount) if market_data.amount else None,
        'factor': float(market_data.factor) if market_data.factor else None,
        'turnover': float(market_data.turnover) if market_data.turnover else None,
        'float_shares': market_data.float_shares
    }), 200

@routes_bp.route('/market-data/<symbol>/populate', methods=['POST'])
def populate_market_data(symbol):
    """
    Fetch today's market data from Yahoo Finance and store it in the database.
    
    POST /market-data/AAPL/populate
    
    Creates a new market_data record with today's OHLCV data plus calculated metrics.
    Factor is calculated as the cumulative split adjustment multiplier.
    """
    try:
        market_data = create_market_data_from_yahoo(symbol)
        
        db.session.add(market_data)
        db.session.commit()
        
        return jsonify({
            'message': f'Market data for {symbol.upper()} created successfully',
            'data': {
                'id': market_data.id,
                'datetime': market_data.datetime.isoformat(),
                'instrument': market_data.instrument,
                'open': float(market_data.open),
                'high': float(market_data.high),
                'low': float(market_data.low),
                'close': float(market_data.close),
                'volume': market_data.volume,
                'vwap': float(market_data.vwap) if market_data.vwap else None,
                'amount': float(market_data.amount) if market_data.amount else None,
                'factor': float(market_data.factor) if market_data.factor else None,
                'turnover': float(market_data.turnover) if market_data.turnover else None,
                'float_shares': market_data.float_shares
            }
        }), 201
        
    except ValueError as e:
        return jsonify({
            'error': f'Failed to populate market data for {symbol}',
            'details': str(e)
        }), 400
        
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'error': f'Failed to populate market data for {symbol}',
            'details': str(e)
        }), 500


@routes_bp.route('/load-stocks', methods=['POST'])
def load_stocks_endpoint():
    """
    Backfill local market_data from Yahoo Finance using symbols in the remote DB,
    then rebuild the Qlib dataset.
    After completion, run the scoring workflow, persist scores into the local DB,
    and optionally sync stocks/market_data/scores into MongoDB.
    """
    payload = request.get_json(silent=True) or {}

    source_db_url = payload.get("source_database_url") or os.getenv("DATABASE_URL")
    target_db_url = payload.get("target_database_url") or load_stocks.DEFAULT_SQLITE_URL
    provider_uri = payload.get("provider_uri") or os.getenv("QLIB_DATA_PATH")
    region = payload.get("region") or os.getenv("QLIB_REGION")
    cache_dir = payload.get("cache_dir") or os.getenv("QLIB_CACHE_DIR")
    skip_clean = bool(payload.get("skip_clean", False))
    skip_stocks = bool(payload.get("skip_stocks", False))
    skip_scores = bool(payload.get("skip_scores", False))
    skip_market = bool(payload.get("skip_market", False))
    limit_market_raw = payload.get("limit_market")
    try:
        limit_market = int(limit_market_raw) if limit_market_raw is not None else None
    except (TypeError, ValueError):
        limit_market = None
    try:
        days = int(payload.get("days", 365))
    except (TypeError, ValueError):
        days = 365

    # Allow overriding symbols via payload for targeted backfills/tests.
    symbols_override_raw = payload.get("symbols")
    symbols_override: list[str] = []
    if isinstance(symbols_override_raw, str):
        symbols_override = [s.strip().upper() for s in symbols_override_raw.split(",") if s.strip()]
    elif isinstance(symbols_override_raw, list):
        symbols_override = [str(s).upper() for s in symbols_override_raw if s]

    symbols_from_mongo: list[str] = []
    if _mongo_stocks_col is not None:
        try:
            symbols_from_mongo = [
                str(sym).upper()
                for sym in _mongo_stocks_col.distinct("_id")
                if sym
            ]
        except Exception:  # pragma: no cover - defensive guard
            current_app.logger.exception("Failed to fetch symbols from Mongo stocks collection")

    if not source_db_url and not (symbols_override or symbols_from_mongo):
        return jsonify({"error": "source_database_url is required or set DATABASE_URL"}), 400

    try:
        result = load_stocks.sync_remote_symbols_to_local(
            source_database_url=source_db_url,
            target_database_url=target_db_url,
            days=days,
            provider_uri=provider_uri,
            region=region,
            cache_dir=cache_dir,
            skip_clean=skip_clean,
            symbols=symbols_override or symbols_from_mongo or None,
        )

        ensure_summary = _ensure_stocks_in_target_db(result.get("symbols", []), target_db_url)
        print(f"load_stocks: ensured stocks in target DB {ensure_summary}", flush=True)

        scoring_summary = {"ok": False, "error": "No symbols to score"}
        if result.get("symbols"):
            try:
                dataset_meta = result.get("dataset") or {}
                scoring_overrides = {
                    "provider_uri": provider_uri or dataset_meta.get("provider_dir"),
                    "region": region,
                    "cache_dir": cache_dir,
                    "symbols": result.get("symbols"),
                }
                if dataset_meta.get("start_date"):
                    scoring_overrides["start_date"] = dataset_meta["start_date"]
                if dataset_meta.get("end_date"):
                    scoring_overrides["end_date"] = dataset_meta["end_date"]
                    # Leave a buffer so labels/segments have data; default to end_date - 20 days.
                    try:
                        end_dt = datetime.fromisoformat(dataset_meta["end_date"])
                        start_dt = (
                            datetime.fromisoformat(dataset_meta["start_date"])
                            if dataset_meta.get("start_date")
                            else end_dt
                        )
                        train_end_dt = max(start_dt, end_dt - timedelta(days=20))
                        scoring_overrides["train_end_date"] = train_end_dt.date().isoformat()
                    except Exception:
                        scoring_overrides["train_end_date"] = dataset_meta["end_date"]

                print("load_stocks: starting scoring", flush=True)
                scoring_config = load_config_from_env(scoring_overrides)
                scoring_output = run_scoring_workflow(scoring_config)
                print("load_stocks: scoring complete; persisting scores", flush=True)
                averaged_results = _average_scores_by_symbol(scoring_output.get("results", []) or [])
                _merge_score_metadata_from_db(averaged_results, target_db_url)
                store_summary = {}
                try:
                    store_summary = _persist_scores_to_db(
                        averaged_results,
                        database_url=target_db_url,
                        symbols=result.get("symbols"),
                    )
                    print("load_stocks: scores persisted", flush=True)
                except Exception as store_exc:
                    print(f"load_stocks: score persistence failed: {store_exc}", flush=True)
                    store_summary = {"ok": False, "error": str(store_exc)}
                results_full = averaged_results
                results_preview = results_full if len(results_full) <= 200 else results_full[:200]
                cleanup_note = None
                provider_dir = (scoring_output.get("config") or {}).get("provider_uri") or dataset_meta.get("provider_dir")
                if provider_dir and os.path.isdir(provider_dir):
                    try:
                        shutil.rmtree(provider_dir)
                        cleanup_note = f"Removed provider_dir {provider_dir}"
                    except Exception as cleanup_exc:  # pragma: no cover - defensive guard
                        cleanup_note = f"Cleanup failed for {provider_dir}: {cleanup_exc}"
                scoring_summary = {
                    "ok": True,
                    "results_count": len(results_full),
                    "results": results_preview,
                    "results_truncated": len(results_full) > len(results_preview),
                    "store_summary": store_summary,
                    "config": scoring_output.get("config"),
                    "cleanup": cleanup_note,
                }
            except Exception as exc:  # pragma: no cover - defensive guard
                current_app.logger.exception("Scoring after load-stocks failed")
                scoring_summary = {"ok": False, "error": str(exc)}

        result["scoring"] = scoring_summary
        # After the local sync finishes, push the data into MongoDB if configured.
        if _mongo_stocks_col and _mongo_market_data_col and _mongo_scores_col and not (skip_stocks and skip_scores and skip_market):
            try:
                print("load_stocks: starting mongo sync", flush=True)
                mongo_sync = _sync_db_to_mongo(
                    stock_db_url=source_db_url,
                    market_db_url=target_db_url,
                    score_db_url=target_db_url,
                    symbols=result.get("symbols"),
                    limit_market=limit_market,
                    skip_stocks=skip_stocks,
                    skip_scores=skip_scores,
                    skip_market=skip_market,
                )
                result["mongo_sync"] = mongo_sync
                print("load_stocks: mongo sync complete", flush=True)
            except Exception as exc:  # pragma: no cover - defensive guard
                current_app.logger.exception("Mongo sync after load-stocks failed")
                result["mongo_sync"] = {"error": str(exc)}
        elif skip_stocks and skip_scores and skip_market:
            result["mongo_sync"] = {"skipped": True}
        elif _mongo_stocks_col or _mongo_market_data_col or _mongo_scores_col:
            result["mongo_sync"] = {"error": "MongoDB not fully configured; set MONGO_URI"}

        return jsonify(result), 200
    except Exception as exc:  # pragma: no cover - defensive guard
        current_app.logger.exception("Load stocks endpoint failed")
        message = str(exc) or f"{exc.__class__.__name__} occurred"
        return jsonify({"error": message}), 500


@routes_bp.route('/sync-mongo', methods=['POST'])
def sync_mongo_endpoint():
    """
    Upsert stocks and market_data from SQL databases into MongoDB without backfilling.

    Request body (optional):
    {
      "source_database_url": "...",   # SQL DB for stocks (defaults to env DATABASE_URL)
      "market_database_url": "...",   # SQL DB for market_data (defaults to local SQLite)
      "target_database_url": "...",   # alias for market_database_url
      "score_database_url": "...",    # optional; defaults to source_database_url
      "symbols": ["AAPL", "MSFT"],    # optional symbol filter
      "limit_market": 5000,           # optional cap on market_data rows processed
      "skip_stocks": false,
      "skip_scores": false,
      "skip_market": false,
      "since": "2025-01-01T00:00:00Z",# optional datetime filter for market_data
      "dry_run": true                 # optional: parse/validate only
    }
    """
    payload = request.get_json(silent=True) or {}

    stock_db_url = payload.get("source_database_url") or os.getenv("DATABASE_URL")
    market_db_url = (
        payload.get("market_database_url")
        or payload.get("target_database_url")
        or load_stocks.DEFAULT_SQLITE_URL
    )
    score_db_url = payload.get("score_database_url") or stock_db_url
    symbols = payload.get("symbols")
    limit_market = payload.get("limit_market")
    skip_stocks = bool(payload.get("skip_stocks", False))
    skip_scores = bool(payload.get("skip_scores", False))
    skip_market = bool(payload.get("skip_market", False))
    since_raw = payload.get("since")
    since = None
    if since_raw:
        try:
            since = parse_iso_datetime(since_raw, "since")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    if not stock_db_url:
        return jsonify({"error": "source_database_url is required or set DATABASE_URL"}), 400
    if (
        (not skip_stocks and _mongo_stocks_col is None)
        or (not skip_scores and _mongo_scores_col is None)
        or (not skip_market and _mongo_market_data_col is None)
    ):
        return jsonify({"error": "MongoDB not configured; set MONGO_URI"}), 400

    try:
        limit_market_int = int(limit_market) if limit_market is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "limit_market must be an integer"}), 400

    if payload.get("dry_run"):
        return jsonify(
            {
                "ok": True,
                "dry_run": True,
                "stock_db_url": stock_db_url,
                "market_db_url": market_db_url,
                "score_db_url": score_db_url,
                "symbols": symbols,
                "limit_market": limit_market_int,
                "skip_stocks": skip_stocks,
                "skip_scores": skip_scores,
                "skip_market": skip_market,
                "since": since.isoformat() if since else None,
            }
        ), 200

    try:
        current_app.logger.info(
            "Starting sync-mongo",
            extra={
                "stock_db_url": stock_db_url,
                "market_db_url": market_db_url,
                "score_db_url": score_db_url,
                "symbols": symbols,
                "limit_market": limit_market_int,
                "skip_stocks": skip_stocks,
                "skip_scores": skip_scores,
                "skip_market": skip_market,
                "since": since.isoformat() if since else None,
            },
        )
        result = _sync_db_to_mongo(
            stock_db_url=stock_db_url,
            market_db_url=market_db_url,
            score_db_url=score_db_url,
            symbols=symbols,
            limit_market=limit_market_int,
            skip_stocks=skip_stocks,
            skip_scores=skip_scores,
            skip_market=skip_market,
            since=since,
        )
        try:
            dedupe_result = _dedupe_market_data(symbols)
            result["dedupe"] = dedupe_result
        except Exception as dedupe_exc:  # pragma: no cover - defensive guard
            current_app.logger.exception("Mongo dedupe failed")
            result["dedupe"] = {"error": str(dedupe_exc)}
        return jsonify(result), 200
    except Exception as exc:  # pragma: no cover - defensive guard
        current_app.logger.exception("Mongo sync endpoint failed")
        message = str(exc) or f"{exc.__class__.__name__} occurred"
        return jsonify({"error": message}), 500


@routes_bp.route('/mongo/dedupe', methods=['POST'])
def dedupe_mongo_endpoint():
    """
    Remove duplicate documents from a Mongo collection based on data fields (not _id).

    Request body:
    {
      "collection": "marketData" | "stocks" | "scores",
      "fields": ["instrument", "dateTime"]   # optional override of grouping fields
    }
    Defaults:
      - marketData: ["instrument", "dateTime"]
      - stocks: ["symbol", "company", "sector", "subSector", "country", "price", "quantity"]
      - scores: ["stockId", "score", "quantity", "volatility", "growth", "price1M", "price2M", "price3M", "price4M", "price5M", "price6M"]
    """
    payload = request.get_json(silent=True) or {}
    collection_name = (payload.get("collection") or "marketData").strip()
    fields = payload.get("fields")

    dedupe_mode = (payload.get("mode") or "full").lower()

    default_fields = {
        "marketData": [
            "instrument",
            "dateTime",
            "open",
            "high",
            "low",
            "close",
            "vwap",
            "volume",
            "amount",
            "factor",
            "turnover",
            "floatShares",
        ],
        "stocks": ["symbol", "company", "sector", "subSector", "country", "price", "quantity"],
        "scores": ["stockId", "score", "quantity", "volatility", "growth", "price1M", "price2M", "price3M", "price4M", "price5M", "price6M"],
    }
    default_partition = {
        "marketData": "instrument",
        "stocks": "symbol",
        "scores": "stockId",
    }

    if fields is not None and not isinstance(fields, list):
        return jsonify({"error": "fields must be a list of field names"}), 400

    collection_map = {
        "marketData": _mongo_market_data_col,
        "stocks": _mongo_stocks_col,
        "scores": _mongo_scores_col,
    }

    if collection_name not in collection_map:
        return jsonify({"error": "collection must be one of: marketData, stocks, scores"}), 400

    collection = collection_map[collection_name]
    if collection is None:
        return jsonify({"error": "MongoDB not configured; set MONGO_URI"}), 400

    if fields is None and collection_name == "marketData" and dedupe_mode == "keys":
        fields_to_use = ["instrument", "dateTime"]
    else:
        fields_to_use = fields or default_fields[collection_name]
    partition_field = default_partition.get(collection_name)
    if not fields_to_use:
        return jsonify({"error": "No fields provided to identify duplicates"}), 400

    try:
        result = _dedupe_collection_by_fields(collection, fields_to_use, partition_field=partition_field)
        result.update({"collection": collection_name, "fields": fields_to_use})
        return jsonify(result), 200
    except Exception as exc:  # pragma: no cover - defensive guard
        current_app.logger.exception("Mongo dedupe endpoint failed")
        return jsonify({"error": str(exc)}), 500


def _get_growth_map(stock_ids):
    if not stock_ids:
        return {}
    latest_score_subq = (
        select(Score.stock_id, func.max(Score.score_id).label("max_id"))
        .where(Score.stock_id.in_(stock_ids))
        .group_by(Score.stock_id)
        .subquery()
    )
    scores = db.session.execute(
        select(Score.stock_id, Score.growth)
        .join(latest_score_subq, Score.score_id == latest_score_subq.c.max_id)
    ).all()
    return {stock_id: float(growth or 0) for stock_id, growth in scores}


# =========================
#  ACCOUNT ROUTES
# =========================

@routes_bp.route('/accounts', methods=['GET'])
@jwt_required()
def get_user_account():
    """Get the account for the authenticated user with total holdings value"""
    user_id = get_jwt_identity()
    account = Account.query.filter_by(user_id=user_id).first()
    
    if not account:
        return jsonify({'error': 'Account not found'}), 404
    
    # Calculate total holdings value from MongoDB stock prices
    holdings = Holding.query.filter(Holding.user_id == user_id).all()
    total_holdings_value = 0.0
    
    for holding in holdings:
        stock = MongoStock.objects(stock_id=holding.stock_id).first()
        if stock and stock.price:
            total_holdings_value += float(stock.price) * holding.quantity
    
    response = jsonify({
        'user_id': account.user_id,
        'balance': total_holdings_value,  # Total value of holdings
        'account_balance': float(account.balance),  # Account balance (cash)
        'created_at': account.created_at.isoformat() if account.created_at else None,
        'updated_at': account.updated_at.isoformat() if account.updated_at else None
    })
    # Prevent caching to ensure fresh data after transactions
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response, 200


@routes_bp.route('/account/<int:user_id>', methods=['GET'])
@jwt_required()
def get_account_by_user_id(user_id):
    """Get account by user_id (with authorization check)"""
    requesting_user_id = int(get_jwt_identity())
    
    # Authorization: user can only access their own account
    if requesting_user_id != user_id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    account = Account.query.filter_by(user_id=user_id).first()
    
    if not account:
        return jsonify({'error': 'Account not found'}), 404
    
    # Calculate total holdings value from MongoDB stock prices
    holdings = Holding.query.filter(Holding.user_id == user_id).all()
    total_holdings_value = 0.0
    
    for holding in holdings:
        stock = MongoStock.objects(stock_id=holding.stock_id).first()
        if stock and stock.price:
            total_holdings_value += float(stock.price) * holding.quantity
    
    response = jsonify({
        'user_id': account.user_id,
        'balance': total_holdings_value,  # Total value of holdings
        'account_balance': float(account.balance),  # Account balance (cash)
        'created_at': account.created_at.isoformat() if account.created_at else None,
        'updated_at': account.updated_at.isoformat() if account.updated_at else None
    })
    # Prevent caching to ensure fresh data after transactions
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response, 200


@routes_bp.route('/portfolio/history/<int:user_id>', methods=['GET'])
@jwt_required()
def get_portfolio_history(user_id):
    """Get historical portfolio values from first transaction to today"""
    requesting_user_id = int(get_jwt_identity())
    
    # Authorization: user can only access their own portfolio history
    if requesting_user_id != user_id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    try:
        history = calculate_portfolio_history(user_id)
        response = jsonify(history)
        # Prevent caching to ensure fresh data after transactions
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response, 200
    except Exception as e:
        return jsonify({'error': f'Failed to calculate portfolio history: {str(e)}'}), 500


@routes_bp.route('/accounts', methods=['POST'])
@jwt_required()
def create_account():
    """Create an account for the authenticated user (only one account per user)"""
    user_id = get_jwt_identity()
    
    # Check if account already exists
    existing_account = Account.query.filter_by(user_id=user_id).first()
    if existing_account:
        return jsonify({'error': 'Account already exists for this user'}), 400
    
    data = request.get_json()
    account = Account(
        user_id=user_id,
        balance=data.get('balance', 0.00)
    )
    db.session.add(account)
    db.session.commit()
    
    return jsonify({
        'message': 'Account created successfully',
        'user_id': account.user_id,
        'balance': float(account.balance)
    }), 201


@routes_bp.route('/accounts', methods=['PUT'])
@jwt_required()
def update_account():
    """Update the account balance for the authenticated user"""
    user_id = get_jwt_identity()
    account = Account.query.filter_by(user_id=user_id).first()
    
    if not account:
        return jsonify({'error': 'Account not found'}), 404
    
    data = request.get_json()
    if 'balance' in data:
        account.balance = data['balance']
    
    db.session.commit()
    
    return jsonify({
        'message': 'Account updated successfully',
        'user_id': account.user_id,
        'balance': float(account.balance),
        'updated_at': account.updated_at.isoformat() if account.updated_at else None
    }), 200


# =========================
#  HOLDING ROUTES
# =========================

@routes_bp.route('/holdings', methods=['GET'])
@jwt_required()
def get_user_holdings():
    """Get all holdings for the authenticated user, sorted alphabetically by stock symbol"""
    user_id = get_jwt_identity()
    
    # Query holdings from MySQL
    holdings = Holding.query.filter(Holding.user_id == user_id).all()
    
    holdings_data = []
    for holding in holdings:
        # Fetch stock data from MongoDB
        stock = MongoStock.objects(stock_id=holding.stock_id).first()
        
        # Calculate average purchase price from transactions
        transactions = Transaction.query.filter(
            Transaction.user_id == user_id,
            Transaction.stock_id == holding.stock_id,
            Transaction.transaction_type == 'buy'
        ).all()
        
        total_cost = sum(float(t.price_transac) * t.quantity_transac for t in transactions)
        total_shares = sum(t.quantity_transac for t in transactions)
        avg_purchase_price = total_cost / total_shares if total_shares > 0 else 0
        
        # Calculate growth percentage
        current_price = float(stock.price) if stock and stock.price else 0
        growth_percent = ((current_price - avg_purchase_price) / avg_purchase_price * 100) if avg_purchase_price > 0 else 0
        
        holdings_data.append({
            'holding_id': holding.holding_id,
            'user_id': holding.user_id,
            'stock_id': holding.stock_id,
            'stock_symbol': stock.symbol if stock else None,
            'stock_company': stock.company if stock else None,
            'stock_price': current_price,
            'quantity': holding.quantity,
            'purchase_price': round(avg_purchase_price, 2),
            'growth_percent': round(growth_percent, 2),
            'updated_at': holding.updated_at.isoformat() if holding.updated_at else None
        })
    
    # Sort by stock symbol
    holdings_data.sort(key=lambda x: x['stock_symbol'] or '')
    
    response = jsonify(holdings_data)
    # Prevent caching to ensure fresh data after transactions
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response, 200


@routes_bp.route('/holdings/<int:user_id>', methods=['GET'])
@jwt_required()
def get_holdings_by_user_id(user_id):
    """Get holdings by user_id (with authorization check)"""
    requesting_user_id = int(get_jwt_identity())
    
    # Authorization: user can only access their own holdings
    if requesting_user_id != user_id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    # Query holdings from MySQL
    holdings = Holding.query.filter(Holding.user_id == user_id).all()
    
    holdings_data = []
    for holding in holdings:
        # Fetch stock data from MongoDB
        stock = MongoStock.objects(stock_id=holding.stock_id).first()
        
        # Calculate average purchase price from transactions
        transactions = Transaction.query.filter(
            Transaction.user_id == user_id,
            Transaction.stock_id == holding.stock_id,
            Transaction.transaction_type == 'buy'
        ).all()
        
        total_cost = sum(float(t.price_transac) * t.quantity_transac for t in transactions)
        total_shares = sum(t.quantity_transac for t in transactions)
        avg_purchase_price = total_cost / total_shares if total_shares > 0 else 0
        
        # Calculate growth percentage
        current_price = float(stock.price) if stock and stock.price else 0
        growth_percent = ((current_price - avg_purchase_price) / avg_purchase_price * 100) if avg_purchase_price > 0 else 0
        
        holdings_data.append({
            'holding_id': holding.holding_id,
            'user_id': holding.user_id,
            'stock_id': holding.stock_id,
            'stock_symbol': stock.symbol if stock else None,
            'stock_company': stock.company if stock else None,
            'stock_price': current_price,
            'quantity': holding.quantity,
            'purchase_price': round(avg_purchase_price, 2),
            'growth_percent': round(growth_percent, 2),
            'updated_at': holding.updated_at.isoformat() if holding.updated_at else None
        })
    
    # Sort by stock symbol
    holdings_data.sort(key=lambda x: x['stock_symbol'] or '')
    
    response = jsonify(holdings_data)
    # Prevent caching to ensure fresh data after transactions
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response, 200


@routes_bp.route('/holdings', methods=['POST'])
@jwt_required()
def create_or_update_holding():
    """Create or update a holding for the authenticated user"""
    user_id = get_jwt_identity()
    data = request.get_json()
    
    stock_id = data.get('stock_id')
    quantity = data.get('quantity', 0)
    
    if not stock_id:
        return jsonify({'error': 'stock_id is required'}), 400
    
    # Check if stock exists in MongoDB
    stock = MongoStock.objects(stock_id=stock_id).first()
    if not stock:
        return jsonify({'error': 'Stock not found'}), 404
    
    # Check if holding already exists
    holding = Holding.query.filter_by(user_id=user_id, stock_id=stock_id).first()
    
    if holding:
        # Update existing holding
        holding.quantity = quantity
        message = 'Holding updated successfully'
    else:
        # Create new holding
        holding = Holding(
            user_id=user_id,
            stock_id=stock_id,
            quantity=quantity
        )
        db.session.add(holding)
        message = 'Holding created successfully'
    
    db.session.commit()
    
    return jsonify({
        'message': message,
        'holding_id': holding.holding_id,
        'user_id': holding.user_id,
        'stock_id': holding.stock_id,
        'quantity': holding.quantity
    }), 201


@routes_bp.route('/holdings/<int:holding_id>', methods=['DELETE'])
@jwt_required()
def delete_holding(holding_id):
    """Delete a specific holding for the authenticated user"""
    user_id = get_jwt_identity()
    holding = Holding.query.filter_by(holding_id=holding_id, user_id=user_id).first()
    
    if not holding:
        return jsonify({'error': 'Holding not found'}), 404
    
    db.session.delete(holding)
    db.session.commit()
    
    return jsonify({'message': 'Holding deleted successfully'}), 200


@cron_bp.route("/cron/daily", methods=["POST"])
def cron_daily():
    """
    Endpoint triggered by Vercel Cron once per day.
    Uses a secret header for basic protection.
    """
    # Read header from request
    auth_header = request.headers.get("X-CRON-SECRET")

    # If secret exists in env, require match
    if CRON_SECRET and auth_header != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    # Run the job
    result = run_daily_job()

    return jsonify({"status": "ok", "result": result}), 200



def run_daily_job():
    """
    Daily job run by Vercel Cron.

    Place your real logic here.
    Example actions:
    - fetch stock data
    - update scores
    - clean old rows from database
    """
    now_utc = datetime.utcnow().isoformat()
    print(f"[CRON] Daily job started at {now_utc}")

    load_stocks_endpoint()

    print("[CRON] Daily job finished")
    return {"timestamp": now_utc}
