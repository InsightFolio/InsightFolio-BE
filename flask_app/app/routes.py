from flask import Blueprint, request, jsonify
from sqlalchemy import select, case
from . import db, jwt
from .models import User, Stock, MarketData
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from .services import upsert_user, upsert_stock, add_transaction, search_stocks, create_market_data_from_yahoo
from .seed import seed_database
import yfinance as yf
from datetime import datetime, timezone

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


