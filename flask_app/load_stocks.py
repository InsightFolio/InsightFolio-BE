"""
Load stock metadata from Twelve Data API into the database.

This script:
1. Fetches stock listings from selected exchanges using /stocks endpoint
2. Inserts/updates them in the stocks table
3. Processes in batches to respect memory limits
4. Rate-limits requests to stay within API quotas

Later steps (add here when ready):
- Step 2: Use /profile endpoint to populate sector/sub_sector fields
- Step 3: Use /price or /quote endpoint to update prices periodically
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

# Configuration - EDIT THESE EXCHANGES AS NEEDED
EXCHANGES = [
    "NYSE",
    "NASDAQ", 
    "TSX",
    "LSE",
    "EURONEXT"
]

# API Configuration
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Processing Configuration
BATCH_SIZE = 500  # Commit every 500 rows to manage memory
REQUEST_DELAY = 8.0  # Seconds between API calls (8 req/min = ~7.5s spacing, adding buffer)
MAX_STOCKS_PER_EXCHANGE = None  # Limit how many stocks to process per exchange (None = no limit)

# Validation
if not TWELVE_DATA_API_KEY:
    print("ERROR: TWELVE_DATA_API_KEY not found in environment variables")
    sys.exit(1)

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found in environment variables")
    sys.exit(1)


def get_stocks_for_exchange(exchange: str) -> list[dict]:
    """
    Fetch all stocks for a given exchange from Twelve Data API.
    
    Args:
        exchange: Exchange code (e.g., "NYSE", "NASDAQ")
    
    Returns:
        List of stock dictionaries with keys: symbol, name, country
        Returns empty list on error
    """
    url = "https://api.twelvedata.com/stocks"
    params = {
        "exchange": exchange,
        "apikey": TWELVE_DATA_API_KEY
    }
    
    try:
        print(f"Fetching stocks from {exchange}...", end=" ", flush=True)
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        
        # Check for API errors
        if "status" in data and data["status"] == "error":
            print(f"❌ API Error: {data.get('message', 'Unknown error')}")
            return []
        
        # Extract stock list
        stocks = data.get("data", [])
        print(f"✓ Found {len(stocks)} stocks")
        return stocks
        
    except requests.exceptions.Timeout:
        print(f"❌ Request timeout")
        return []
    except requests.exceptions.RequestException as e:
        print(f"❌ Network error: {e}")
        return []
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return []


def upsert_stock(session, stock_dict):
    """
    Insert or update a stock record in the database.
    
    Args:
        session: SQLAlchemy session
        stock_dict: Dict with keys: symbol, name, country
    
    Note: Uses raw SQL to avoid importing Flask app models
    """
    symbol = stock_dict.get("symbol", "").strip()
    company = stock_dict.get("name", "").strip()
    country = stock_dict.get("country", "").strip()
    
    # Skip if missing required fields
    if not symbol or not company:
        return False
    
    # Truncate to fit database constraints
    symbol = symbol[:10]
    company = company[:100]
    country = country[:100] if country else None
    
    try:
        # Check if stock exists
        result = session.execute(
            text("SELECT StockID FROM stocks WHERE Symbol = :symbol"),
            {"symbol": symbol}
        ).fetchone()
        
        current_time = datetime.now(timezone.utc)
        
        if result:
            # Update existing stock
            session.execute(
                text("""
                UPDATE stocks 
                SET Company = :company, Country = :country, LastUpdated = :last_updated
                WHERE Symbol = :symbol
                """),
                {"symbol": symbol, "company": company, "country": country, "last_updated": current_time}
            )
        else:
            # Insert new stock with defaults
            session.execute(
                text("""
                INSERT INTO stocks (Symbol, Company, Country, Price, Quantity, Sector, SubSector, LastUpdated)
                VALUES (:symbol, :company, :country, 0, 0, NULL, NULL, :last_updated)
                """),
                {"symbol": symbol, "company": company, "country": country, "last_updated": current_time}
            )
        
        return True
        
    except SQLAlchemyError as e:
        print(f"\n  ⚠ Error processing {symbol}: {e}")
        return False


def main():
    """
    Main execution function.
    Processes all configured exchanges and loads stocks into database.
    """
    print("=" * 60)
    print("TWELVE DATA STOCK LOADER")
    print("=" * 60)
    print(f"Exchanges to process: {', '.join(EXCHANGES)}")
    print(f"Batch size: {BATCH_SIZE}")
    print(f"Rate limit: {REQUEST_DELAY}s between requests")
    print("=" * 60)
    print()
    
    # Setup database connection
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    
    total_processed = 0
    total_inserted = 0
    total_updated = 0
    
    for idx, exchange in enumerate(EXCHANGES):
        print(f"\n[{idx + 1}/{len(EXCHANGES)}] Processing {exchange}")
        print("-" * 60)
        
        # Fetch stocks from API
        stocks = get_stocks_for_exchange(exchange)
        
        if not stocks:
            print(f"  Skipping {exchange} (no data)")
            # Rate limit even on failures to avoid hammering API
            if idx < len(EXCHANGES) - 1:
                print(f"  Waiting {REQUEST_DELAY}s before next request...")
                time.sleep(REQUEST_DELAY)
            continue
        
        # Apply limit if configured
        original_count = len(stocks)
        if MAX_STOCKS_PER_EXCHANGE and len(stocks) > MAX_STOCKS_PER_EXCHANGE:
            stocks = stocks[:MAX_STOCKS_PER_EXCHANGE]
            print(f"  ⚠ Limited to {MAX_STOCKS_PER_EXCHANGE} stocks (out of {original_count} available)")
        
        # Process in batches
        session = Session()
        processed_count = 0
        
        try:
            for i, stock in enumerate(stocks, 1):
                success = upsert_stock(session, stock)
                if success:
                    processed_count += 1
                
                # Commit in batches
                if i % BATCH_SIZE == 0:
                    session.commit()
                    print(f"  Committed batch {i // BATCH_SIZE} ({i}/{len(stocks)} stocks)")
            
            # Final commit for remaining records
            session.commit()
            print(f"  ✓ Exchange {exchange}: {processed_count}/{len(stocks)} stocks processed")
            
            total_processed += processed_count
            
        except Exception as e:
            session.rollback()
            print(f"  ❌ Error during batch processing: {e}")
        finally:
            session.close()
        
        # Rate limiting between exchanges
        if idx < len(EXCHANGES) - 1:
            print(f"  Waiting {REQUEST_DELAY}s before next request...")
            time.sleep(REQUEST_DELAY)
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total stocks processed: {total_processed}")
    print(f"Exchanges completed: {len(EXCHANGES)}")
    print("=" * 60)
    print("\nNext steps:")
    print("  • Run /profile endpoint fetcher to populate sector/sub_sector")
    print("  • Set up periodic /price updates for live data")
    print("=" * 60)


if __name__ == "__main__":
    main()
