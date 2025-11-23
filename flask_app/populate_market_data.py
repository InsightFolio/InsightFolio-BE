"""
Bulk populate market_data table with current data for all stocks.
This script fetches OHLCV data from Yahoo Finance for each stock in the stocks table.
Uses batch processing to commit in groups and time delays to avoid rate limiting.
"""

from app import create_app
from app.extensions import db
from app.models import Stock
from app.services import create_market_data_from_yahoo
from sqlalchemy.exc import IntegrityError
import time

app = create_app()

# Configuration
BATCH_SIZE = 50  # Commit every 50 stocks
DELAY_BETWEEN_REQUESTS = 0.5  # 0.5 seconds between each Yahoo Finance request
DELAY_BETWEEN_BATCHES = 5  # 5 seconds pause after each batch

def populate_market_data_for_all_stocks():
    """
    Fetch market data from Yahoo Finance for all stocks and insert into market_data table.
    Uses batch processing to improve performance and avoid rate limiting.
    """
    with app.app_context():
        # Get all stocks from database, ordered by StockID
        stocks = Stock.query.order_by(Stock.stock_id).all()
        total = len(stocks)
        
        # Time estimate
        estimated_time_per_stock = DELAY_BETWEEN_REQUESTS + 0.5  # Request delay + processing time
        estimated_batches = (total // BATCH_SIZE) + 1
        estimated_batch_delays = estimated_batches * DELAY_BETWEEN_BATCHES
        total_estimated_seconds = (total * estimated_time_per_stock) + estimated_batch_delays
        estimated_minutes = total_estimated_seconds / 60
        
        print(f"📊 Found {total} stocks to process")
        print(f"⏱️  Estimated time: {estimated_minutes:.1f} minutes (~{estimated_minutes/60:.1f} hours)")
        print(f"📦 Batch size: {BATCH_SIZE} stocks per batch")
        print(f"⏳ Delays: {DELAY_BETWEEN_REQUESTS}s between requests, {DELAY_BETWEEN_BATCHES}s between batches")
        print("-" * 60)
        
        success_count = 0
        failed_count = 0
        skipped_count = 0
        batch_records = []
        start_time = time.time()
        
        for idx, stock in enumerate(stocks, 1):
            symbol = stock.symbol
            print(f"[{idx}/{total}] Processing {symbol}...", end=" ")
            
            try:
                # Use the shared service function to create market data
                market_data = create_market_data_from_yahoo(symbol)
                
                batch_records.append(market_data)
                print(f"✓ SUCCESS (price: ${market_data.close:.2f}, volume: {market_data.volume:,})")
                success_count += 1
                
                # Batch commit
                if len(batch_records) >= BATCH_SIZE:
                    try:
                        db.session.bulk_save_objects(batch_records)
                        db.session.commit()
                        print(f"💾 Committed batch of {len(batch_records)} records")
                        batch_records = []
                        time.sleep(DELAY_BETWEEN_BATCHES)
                    except Exception as batch_error:
                        db.session.rollback()
                        print(f"⚠️  Batch commit failed: {batch_error}")
                        batch_records = []
            
            except ValueError as e:
                # Missing OHLCV data
                print(f"❌ SKIPPED: {str(e)[:50]}")
                skipped_count += 1
                
            except IntegrityError as e:
                db.session.rollback()
                print(f"⚠️  DUPLICATE (already exists)")
                skipped_count += 1
                
            except Exception as e:
                db.session.rollback()
                print(f"❌ FAILED: {str(e)[:50]}")
                failed_count += 1
            
            # Delay between requests to avoid rate limiting
            time.sleep(DELAY_BETWEEN_REQUESTS)
        
        # Commit any remaining records
        if batch_records:
            try:
                db.session.bulk_save_objects(batch_records)
                db.session.commit()
                print(f"💾 Committed final batch of {len(batch_records)} records")
            except Exception as batch_error:
                db.session.rollback()
                print(f"⚠️  Final batch commit failed: {batch_error}")
        
        elapsed_time = time.time() - start_time
        elapsed_minutes = elapsed_time / 60
        
        print("-" * 60)
        print(f"\n📊 Summary:")
        print(f"   ✓ Success: {success_count}")
        print(f"   ❌ Failed:  {failed_count}")
        print(f"   ⚠️  Skipped: {skipped_count}")
        print(f"   Total:    {total}")
        print(f"\n⏱️  Time taken: {elapsed_minutes:.1f} minutes ({elapsed_time:.0f} seconds)")
        print(f"✅ Market data population complete!")

if __name__ == '__main__':
    populate_market_data_for_all_stocks()
