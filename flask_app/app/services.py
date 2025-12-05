from sqlalchemy import select
from .extensions import db
from decimal import Decimal
from .models import User, Stock, Score, Log, Transaction, Account, Holding, MarketData
from .mongo_models import Stock as MongoStock, MarketData as MongoMarketData
import yfinance as yf
from datetime import datetime, timezone

# USERS
def upsert_user(username: str, email: str, password_plain: str, balance=0.0, risk_averse='no') -> User:
    user = db.session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(username=username, email=email, balance=balance, risk_averse=risk_averse)
        user.set_password(password_plain)
        db.session.add(user)
    else:
        user.username = username
        user.balance = balance
        user.risk_averse = risk_averse
        user.set_password(password_plain)  # rotate/update hash as needed
    return user

# STOCKS
def upsert_stock(symbol: str, company: str, sector=None, sub_sector=None, country=None, price=0.0, quantity=0) -> Stock:
    stock = db.session.execute(select(Stock).where(Stock.symbol == symbol)).scalar_one_or_none()
    if stock is None:
        stock = Stock(symbol=symbol, company=company, sector=sector, sub_sector=sub_sector,
                      country=country, price=price, quantity=quantity)
        db.session.add(stock)
    else:
        stock.company = company
        stock.sector = sector
        stock.sub_sector = sub_sector
        stock.country = country
        stock.price = price
        stock.quantity = quantity
    return stock

# SCORES
def add_score_for_symbol(symbol: str, price=None, quantity=None, volatility=None, growth=None) -> Score:
    stock = db.session.execute(select(Stock).where(Stock.symbol == symbol)).scalar_one()
    score = Score(stock_id=stock.stock_id, price=price, quantity=quantity, volatility=volatility, growth=growth)
    db.session.add(score)
    return score

# LOGS
def add_log_for_email(email: str, action_type: str, details=None) -> Log:
    user = db.session.execute(select(User).where(User.email == email)).scalar_one()
    log = Log(user_id=user.user_id, action_type=action_type, details=details)
    db.session.add(log)
    return log

# TRANSACTIONS
def add_transaction(email: str, symbol: str, txn_type: str, qty: int, price: float) -> Transaction:
    user = db.session.execute(select(User).where(User.email == email)).scalar_one()
    stock = db.session.execute(select(Stock).where(Stock.symbol == symbol)).scalar_one()
    txn = Transaction(user_id=user.user_id, stock_id=stock.stock_id, transaction_type=txn_type,
                      quantity_transac=qty, price_transac=price)
    db.session.add(txn)
    return txn


def process_transaction(user_id: int, stock_id: int, txn_type: str, qty: int) -> Transaction:
    """
    Process a complete transaction: validate, update account, update holdings, and record transaction.
    Uses current stock price from MongoDB.
    
    Args:
        user_id: ID of the user making the transaction
        stock_id: ID of the stock being traded (MySQL stock_id)
        txn_type: 'buy' or 'sell'
        qty: Quantity of shares
    
    Returns:
        Transaction object
    
    Raises:
        ValueError: If validation fails (insufficient funds/holdings/stock not found)
    """
    # Get stock price from MongoDB using stockId
    try:
        stock = MongoStock.objects(stock_id=stock_id).first()
    except Exception as e:
        raise ValueError(f"Error querying MongoDB for stock_id {stock_id}: {str(e)}")
    
    if not stock:
        raise ValueError(f"Stock with ID {stock_id} not found in MongoDB")
    
    if not stock.price or stock.price <= 0:
        raise ValueError(f"Stock {stock.symbol} does not have a valid price")
    
    price = Decimal(str(stock.price))
    total_cost = price * Decimal(qty)
    
    # Get or create account
    account = db.session.execute(
        select(Account).where(Account.user_id == user_id)
    ).scalar_one_or_none()
    
    if not account:
        raise ValueError("Account not found. Please create an account first.")
    
    # Ensure balance is Decimal (handle legacy float values)
    if not isinstance(account.balance, Decimal):
        account.balance = Decimal(str(account.balance))
    
    # Validate and update based on transaction type
    if txn_type == 'buy':
        # Check if user has enough balance
        if account.balance < total_cost:
            raise ValueError(f"Insufficient funds. Balance: {account.balance}, Required: {total_cost}")
        
        # Deduct from account balance
        account.balance -= total_cost
        
        # Update or create holding
        holding = db.session.execute(
            select(Holding).where(
                Holding.user_id == user_id,
                Holding.stock_id == stock_id
            )
        ).scalar_one_or_none()
        
        if holding:
            holding.quantity += qty
        else:
            holding = Holding(user_id=user_id, stock_id=stock_id, quantity=qty)
            db.session.add(holding)
    
    elif txn_type == 'sell':
        # Check if user has enough holdings
        holding = db.session.execute(
            select(Holding).where(
                Holding.user_id == user_id,
                Holding.stock_id == stock_id
            )
        ).scalar_one_or_none()
        
        if not holding or holding.quantity < qty:
            current_qty = holding.quantity if holding else 0
            raise ValueError(f"Insufficient holdings. You have {current_qty} shares, trying to sell {qty}")
        
        # Update holding quantity
        holding.quantity -= qty
        
        # Remove holding if quantity becomes 0
        if holding.quantity == 0:
            db.session.delete(holding)
        
        # Add to account balance
        account.balance += total_cost
    
    else:
        raise ValueError(f"Invalid transaction type: {txn_type}. Must be 'buy' or 'sell'")
    
    # Create transaction record
    txn = Transaction(
        user_id=user_id,
        stock_id=stock_id,
        transaction_type=txn_type,
        quantity_transac=qty,
        price_transac=price
    )
    db.session.add(txn)
    
    return txn

# SEARCH STOCKS
def search_stocks(text: str, country=None, min_price: float = 0, max_price: float = 0, 
                  sector=None, sub_sector=None) -> list[Stock]:
    """
    Search stocks based on text and optional filters.
    Returns first 10 matching results.
    
    Args:
        text: Search text for symbol or company name
        country: Single country string, list of countries, or None
        min_price: Minimum price filter
        max_price: Maximum price filter
        sector: Single sector string, list of sectors, or None
        sub_sector: Single sub_sector string, list of sub_sectors, or None
    """
    query = select(Stock)
    
    # Search text in Symbol or Company (case-insensitive)
    if text:
        search_pattern = f"%{text}%"
        query = query.where(
            db.or_(
                Stock.symbol.ilike(search_pattern),
                Stock.company.ilike(search_pattern)
            )
        )
    
    # Apply country filter - handles both single value and list
    if country:
        if isinstance(country, list) and len(country) > 0:
            query = query.where(Stock.country.in_(country))
        elif isinstance(country, str) and country:
            query = query.where(Stock.country == country)
    
    # Apply price range filter if both min and max are not 0
    if min_price > 0 or max_price > 0:
        if min_price > 0:
            query = query.where(Stock.price >= min_price)
        if max_price > 0:
            query = query.where(Stock.price <= max_price)
    
    # Apply sector filter - handles both single value and list
    if sector:
        if isinstance(sector, list) and len(sector) > 0:
            query = query.where(Stock.sector.in_(sector))
        elif isinstance(sector, str) and sector:
            query = query.where(Stock.sector == sector)
    
    # Apply sub_sector filter - handles both single value and list
    if sub_sector:
        if isinstance(sub_sector, list) and len(sub_sector) > 0:
            query = query.where(Stock.sub_sector.in_(sub_sector))
        elif isinstance(sub_sector, str) and sub_sector:
            query = query.where(Stock.sub_sector == sub_sector)
    
    # Limit to 10 results
    query = query.limit(10)
    
    results = db.session.execute(query).scalars().all()
    return results

# MARKET DATA
def create_market_data_from_yahoo(symbol: str) -> MarketData:
    ticker = yf.Ticker(symbol.upper())
    info = ticker.info
    
    high = info.get('regularMarketDayHigh') or info.get('dayHigh')
    low = info.get('regularMarketDayLow') or info.get('dayLow')
    close = info.get('regularMarketPrice') or info.get('currentPrice')
    open_price = info.get('regularMarketOpen') or info.get('open')
    volume = info.get('regularMarketVolume') or info.get('volume')
    
    if not all([high, low, close, open_price, volume]):
        raise ValueError(f"Missing required OHLCV data for {symbol}")
    
    vwap = (high + low + close) / 3
    amount = volume * vwap
    float_shares = info.get('floatShares') or info.get('sharesOutstanding')
    turnover = (volume / float_shares) if float_shares else None
    
    factor = 1.0
    try:
        splits = ticker.splits
        if not splits.empty:
            # Calculate cumulative product of all split ratios
            for split_ratio in splits:
                factor *= split_ratio
    except Exception:
        factor = 1.0
    
    market_data = MarketData(
        datetime=datetime.now(timezone.utc),
        instrument=symbol.upper(),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        vwap=vwap,
        amount=amount,
        factor=factor,
        turnover=turnover,
        float_shares=float_shares
    )
    
    return market_data
