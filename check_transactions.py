from flask_app.app import create_app, db
from flask_app.app.models import Transaction

app = create_app()

with app.app_context():
    transactions = Transaction.query.filter_by(user_id=23).order_by(Transaction.date_transac).all()
    
    print(f"Found {len(transactions)} transactions for user 23:")
    for txn in transactions:
        print(f"  {txn.date_transac} - {txn.transaction_type} {txn.quantity_transac} shares of stock_id {txn.stock_id} @ ${txn.price_transac}")
