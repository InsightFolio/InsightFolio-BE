from flask import Blueprint, request, jsonify
from . import db, jwt
from .models import User, Stock
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