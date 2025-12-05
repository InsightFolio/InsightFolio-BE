import os
from datetime import datetime, timezone

import yfinance as yf
from pymongo import UpdateOne
from flask import Blueprint, jsonify, current_app, request
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required
from pymongo import MongoClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from contextlib import contextmanager

# Prefer absolute import when app is run from repo root; fall back to local module when running inside flask_app.
try:
    from flask_app import load_stocks
except ImportError:  # pragma: no cover - runtime safety
    import load_stocks  # type: ignore

from . import db, jwt
from .models import Account, Holding, MarketData, Score, Stock, Transaction, User
from .scoring import QlibConfig, load_config_from_env, run_scoring_workflow
from .seed import seed_database
from .services import (
    add_transaction,
    create_market_data_from_yahoo,
    process_transaction,
    search_stocks,
    upsert_user,
)


routes_bp = Blueprint('routes', __name__)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "marketdb")
try:
    _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000) if MONGO_URI else None
    _mongo_db = _mongo_client[MONGO_DB_NAME] if _mongo_client else None
    _mongo_stocks_col = _mongo_db["stocks"] if _mongo_db else None
    _mongo_market_data_col = _mongo_db["marketData"] if _mongo_db else None
except Exception:
    _mongo_client = None
    _mongo_db = None
    _mongo_stocks_col = None
    _mongo_market_data_col = None


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
    symbols: list[str] | None = None,
    limit_market: int | None = None,
    skip_stocks: bool = False,
    skip_market: bool = False,
    since: datetime | None = None,
) -> dict:
    """
    Copy stocks and market_data from SQL databases into MongoDB collections.
    Stock metadata is read from stock_db_url (remote prod), while market data
    is read from market_db_url (local backfilled SQLite).
    """
    if _mongo_stocks_col is None or _mongo_market_data_col is None:
        raise RuntimeError("MongoDB not configured; set MONGO_URI")

    synced_stocks = 0
    synced_market_data = 0
    symbol_filter = {s.upper() for s in symbols} if symbols else None

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

    if not skip_market:
        with _session_scope_for_sync(market_db_url) as session:
            market_query = select(MarketData).execution_options(stream_results=True)
            if symbol_filter:
                market_query = market_query.where(MarketData.instrument.in_(symbol_filter))
            if since is not None:
                market_query = market_query.where(MarketData.datetime >= since)
            market_stream = session.execute(market_query).scalars().yield_per(1000)

            market_ops: list[UpdateOne] = []
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
                market_ops.append(
                    UpdateOne(
                        {"instrument": md.instrument, "dateTime": md.datetime},
                        {"$set": doc},
                        upsert=True,
                    )
                )
                synced_market_data += 1
                if limit_market is not None and synced_market_data >= limit_market:
                    break
                if len(market_ops) >= 500:
                    _mongo_market_data_col.bulk_write(market_ops, ordered=False)
                    market_ops.clear()
            if market_ops:
                _mongo_market_data_col.bulk_write(market_ops, ordered=False)

    return {
        "stocks_synced": synced_stocks,
        "market_data_synced": synced_market_data,
        "backend": "mongo",
        "stock_db": stock_db_url,
        "market_db": market_db_url,
        "symbols_filtered": sorted(symbol_filter) if symbol_filter else None,
        "limit_market": limit_market,
        "skip_stocks": skip_stocks,
        "skip_market": skip_market,
        "since": since.isoformat() if since else None,
    }


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


@routes_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@routes_bp.route('/signup', methods=['POST'])
def signup():
    data = request.get_json()
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email already registered'}), 400
    user = User(username=data['username'], email=data['email'])
    user.set_password(data['password'])
    db.session.add(user)
    db.session.commit()
    return jsonify({'message': 'User created successfully'}), 201

@routes_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    user = User.query.filter_by(username=data['username']).first() or User.query.filter_by(email=data['username']).first()
    if user and user.check_password(data['password']):
        token = create_access_token(identity=str(user.user_id))
        return jsonify({'token': token}), 200
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
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Transaction failed: {str(e)}'}), 500



@routes_bp.route('/transactions', methods=['GET'])
@jwt_required()
def get_user_transactions():
    """Get all transactions for the authenticated user, sorted by date (newest first)"""
    user_id = get_jwt_identity()
    
    # Query transactions with joined stock data, filter by user_id, sort by date descending
    transactions = db.session.query(Transaction, Stock)\
        .join(Stock, Transaction.stock_id == Stock.stock_id)\
        .filter(Transaction.user_id == user_id)\
        .order_by(Transaction.date_transac.desc())\
        .all()
    
    transactions_data = []
    for transaction, stock in transactions:
        transactions_data.append({
            'transaction_id': transaction.transaction_id,
            'user_id': transaction.user_id,
            'stock_id': transaction.stock_id,
            'stock_symbol': stock.symbol,
            'stock_company': stock.company,
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
    
    stock = Stock.query.get(transaction.stock_id)
    
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
    Return a fixed list of 10 scored stocks (optionally limited via ?limit=10).
    Uses DB values when present; falls back to placeholders otherwise.
    """
    score_list = [
        ("AAPL", "Apple Inc."),
        ("MSFT", "Microsoft Corporation"),
        ("GOOGL", "Alphabet Inc."),
        ("AMZN", "Amazon.com, Inc."),
        ("META", "Meta Platforms, Inc."),
        ("NVDA", "NVIDIA Corporation"),
        ("TSLA", "Tesla, Inc."),
        ("NFLX", "Netflix, Inc."),
        ("JPM", "JPMorgan Chase & Co."),
        ("V", "Visa Inc."),
    ]

    try:
        limit = int(request.args.get("limit", 10))
    except ValueError:
        limit = 10
    limit = max(0, limit)
    if limit == 0:
        return jsonify([]), 200

    selected = score_list[:limit]
    symbols = [s[0] for s in selected]
    db_results = db.session.execute(
        select(Stock).where(Stock.symbol.in_(symbols))
    ).scalars().all()
    existing_map = {stock.symbol: stock for stock in db_results}
    growth_map = _get_growth_map([s.stock_id for s in db_results])

    response = []
    for symbol, company in selected:
        stock = existing_map.get(symbol)
        if stock:
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
                'change': change_abs,
                'change_pct': change_pct,
            })
        else:
            response.append({
                'stock_id': None,
                'symbol': symbol,
                'company': company,
                'sector': None,
                'sub_sector': None,
                'country': None,
                'price': 0.0,
                'quantity': 0,
                'last_updated': None,
                'change': 0.0,
                'change_pct': 0.0,
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
    After completion, optionally sync the resulting stocks/market_data into MongoDB.
    """
    payload = request.get_json(silent=True) or {}

    source_db_url = payload.get("source_database_url") or os.getenv("DATABASE_URL")
    target_db_url = payload.get("target_database_url") or load_stocks.DEFAULT_SQLITE_URL
    provider_uri = payload.get("provider_uri") or os.getenv("QLIB_DATA_PATH")
    region = payload.get("region") or os.getenv("QLIB_REGION")
    cache_dir = payload.get("cache_dir") or os.getenv("QLIB_CACHE_DIR")
    skip_clean = bool(payload.get("skip_clean", False))
    try:
        days = int(payload.get("days", 365))
    except (TypeError, ValueError):
        days = 365

    if not source_db_url:
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
        )
        # After the local sync finishes, push the data into MongoDB if configured.
        if _mongo_stocks_col and _mongo_market_data_col:
            try:
                mongo_sync = _sync_db_to_mongo(source_db_url, target_db_url)
                result["mongo_sync"] = mongo_sync
            except Exception as exc:  # pragma: no cover - defensive guard
                current_app.logger.exception("Mongo sync after load-stocks failed")
                result["mongo_sync"] = {"error": str(exc)}

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
      "symbols": ["AAPL", "MSFT"],    # optional symbol filter
      "limit_market": 5000,           # optional cap on market_data rows processed
      "skip_stocks": false,
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
    symbols = payload.get("symbols")
    limit_market = payload.get("limit_market")
    skip_stocks = bool(payload.get("skip_stocks", False))
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
    if _mongo_stocks_col is None or _mongo_market_data_col is None:
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
                "symbols": symbols,
                "limit_market": limit_market_int,
                "skip_stocks": skip_stocks,
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
                "symbols": symbols,
                "limit_market": limit_market_int,
                "skip_stocks": skip_stocks,
                "skip_market": skip_market,
                "since": since.isoformat() if since else None,
            },
        )
        result = _sync_db_to_mongo(
            stock_db_url=stock_db_url,
            market_db_url=market_db_url,
            symbols=symbols,
            limit_market=limit_market_int,
            skip_stocks=skip_stocks,
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
    """Get the account for the authenticated user"""
    user_id = get_jwt_identity()
    account = Account.query.filter_by(user_id=user_id).first()
    
    if not account:
        return jsonify({'error': 'Account not found'}), 404
    
    return jsonify({
        'account_id': account.account_id,
        'user_id': account.user_id,
        'balance': float(account.balance),
        'created_at': account.created_at.isoformat() if account.created_at else None,
        'updated_at': account.updated_at.isoformat() if account.updated_at else None
    }), 200


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
        'account_id': account.account_id,
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
        'account_id': account.account_id,
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
    
    # Query holdings with joined stock data, filter by user_id, sort by stock symbol
    holdings = db.session.query(Holding, Stock)\
        .join(Stock, Holding.stock_id == Stock.stock_id)\
        .filter(Holding.user_id == user_id)\
        .order_by(Stock.symbol)\
        .all()
    
    holdings_data = []
    for holding, stock in holdings:
        holdings_data.append({
            'holding_id': holding.holding_id,
            'user_id': holding.user_id,
            'stock_id': holding.stock_id,
            'stock_symbol': stock.symbol,
            'stock_company': stock.company,
            'stock_price': float(stock.price) if stock.price else None,
            'quantity': holding.quantity,
            'updated_at': holding.updated_at.isoformat() if holding.updated_at else None
        })
    
    return jsonify(holdings_data), 200


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
    
    # Check if stock exists
    stock = Stock.query.get(stock_id)
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
