from decimal import Decimal
from unittest.mock import patch

import pytest

from flask_app.app.models import Account, Holding, Score, Stock, Transaction


@patch("flask_app.app.routes.run_scoring_workflow")
def test_score_endpoint_uses_payload_config(mock_run_scoring_workflow, client):
    mock_run_scoring_workflow.return_value = {
        "results": [{"symbol": "AAPL", "score": 0.1, "timestamp": "2024-01-01"}],
        "config": {"symbols": ["AAPL"], "region": "US"},
    }

    response = client.post("/score", json={"config": {"symbols": ["AAPL"], "region": "US"}})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["results"][0]["symbol"] == "AAPL"
    assert mock_run_scoring_workflow.called


@patch("flask_app.app.routes.run_scoring_workflow", side_effect=RuntimeError("boom"))
def test_score_endpoint_handles_errors(mock_run_scoring_workflow, client):
    response = client.post("/score", json={})
    assert response.status_code == 500
    assert response.get_json()["error"] == "boom"


def test_search_returns_enriched_results(client, db_session, monkeypatch):
    stock = Stock(symbol="LOOK", company="Looker", price=Decimal("10.0"), quantity=1)
    db_session.add(stock)
    db_session.commit()
    db_session.add(Score(stock_id=stock.stock_id, growth=2.0, price=stock.price, quantity=stock.quantity))
    db_session.commit()

    monkeypatch.setattr("flask_app.app.routes.search_stocks", lambda **kwargs: [stock])

    resp = client.post("/search", json={"text": "LO"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload[0]["symbol"] == "LOOK"
    assert payload[0]["change_pct"] == 2.0
    assert payload[0]["change"] == pytest.approx(0.2)


def test_popular_stocks_orders_by_turnover(client, db_session, user_factory):
    user = user_factory(email="turn@example.com")
    stock_low = Stock(symbol="LOW", company="Low Inc", price=Decimal("5.0"), quantity=1)
    stock_high = Stock(symbol="HIGH", company="High Inc", price=Decimal("15.0"), quantity=1)
    db_session.add_all([stock_low, stock_high])
    db_session.commit()

    db_session.add_all(
        [
            Transaction(user_id=user.user_id, stock_id=stock_low.stock_id, transaction_type="buy", quantity_transac=1, price_transac=Decimal("5.0")),
            Transaction(user_id=user.user_id, stock_id=stock_high.stock_id, transaction_type="buy", quantity_transac=3, price_transac=Decimal("15.0")),
        ]
    )
    db_session.add(Score(stock_id=stock_high.stock_id, growth=1.0))
    db_session.commit()

    resp = client.get("/popular?limit=2")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert [item["symbol"] for item in payload] == ["HIGH", "LOW"]
    assert payload[0]["turnover"] > payload[1]["turnover"]


def test_popular_stocks_fallback_uses_prices(client, db_session):
    pricey = Stock(symbol="PRC", company="Pricey", price=Decimal("20.0"), quantity=1)
    cheap = Stock(symbol="CHP", company="Cheap", price=Decimal("1.0"), quantity=1)
    db_session.add_all([pricey, cheap])
    db_session.commit()

    resp = client.get("/popular?limit=1")
    payload = resp.get_json()
    assert payload[0]["symbol"] == "PRC"
    assert payload[0]["turnover"] == 0.0


def test_execute_transaction_buy_flow(client, db_session, user_factory, auth_header):
    user = user_factory(email="routebuyer@example.com")
    account = Account(user_id=user.user_id, balance=Decimal("30.0"))
    stock = Stock(symbol="RTBUY", company="Route Buy", price=Decimal("5.0"), quantity=1)
    db_session.add_all([account, stock])
    db_session.commit()

    headers = auth_header(user.user_id)
    resp = client.post(
        "/transactions/execute",
        json={"stock_id": stock.stock_id, "transaction_type": "buy", "quantity": 2},
        headers=headers,
    )

    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["transaction_type"] == "buy"
    assert payload["new_balance"] == 20.0

    holding = Holding.query.filter_by(user_id=user.user_id, stock_id=stock.stock_id).one()
    assert holding.quantity == 2
