from app import create_app, db
import os
from flask import request, jsonify
from services import upsert_user, upsert_stock, add_transaction
from extensions import db
app = create_app()

with app.app_context():
    db.create_all()
# in app.py after create_app()


def register_routes(app):
    @app.post("/users")
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

    @app.post("/stocks")
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

    @app.post("/transactions")
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

def create_app():
    register_routes(app)
    return app

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)