"""
Simulate a year of stock transactions for user 23.
This script adds realistic buy/sell transactions throughout 2025,
using actual historical prices from MongoDB MarketData.
"""
from flask_app.app import create_app, db
from flask_app.app.models import Transaction, Account, Holding
from flask_app.app.mongo_models import Stock as MongoStock, MarketData as MongoMarketData
from datetime import datetime, timedelta
from decimal import Decimal
import random

app = create_app()

def get_stock_price_on_date(symbol, target_date):
    """Get the closing price of a stock on a specific date from MongoDB."""
    # Try to get price on exact date
    start_datetime = datetime.combine(target_date, datetime.min.time())
    end_datetime = datetime.combine(target_date, datetime.max.time())
    
    market_data = MongoMarketData.objects(
        instrument=symbol,
        date_time__gte=start_datetime,
        date_time__lte=end_datetime
    ).first()
    
    if market_data and market_data.close:
        return float(market_data.close)
    
    # If no data on that date, look back up to 7 days (weekends/holidays)
    for i in range(1, 8):
        prev_date = target_date - timedelta(days=i)
        start_datetime = datetime.combine(prev_date, datetime.min.time())
        end_datetime = datetime.combine(prev_date, datetime.max.time())
        
        market_data = MongoMarketData.objects(
            instrument=symbol,
            date_time__gte=start_datetime,
            date_time__lte=end_datetime
        ).first()
        
        if market_data and market_data.close:
            return float(market_data.close)
    
    # Fallback to current stock price
    stock = MongoStock.objects(_id=symbol).first()
    if stock and stock.price:
        return float(stock.price)
    
    return None


def simulate_transactions():
    with app.app_context():
        user_id = 23
        
        # Get user's account
        account = Account.query.filter_by(user_id=user_id).first()
        if not account:
            print("Account not found!")
            return
        
        # Ensure balance is Decimal
        if not isinstance(account.balance, Decimal):
            account.balance = Decimal(str(account.balance))
        
        print(f"Initial account balance: ${account.balance}")
        
        # Clear existing holdings and transactions for clean simulation
        print("\nClearing existing transactions and holdings...")
        Holding.query.filter_by(user_id=user_id).delete()
        Transaction.query.filter_by(user_id=user_id).delete()
        db.session.commit()
        
        # Get 10 popular stocks from MongoDB
        stocks = list(MongoStock.objects.limit(20))
        selected_stocks = random.sample(stocks, min(10, len(stocks)))
        
        print(f"\nSelected stocks: {[s.symbol for s in selected_stocks]}")
        
        # Generate transactions throughout 2025
        transactions_data = []
        start_date = datetime(2025, 1, 15).date()  # Start in mid-January
        end_date = datetime(2025, 12, 5).date()  # Today
        
        # Track holdings to know what we can sell
        current_holdings = {}  # {stock_id: quantity}
        
        # Generate buy transactions for each stock at different times
        for stock in selected_stocks:
            # Each stock gets 1-3 buy transactions throughout the year
            num_buys = random.randint(1, 3)
            
            for _ in range(num_buys):
                # Random date in 2025
                days_diff = (end_date - start_date).days
                random_days = random.randint(0, days_diff - 30)  # Leave room for potential sells
                transaction_date = start_date + timedelta(days=random_days)
                
                # Get actual historical price for that date (use _id which is symbol)
                price = get_stock_price_on_date(stock._id, transaction_date)
                
                if not price:
                    print(f"  ⚠️  No price data for {stock.symbol} on {transaction_date}, skipping...")
                    continue
                
                # Buy between 1-5 shares
                quantity = random.randint(1, 5)
                
                transactions_data.append({
                    'stock_id': stock.stock_id,
                    'symbol': stock.symbol,
                    'date': transaction_date,
                    'type': 'buy',
                    'quantity': quantity,
                    'price': price
                })
                
                # Update holdings tracker
                if stock.stock_id not in current_holdings:
                    current_holdings[stock.stock_id] = 0
                current_holdings[stock.stock_id] += quantity
        
        # Add some sell transactions (sell about 30% of stocks)
        stocks_to_sell = random.sample(list(current_holdings.keys()), 
                                       max(1, len(current_holdings) // 3))
        
        for stock_id in stocks_to_sell:
            stock = next(s for s in selected_stocks if s.stock_id == stock_id)
            
            # Find a buy transaction for this stock
            buy_transactions = [t for t in transactions_data 
                              if t['stock_id'] == stock_id and t['type'] == 'buy']
            
            if not buy_transactions:
                continue
            
            # Sell date should be after buy date
            earliest_buy = min(t['date'] for t in buy_transactions)
            latest_sell_date = end_date - timedelta(days=10)
            
            if earliest_buy >= latest_sell_date:
                continue
            
            days_diff = (latest_sell_date - earliest_buy).days
            sell_date = earliest_buy + timedelta(days=random.randint(30, days_diff))
            
            # Get price on sell date (use _id which is symbol)
            price = get_stock_price_on_date(stock._id, sell_date)
            
            if not price:
                print(f"  ⚠️  No price data for {stock.symbol} on {sell_date}, skipping sell...")
                continue
            
            # Sell some or all shares
            max_shares = current_holdings[stock_id]
            quantity = random.randint(1, max_shares)
            
            transactions_data.append({
                'stock_id': stock_id,
                'symbol': stock.symbol,
                'date': sell_date,
                'type': 'sell',
                'quantity': quantity,
                'price': price
            })
            
            current_holdings[stock_id] -= quantity
        
        # Sort transactions by date
        transactions_data.sort(key=lambda x: x['date'])
        
        # Execute transactions and update database
        print(f"\nExecuting {len(transactions_data)} transactions...")
        print("-" * 80)
        
        for i, txn_data in enumerate(transactions_data, 1):
            stock_id = txn_data['stock_id']
            txn_type = txn_data['type']
            quantity = txn_data['quantity']
            price = Decimal(str(txn_data['price']))
            txn_date = datetime.combine(txn_data['date'], datetime.now().time())
            total_cost = price * quantity
            
            print(f"{i}. {txn_data['date']} | {txn_type.upper():4} {quantity:2} {txn_data['symbol']:6} @ ${price:8.2f} | Total: ${total_cost:10.2f}")
            
            # Create transaction record
            transaction = Transaction(
                user_id=user_id,
                stock_id=stock_id,
                transaction_type=txn_type,
                quantity_transac=quantity,
                price_transac=price,
                date_transac=txn_date
            )
            db.session.add(transaction)
            
            # Update account balance
            if txn_type == 'buy':
                account.balance = Decimal(str(account.balance)) - total_cost
            else:  # sell
                account.balance = Decimal(str(account.balance)) + total_cost
            
            # Update holdings
            holding = Holding.query.filter_by(user_id=user_id, stock_id=stock_id).first()
            
            if txn_type == 'buy':
                if holding:
                    holding.quantity += quantity
                else:
                    holding = Holding(user_id=user_id, stock_id=stock_id, quantity=quantity)
                    db.session.add(holding)
            else:  # sell
                if holding:
                    holding.quantity -= quantity
                    if holding.quantity == 0:
                        db.session.delete(holding)
        
        # Commit all changes
        db.session.commit()
        
        print("-" * 80)
        print(f"\n✅ Simulation complete!")
        print(f"Final account balance: ${account.balance}")
        
        # Show final holdings
        holdings = Holding.query.filter_by(user_id=user_id).all()
        print(f"\nFinal holdings ({len(holdings)} stocks):")
        total_value = 0
        for holding in holdings:
            stock = MongoStock.objects(stock_id=holding.stock_id).first()
            if stock:
                current_price = float(stock.price) if stock.price else 0
                value = current_price * holding.quantity
                total_value += value
                print(f"  {stock.symbol:6} | {holding.quantity:2} shares @ ${current_price:8.2f} = ${value:10.2f}")
        
        print(f"\nTotal portfolio value: ${total_value:.2f}")
        print(f"Total account value: ${float(account.balance) + total_value:.2f}")


if __name__ == '__main__':
    simulate_transactions()
