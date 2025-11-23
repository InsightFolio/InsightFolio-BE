from flask import Blueprint, request, jsonify
from sqlalchemy import select, func
from . import db, jwt
from .models import User, Stock, Account, Holding, Transaction, Score
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from .services import upsert_user, upsert_stock, add_transaction, search_stocks
from .seed import seed_database

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
        result.append({
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
            'last_updated': stock.last_updated.isoformat() if stock.last_updated else None,
            'change': change_abs,
            'change_pct': change_pct,
        })
    return result

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
