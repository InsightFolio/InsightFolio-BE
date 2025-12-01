from decimal import Decimal

import pandas as pd
import pytest

from flask_app.app.models import Account, Holding, Stock
from flask_app.app.services import (
    create_market_data_from_yahoo,
    process_transaction,
    search_stocks,
    upsert_stock,
    upsert_user,
)


def test_upsert_user_creates_and_updates(db_session):
    created = upsert_user(
        username="alice",
        email="alice@example.com",
        password_plain="secret",
        balance=10.0,
        risk_averse="yes",
    )
    db_session.commit()
    first_hash = created.password

    updated = upsert_user(
        username="alice-renamed",
        email="alice@example.com",
        password_plain="newsecret",
        balance=5.0,
        risk_averse="no",
    )
    db_session.commit()

    assert updated.user_id == created.user_id
    assert updated.username == "alice-renamed"
    assert updated.password != first_hash  # password re-hashed
    assert float(updated.balance) == 5.0
    assert updated.risk_averse == "no"


def test_upsert_stock_creates_and_updates(db_session):
    stock = upsert_stock(
        symbol="AAA",
        company="Alpha",
        sector="Tech",
        sub_sector="Software",
        country="US",
        price=10.0,
        quantity=5,
    )
    db_session.commit()

    updated = upsert_stock(
        symbol="AAA",
        company="Alpha Inc.",
        sector="Technology",
        sub_sector="Software",
        country="USA",
        price=12.5,
        quantity=7,
    )
    db_session.commit()

    assert updated.stock_id == stock.stock_id
    assert updated.company == "Alpha Inc."
    assert float(updated.price) == 12.5
    assert updated.quantity == 7
    assert updated.country == "USA"
    assert updated.sector == "Technology"


def test_process_transaction_buy_updates_balance_and_holdings(db_session, user_factory):
    user = user_factory(email="buyer@example.com")
    stock = Stock(symbol="BUY", company="Buyer Corp", price=Decimal("10.0"), quantity=100)
    account = Account(user_id=user.user_id, balance=Decimal("50.0"))
    db_session.add_all([stock, account])
    db_session.commit()

    txn = process_transaction(user.user_id, stock.stock_id, "buy", 3)
    db_session.commit()

    updated_account = Account.query.get(account.account_id)
    holding = Holding.query.filter_by(user_id=user.user_id, stock_id=stock.stock_id).one()

    assert txn.transaction_type == "buy"
    assert holding.quantity == 3
    assert updated_account.balance == Decimal("20.0")


def test_process_transaction_sell_updates_balance_and_holdings(db_session, user_factory):
    user = user_factory(email="seller@example.com")
    stock = Stock(symbol="SELL", company="Seller Corp", price=Decimal("5.0"), quantity=100)
    account = Account(user_id=user.user_id, balance=Decimal("0.0"))
    db_session.add_all([stock, account])
    db_session.commit()

    holding = Holding(user_id=user.user_id, stock_id=stock.stock_id, quantity=4)
    db_session.add(holding)
    db_session.commit()

    txn = process_transaction(user.user_id, stock.stock_id, "sell", 3)
    db_session.commit()

    updated_account = Account.query.get(account.account_id)
    updated_holding = Holding.query.filter_by(user_id=user.user_id, stock_id=stock.stock_id).one()

    assert txn.transaction_type == "sell"
    assert updated_holding.quantity == 1
    assert updated_account.balance == Decimal("15.0")


def test_process_transaction_validates_balance(db_session, user_factory):
    user = user_factory(email="short@example.com")
    stock = Stock(symbol="SHORT", company="Short Funds", price=Decimal("20.0"), quantity=100)
    account = Account(user_id=user.user_id, balance=Decimal("10.0"))
    db_session.add_all([stock, account])
    db_session.commit()

    with pytest.raises(ValueError):
        process_transaction(user.user_id, stock.stock_id, "buy", 2)


def test_process_transaction_rejects_invalid_type(db_session, user_factory):
    user = user_factory(email="badtype@example.com")
    stock = Stock(symbol="TYPE", company="Type Co", price=Decimal("5.0"), quantity=100)
    account = Account(user_id=user.user_id, balance=Decimal("50.0"))
    db_session.add_all([stock, account])
    db_session.commit()

    with pytest.raises(ValueError):
        process_transaction(user.user_id, stock.stock_id, "hold", 1)


def test_search_stocks_respects_filters(db_session):
    stocks = [
        Stock(symbol="AAA", company="Alpha", country="USA", price=50.0, sector="Tech", sub_sector="Software"),
        Stock(symbol="BBB", company="Beta", country="Canada", price=10.0, sector="Finance", sub_sector="Banking"),
        Stock(symbol="AAC", company="Alpha Chips", country="USA", price=55.0, sector="Tech", sub_sector="Hardware"),
    ]
    db_session.add_all(stocks)
    db_session.commit()

    results = search_stocks(
        text="AA",
        country="USA",
        min_price=20,
        max_price=60,
        sector="Tech",
    )
    symbols = sorted([s.symbol for s in results])
    assert symbols == ["AAA", "AAC"]


def test_create_market_data_from_yahoo(monkeypatch):
    info = {
        "regularMarketDayHigh": 12.0,
        "regularMarketDayLow": 8.0,
        "regularMarketPrice": 10.0,
        "regularMarketOpen": 9.5,
        "regularMarketVolume": 1000,
        "floatShares": 2000,
    }

    class FakeTicker:
        def __init__(self, symbol):
            self.info = info
            self.splits = pd.Series(dtype=float)

    monkeypatch.setattr("flask_app.app.services.yf.Ticker", lambda symbol: FakeTicker(symbol))

    market_data = create_market_data_from_yahoo("abc")
    assert market_data.instrument == "ABC"
    assert float(market_data.vwap) == pytest.approx(10.0)
    assert float(market_data.amount) == pytest.approx(10000.0)
    assert float(market_data.turnover) == pytest.approx(0.5)
    assert float(market_data.factor) == 1.0


def test_create_market_data_from_yahoo_missing_data(monkeypatch):
    class FakeTicker:
        def __init__(self, symbol):
            self.info = {}
            self.splits = pd.Series(dtype=float)

    monkeypatch.setattr("flask_app.app.services.yf.Ticker", lambda symbol: FakeTicker(symbol))

    with pytest.raises(ValueError):
        create_market_data_from_yahoo("missing")
