from app import create_app
from extensions import db
from services import upsert_user, upsert_stock, add_score_for_symbol, add_log_for_email, add_transaction

app = create_app()
with app.app_context():
    try:
        upsert_user('liam', 'liam@example.com', 'SuperSecret123!', balance=1000.00, risk_averse='no')
        upsert_stock('AAPL', 'Apple Inc.', 'Technology', 'Consumer Electronics', 'United States', 228.55, 0)
        add_score_for_symbol('AAPL', price=228.55, quantity=1000, volatility=0.0421, growth=0.0133)
        add_log_for_email('liam@example.com', 'login', 'Successful login')
        add_transaction('liam@example.com', 'AAPL', 'buy', qty=10, price=229.10)

        db.session.commit()
        print("Seed complete.")
    except Exception as e:
        db.session.rollback()
        raise
