from flask import Blueprint, request, jsonify
from sqlalchemy import select, case
from . import db, jwt
from .services import upsert_user, upsert_stock, add_transaction, search_stocks, process_holding_update
from .models import User, Stock, Holding, Account, Transaction
from flask_jwt_extended import (
    create_access_token,
    jwt_required,
    get_jwt_identity,
    verify_jwt_in_request,
)
from .seed import seed_database
from datetime import datetime

routes_bp = Blueprint('routes', __name__)

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
    data = request.get_json()
    s = upsert_stock(
        symbol=data["symbol"],
        company=data["company"],
        sector=data.get("sector"),
        sub_sector=data.get("sub_sector"),
        country=data.get("country"),
        price=data.get("price", 0.0),
        quantity=data.get("quantity", 0),
    )
    db.session.commit()
    return jsonify({"stock_id": s.stock_id}), 201

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
    
@routes_bp.route('/seed_database', methods=['POST'])
def seed_database_endpoint():
    result = seed_database()
    return result, 201

@routes_bp.route('/popular', methods=['GET'])
def popular_stocks():
    """
    Return the curated popular stocks (optionally limited via ?limit=6).
    Uses DB values when present; falls back to the curated defaults when missing.
    """
    popular_list = [
        ("NVDA", "NVIDIA Corporation"),
        ("ONDS", "Ondas Holdings Inc."),
        ("TSLA", "Tesla, Inc."),
        ("OPEN", "Opendoor Technologies, Inc."),
        ("PLUG", "Plug Power Inc."),
        ("SOFI", "SoFi Technologies, Inc."),
    ]

    try:
        limit = int(request.args.get("limit", 6))
    except ValueError:
        limit = 6
    limit = max(0, limit)
    if limit == 0:
        return jsonify([]), 200

    selected = popular_list[:limit]
    symbols = [s[0] for s in selected]

    # Pull curated symbols (any price), preserving curated order.
    ordering = case({sym: idx for idx, sym in enumerate(symbols)}, value=Stock.symbol, else_=len(symbols))
    db_results = db.session.execute(
        select(Stock).where(Stock.symbol.in_(symbols)).order_by(ordering)
    ).scalars().all()
    existing_map = {stock.symbol: stock for stock in db_results}

    response = []
    for symbol, company in selected:
        stock = existing_map.get(symbol)
        if stock:
            response.append({
                'stock_id': stock.stock_id,
                'symbol': stock.symbol,
                'company': stock.company,
                'sector': stock.sector,
                'sub_sector': stock.sub_sector,
                'country': stock.country,
                'price': float(stock.price or 0),
                'quantity': stock.quantity,
                'last_updated': stock.last_updated.isoformat() if stock.last_updated else None
            })
        else:
            # Fallback to curated default when DB row is missing
            response.append({
                'stock_id': None,
                'symbol': symbol,
                'company': company,
                'sector': None,
                'sub_sector': None,
                'country': None,
                'price': 0.0,
                'quantity': 0,
                'last_updated': None
            })

    return jsonify(response), 200

@routes_bp.route('/api/stocks/search', methods=['GET'])
@routes_bp.route('/search', methods=['POST'])
def search_stocks_endpoint():
    """
    Search stocks based on text and optional filters.
    Request body:
    {
        "text": "",
        "filters": {
            "country": "",
            "min_price": 0,
            "max_price": 100,
            "sector": "",
            "sub_sector": ""
        }
    }
    """
    data = request.get_json()
    
    text = data.get('text', '')
    filters = data.get('filters', {})
    
    country = filters.get('country', '')
    min_price = float(filters.get('min_price', 0))
    max_price = float(filters.get('max_price', 0))
    sector = filters.get('sector', '')
    sub_sector = filters.get('sub_sector', '')
    
    results = search_stocks(
        text=text,
        country=country,
        min_price=min_price,
        max_price=max_price,
        sector=sector,
        sub_sector=sub_sector
    )
    
    # Convert results to JSON-serializable format
    stocks_data = [
        {
            'stock_id': stock.stock_id,
            'symbol': stock.symbol,
            'company': stock.company,
            'sector': stock.sector,
            'sub_sector': stock.sub_sector,
            'country': stock.country,
            'price': float(stock.price),
            'quantity': stock.quantity,
            'last_updated': stock.last_updated.isoformat() if stock.last_updated else None
        }
        for stock in results
    ]
    
    return jsonify(stocks_data), 200

def _stocks_to_json(stocks):
    return [
        {
            'stock_id': stock.stock_id,
            'symbol': stock.symbol,
            'company': stock.company,
            'sector': stock.sector,
            'sub_sector': stock.sub_sector,
            'country': stock.country,
            'price': float(stock.price),
            'quantity': stock.quantity,
            'last_updated': stock.last_updated.isoformat() if stock.last_updated else None
        }
        for stock in stocks
    ]

def _get_request_user_id():
    """
    Resolve user_id from query/body or optional JWT.
    Returns int or None.
    """
    # try query param
    user_id = request.args.get('user_id', type=int)
    if user_id:
        return user_id

    # try JSON body
    try:
        j = request.get_json(silent=True) or {}
    except Exception:
        j = {}
    body_user = j.get('user_id') if isinstance(j, dict) else None
    if body_user:
        try:
            return int(body_user)
        except Exception:
            pass

    # try optional JWT identity
    try:
        # optional=True allows requests without token
        verify_jwt_in_request(optional=True)
        ident = get_jwt_identity()
        if ident:
            try:
                return int(ident)
            except Exception:
                return None
    except Exception:
        # no valid JWT present
        return None

    return None

@routes_bp.route('/holdings', methods=['GET'])
def get_holdings_compat():
    """
    Compatibility endpoint for frontend:
    GET /holdings?user_id=1  or use Authorization Bearer <token>
    Returns holdings list with fields frontend expects: shares, symbol, company, value, growthPercent.
    """
    user_id = _get_request_user_id()
    if not user_id:
        return jsonify({"error": "user_id required (query/body) or provide Authorization token"}), 400

    stmt = db.select(Holding, Stock).join(Stock, Holding.stock_id == Stock.stock_id).filter(Holding.user_id == user_id)
    rows = db.session.execute(stmt).all()

    holdings = []
    for holding, stock in rows:
        shares = int(holding.quantity)
        price = float(stock.price) if stock.price is not None else None
        value = round(shares * price, 2) if price is not None else None
        holdings.append({
            "holding_id": holding.holding_id,
            "user_id": holding.user_id,
            "stock_id": holding.stock_id,
            "symbol": stock.symbol,
            "company": stock.company,
            "shares": shares,                    
            "quantity": shares,                   
            "value": value,
            "growthPercent": None,
            "updated_at": holding.updated_at.isoformat() if holding.updated_at else None
        })

    return jsonify({"holdings": holdings}), 200


@routes_bp.route('/holdings', methods=['POST'])
def upsert_holding_compat():
    """
    Compatibility upsert for frontend.
    Accepts either:
      - { user_id, stock_id, quantity } or
      - { user_id, symbol, company, quantity } or { shares } instead of quantity
    If user_id not provided, will try JWT identity.
    """
    data = request.get_json() or {}
    user_id = data.get('user_id') or _get_request_user_id()
    stock_id = data.get('stock_id')
    symbol = data.get('symbol')
    company = data.get('company')
    quantity = data.get('quantity') if data.get('quantity') is not None else data.get('shares')

    if user_id is None or quantity is None or (stock_id is None and not symbol):
        return jsonify({"error": "user_id, (stock_id or symbol), and quantity/shares required"}), 400

    # ensure user exists
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "user not found"}), 404

    # resolve stock_id if only symbol provided (create minimal stock row if missing)
    stock = None
    if stock_id is None:
        stock = db.session.execute(db.select(Stock).filter_by(symbol=symbol)).scalar_one_or_none()
        if not stock:
            stock = Stock(symbol=symbol, company=company or symbol, price=0.0, quantity=0)
            db.session.add(stock)
            db.session.flush()  # populate stock.stock_id
        stock_id = stock.stock_id
    else:
        stock = db.session.get(Stock, stock_id)
        if not stock:
            return jsonify({"error": "stock not found"}), 404

    # Use the service to process buy/sell and update account/transaction atomically
    try:
        result = process_holding_update(user_id=int(user_id), stock_id=int(stock_id), new_quantity=int(quantity))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        # unexpected
        return jsonify({"error": "internal error"}), 500

    holding = result.get("holding")
    account = result.get("account")
    txns = result.get("transactions", [])

    # compute value if possible
    price = float(stock.price) if stock.price is not None else None
    value = round((int(holding.quantity) * price), 2) if (holding and price is not None) else None

    return jsonify({
        "holding_id": holding.holding_id if holding else None,
        "user_id": user_id,
        "stock_id": stock_id,
        "symbol": stock.symbol,
        "company": stock.company,
        "shares": int(holding.quantity) if holding else 0,
        "quantity": int(holding.quantity) if holding else 0,
        "value": value,
        "growthPercent": None,
        "updated_at": holding.updated_at.isoformat() if holding and holding.updated_at else None,
        "account_balance": str(account.balance)
    }), 200


@routes_bp.route('/holdings/<int:holding_id>', methods=['DELETE'])
def delete_holding_by_id(holding_id):
    """
    Delete holding by id: perform a sell-all via process_holding_update so
    Transactions and Account balance are updated atomically.
    """
    holding = db.session.get(Holding, holding_id)
    if not holding:
        return jsonify({"error": "holding not found"}), 404

    try:
        result = process_holding_update(user_id=holding.user_id, stock_id=holding.stock_id, new_quantity=0)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "internal error"}), 500

    account = result.get("account")
    return jsonify({"ok": True, "account_balance": str(account.balance)}), 200


@routes_bp.route('/holdings', methods=['DELETE'])
def delete_holding_by_symbol():
    """
    DELETE /holdings?symbol=XYZ&user_id=1
    Compatibility: delete user's holding by symbol.
    """
    symbol = request.args.get('symbol')
    user_id = request.args.get('user_id', type=int) or _get_request_user_id()
    if not symbol or not user_id:
        return jsonify({"error": "symbol and user_id required"}), 400

    stock = db.session.execute(db.select(Stock).filter_by(symbol=symbol)).scalar_one_or_none()
    if not stock:
        return jsonify({"error": "stock not found"}), 404

    holding = db.session.execute(db.select(Holding).filter_by(user_id=user_id, stock_id=stock.stock_id)).scalar_one_or_none()
    if not holding:
        return jsonify({"error": "holding not found"}), 404

    try:
        result = process_holding_update(user_id=user_id, stock_id=stock.stock_id, new_quantity=0)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "internal error"}), 500

    account = result.get("account")
    return jsonify({"ok": True, "account_balance": str(account.balance)}), 200
