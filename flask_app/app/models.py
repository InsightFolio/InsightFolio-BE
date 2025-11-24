from .extensions import db, bcrypt
from datetime import datetime, timezone
from sqlalchemy import Enum

# =========================
#  MODEL: USERS
# =========================
class User(db.Model):
    __tablename__ = 'users'
    
    user_id = db.Column('UserID', db.Integer, primary_key=True, autoincrement=True)
    username = db.Column('Username', db.String(50), nullable=False)
    email = db.Column('Email', db.String(100), unique=True, nullable=False)
    password = db.Column('Password', db.String(255), nullable=False)
<<<<<<< Updated upstream
    balance = db.Column('Balance', db.Numeric(15, 2), default=0.00)
    risk_averse = db.Column('RiskAverse', Enum('yes', 'no', name='risk_averse_enum'), nullable=False, default='no')
<<<<<<< HEAD
    registered_at = db.Column('RegisteredAt', db.DateTime, default=datetime.utcnow)
=======
    risk_averse = db.Column('RiskAverse', db.String(3), nullable=False, default='no')
    registered_at = db.Column('RegisteredAt', db.DateTime, default=lambda: datetime.now(timezone.utc))
>>>>>>> Stashed changes
=======
    registered_at = db.Column('RegisteredAt', db.DateTime, default=lambda: datetime.now(timezone.utc))
>>>>>>> 20fa7bb758e07599f19f3b95e1ff2f09edf632ae
    
    # Relationships
    logs = db.relationship('Log', backref='user', lazy=True, cascade='all, delete-orphan')
    transactions = db.relationship('Transaction', backref='user', lazy=True, cascade='all, delete-orphan')
    account = db.relationship('Account', backref='user', uselist=False, lazy=True, cascade='all, delete-orphan')
    holdings = db.relationship('Holding', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        """Hash and set the user's password"""
        self.password = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        """Verify the user's password"""
        return bcrypt.check_password_hash(self.password, password)

    def __repr__(self):
        return f'<User {self.username}>'


# =========================
#  MODEL: STOCKS
# =========================
class Stock(db.Model):
    __tablename__ = 'stocks'
    
    stock_id = db.Column('StockID', db.Integer, primary_key=True, autoincrement=True)
    symbol = db.Column('Symbol', db.String(10), unique=True, nullable=False)
    company = db.Column('Company', db.String(100), nullable=False)
    sector = db.Column('Sector', db.String(50))
    sub_sector = db.Column('SubSector', db.String(50))
    country = db.Column('Country', db.String(100))
    price = db.Column('Price', db.Numeric(15, 2), nullable=False)
    quantity = db.Column('Quantity', db.BigInteger, default=0)
    last_updated = db.Column('LastUpdated', db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    scores = db.relationship('Score', backref='stock', lazy=True, cascade='all, delete-orphan')
    transactions = db.relationship('Transaction', backref='stock', lazy=True, cascade='all, delete-orphan')
    holdings = db.relationship('Holding', backref='stock', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Stock {self.symbol} - {self.company}>'


# =========================
#  MODEL: SCORES
# =========================
class Score(db.Model):
    __tablename__ = 'scores'
    
    score_id = db.Column('ScoreID', db.Integer, primary_key=True, autoincrement=True)
    stock_id = db.Column('StockID', db.Integer, db.ForeignKey('stocks.StockID', ondelete='CASCADE', onupdate='CASCADE'), nullable=False)
    price = db.Column('Price', db.Numeric(15, 2))
    quantity = db.Column('Quantity', db.BigInteger)
    volatility = db.Column('Volatility', db.Numeric(10, 4))
    growth = db.Column('Growth', db.Numeric(10, 4))

    def __repr__(self):
        return f'<Score {self.score_id} for Stock {self.stock_id}>'


# =========================
#  MODEL: LOGS
# =========================
class Log(db.Model):
    __tablename__ = 'logs'
    
    log_id = db.Column('LogID', db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column('UserID', db.Integer, db.ForeignKey('users.UserID', ondelete='CASCADE', onupdate='CASCADE'), nullable=False)
    action_type = db.Column('ActionType', Enum('login', 'logout', 'change', name='action_type_enum'), nullable=False)
    details = db.Column('Details', db.Text)
    date_log = db.Column('DateLog', db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f'<Log {self.log_id} - {self.action_type}>'


# =========================
#  MODEL: TRANSACTIONS
# =========================
class Transaction(db.Model):
    __tablename__ = 'transactions'
    
    transaction_id = db.Column('TransactionID', db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column('UserID', db.Integer, db.ForeignKey('users.UserID', ondelete='CASCADE', onupdate='CASCADE'), nullable=False)
    stock_id = db.Column('StockID', db.Integer, db.ForeignKey('stocks.StockID', ondelete='CASCADE', onupdate='CASCADE'), nullable=False)
    transaction_type = db.Column('TransactionType', Enum('buy', 'sell', name='transaction_type_enum'), nullable=False)
    quantity_transac = db.Column('QuantityTransac', db.BigInteger, nullable=False)
    price_transac = db.Column('PriceTransac', db.Numeric(15, 2), nullable=False)
    date_transac = db.Column('DateTransac', db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f'<Transaction {self.transaction_id} - {self.transaction_type}>'
<<<<<<< HEAD
<<<<<<< Updated upstream
=======
=======
>>>>>>> 20fa7bb758e07599f19f3b95e1ff2f09edf632ae


# =========================
#  MODEL: MARKET DATA
# =========================
class MarketData(db.Model):
    __tablename__ = 'market_data'
    
    id = db.Column('ID', db.Integer, primary_key=True, autoincrement=True)
    datetime = db.Column('DateTime', db.DateTime, nullable=False, index=True)
    instrument = db.Column('Instrument', db.String(10), nullable=False, index=True)
    
    open = db.Column('Open', db.Numeric(15, 4), nullable=False)
    high = db.Column('High', db.Numeric(15, 4), nullable=False)
    low = db.Column('Low', db.Numeric(15, 4), nullable=False)
    close = db.Column('Close', db.Numeric(15, 4), nullable=False)
    volume = db.Column('Volume', db.BigInteger, nullable=False)
    
    vwap = db.Column('VWAP', db.Numeric(15, 4))  # Volume-weighted average price
    amount = db.Column('Amount', db.Numeric(20, 2))  # Dollar value traded
    factor = db.Column('Factor', db.Numeric(10, 6), default=1.0)  # Adjustment factor for splits
    turnover = db.Column('Turnover', db.Numeric(10, 6))  # Volume / float shares
    float_shares = db.Column('FloatShares', db.BigInteger)  # Outstanding shares
    
    created_at = db.Column('CreatedAt', db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Unique constraint to prevent duplicate entries for same symbol and datetime
    __table_args__ = (
        db.UniqueConstraint('Instrument', 'DateTime', name='uq_instrument_datetime'),
        db.Index('idx_instrument_datetime', 'Instrument', 'DateTime'),
    )

    def __repr__(self):
        return f'<MarketData {self.instrument} @ {self.datetime}>'
#  MODEL: ACCOUNT
# =========================
class Account(db.Model):
    __tablename__ = 'accounts'
    
    account_id = db.Column('AccountID', db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column('UserID', db.Integer, db.ForeignKey('users.UserID', ondelete='CASCADE', onupdate='CASCADE'), unique=True, nullable=False)
<<<<<<< HEAD
    balance = db.Column('Balance', db.Numeric(15, 2), default=10000.00, nullable=False)
=======
    balance = db.Column('Balance', db.Numeric(15, 2), default=0.00, nullable=False)
>>>>>>> 20fa7bb758e07599f19f3b95e1ff2f09edf632ae
    created_at = db.Column('CreatedAt', db.DateTime, default=datetime.utcnow)
    updated_at = db.Column('UpdatedAt', db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<Account {self.account_id} - User {self.user_id}>'


# =========================
#  MODEL: HOLDING
# =========================
class Holding(db.Model):
    __tablename__ = 'holdings'
    
    holding_id = db.Column('HoldingID', db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column('UserID', db.Integer, db.ForeignKey('users.UserID', ondelete='CASCADE', onupdate='CASCADE'), nullable=False)
    stock_id = db.Column('StockID', db.Integer, db.ForeignKey('stocks.StockID', ondelete='CASCADE', onupdate='CASCADE'), nullable=False)
    quantity = db.Column('Quantity', db.BigInteger, default=0, nullable=False)
    updated_at = db.Column('UpdatedAt', db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<Holding {self.holding_id} - User {self.user_id} Stock {self.stock_id}>'
<<<<<<< HEAD
>>>>>>> Stashed changes
=======
>>>>>>> 20fa7bb758e07599f19f3b95e1ff2f09edf632ae
