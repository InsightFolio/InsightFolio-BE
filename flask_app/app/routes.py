from flask import Blueprint, request, jsonify
from sqlalchemy import select, case
from . import db, jwt
from .models import User, Stock, Account, Holding
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from .services import upsert_user, upsert_stock, add_transaction, search_stocks
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

@routes_bp.route('/accounts', methods=['POST'])
def create_account():
    """Create an account for a user. Body: { "user_id": int, "balance": "decimal" (optional) }"""
    data = request.get_json() or {}
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "user not found"}), 404

    existing = db.session.execute(db.select(Account).filter_by(user_id=user_id)).scalar_one_or_none()
    if existing:
        return jsonify({"error": "account already exists for user"}), 409

    balance = data.get('balance', 0)
    account = Account(user_id=user_id, balance=balance)
    db.session.add(account)
    db.session.commit()

    return jsonify({
        "account_id": account.account_id,
        "user_id": account.user_id,
        "balance": str(account.balance),
        "created_at": account.created_at.isoformat()
    }), 201


@routes_bp.route('/users/<int:user_id>/account', methods=['GET'])
def get_account(user_id):
    acct = db.session.execute(db.select(Account).filter_by(user_id=user_id)).scalar_one_or_none()
    if not acct:
        return jsonify({"error": "account not found"}), 404
    return jsonify({
        "account_id": acct.account_id,
        "user_id": acct.user_id,
        "balance": str(acct.balance),
        "created_at": acct.created_at.isoformat(),
        "updated_at": acct.updated_at.isoformat() if acct.updated_at else None
    })


@routes_bp.route('/users/<int:user_id>/portfolio', methods=['GET'])
def get_portfolio(user_id):
    """Return all holdings for a user (with basic stock info)."""
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "user not found"}), 404

    stmt = db.select(Holding, Stock).join(Stock, Holding.stock_id == Stock.stock_id).filter(Holding.user_id == user_id)
    rows = db.session.execute(stmt).all()

    holdings = []
    for holding, stock in rows:
        holdings.append({
            "holding_id": holding.holding_id,
            "user_id": holding.user_id,
            "stock_id": holding.stock_id,
            "symbol": stock.symbol,
            "company": stock.company,
            "quantity": int(holding.quantity),
            "updated_at": holding.updated_at.isoformat() if holding.updated_at else None
        })

    return jsonify({"holdings": holdings})


@routes_bp.route('/holdings', methods=['POST'])
def upsert_holding():
    """
    Upsert a holding.
    Body: { "user_id": int, "stock_id": int, "quantity": int }
    If the holding exists, it will be updated to the provided quantity (replace).
    """
    data = request.get_json() or {}
    user_id = data.get('user_id')
    stock_id = data.get('stock_id')
    quantity = data.get('quantity')

    if user_id is None or stock_id is None or quantity is None:
        return jsonify({"error": "user_id, stock_id, quantity are required"}), 400

    user = db.session.get(User, user_id)
    stock = db.session.get(Stock, stock_id)
    if not user or not stock:
        return jsonify({"error": "user or stock not found"}), 404

    stmt = db.select(Holding).filter_by(user_id=user_id, stock_id=stock_id)
    holding = db.session.execute(stmt).scalar_one_or_none()

    if holding:
        holding.quantity = int(quantity)
        holding.updated_at = datetime.utcnow()
    else:
        holding = Holding(user_id=user_id, stock_id=stock_id, quantity=int(quantity))
        db.session.add(holding)

    db.session.commit()

    return jsonify({
        "holding_id": holding.holding_id,
        "user_id": holding.user_id,
        "stock_id": holding.stock_id,
        "quantity": int(holding.quantity),
        "updated_at": holding.updated_at.isoformat() if holding.updated_at else None
    }), 200

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
