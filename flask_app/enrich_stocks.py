"""
Enrich stock data with sector, subsector (industry), and price from Twelve Data API.

This script:
1. Fetches /profile for sector and industry (subsector) data
2. Fetches /quote for closing price data
3. Updates existing stocks in the database
4. Processes 320 stocks per exchange (1600 total) to stay within API limits

API Limits:
- 4 API keys available
- 3200 total calls (800 per key)
- 1600 calls for /profile (320 per exchange × 5 exchanges)
- 1600 calls for /quote (320 per exchange × 5 exchanges)
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

# Configuration
EXCHANGES = ["NYSE", "NASDAQ", "TSX", "LSE", "EURONEXT"]
STOCKS_PER_EXCHANGE = 160  # 800 total / 5 exchanges

# API Configuration - EDIT: Add all 4 API keys here
API_KEYS = [
    os.getenv("TWELVE_DATA_API_KEY_1"),
    os.getenv("TWELVE_DATA_API_KEY_2"),
    os.getenv("TWELVE_DATA_API_KEY_3"),
    os.getenv("TWELVE_DATA_API_KEY_4"),
]

DATABASE_URL = os.getenv("DATABASE_URL")

# Processing Configuration
BATCH_SIZE = 50  # Commit every 50 stocks
REQUEST_DELAY = 2.0  # Seconds between API calls (with 4 keys, we can go faster)

# API call tracking
current_key_index = 0
calls_per_key = {i: 0 for i in range(len(API_KEYS))}
MAX_CALLS_PER_KEY = 800  # 800 calls per key

# Validation
# Remove None values (keys that weren't set)
API_KEYS = [key for key in API_KEYS if key]

if not API_KEYS:
    print("ERROR: No TWELVE_DATA_API_KEY found in environment variables")
    print("Please set at least TWELVE_DATA_API_KEY_1 in your .env file")
    sys.exit(1)

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found in environment variables")
    sys.exit(1)

print(f"Loaded {len(API_KEYS)} API key(s)")


def get_next_api_key():
    """
    Rotate through API keys to distribute load.
    Returns the next available API key.
    """
    global current_key_index
    
    # Find next key that hasn't hit the limit
    for _ in range(len(API_KEYS)):
        if calls_per_key[current_key_index] < MAX_CALLS_PER_KEY:
            key = API_KEYS[current_key_index]
            calls_per_key[current_key_index] += 1
            # Rotate to next key for next call
            current_key_index = (current_key_index + 1) % len(API_KEYS)
            return key
        current_key_index = (current_key_index + 1) % len(API_KEYS)
    
    print("ERROR: All API keys have reached their limit!")
    return None


def get_stock_profile(symbol: str) -> dict:
    """
    Fetch company profile (sector, industry) from Twelve Data /profile endpoint.
    
    Args:
        symbol: Stock ticker symbol
    
    Returns:
        Dict with keys: sector, industry (or None values on error)
    """
    api_key = get_next_api_key()
    if not api_key:
        return {"sector": None, "industry": None}
    
    url = "https://api.twelvedata.com/profile"
    params = {
        "symbol": symbol,
        "apikey": api_key
    }
    
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        # Check for API errors
        if "status" in data and data["status"] == "error":
            return {"sector": None, "industry": None}
        
        return {
            "sector": data.get("sector"),
            "industry": data.get("industry")
        }
        
    except Exception:
        return {"sector": None, "industry": None}


def get_stock_quote(symbol: str) -> float:
    """
    Fetch current stock price from Twelve Data /quote endpoint.
    
    Args:
        symbol: Stock ticker symbol
    
    Returns:
        Float price (close value) rounded to 2 decimals, or None on error
    """
    api_key = get_next_api_key()
    if not api_key:
        return None
    
    url = "https://api.twelvedata.com/quote"
    params = {
        "symbol": symbol,
        "apikey": api_key
    }
    
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        # Check for API errors
        if "status" in data and data["status"] == "error":
            return None
        
        close_price = data.get("close")
        if close_price:
            return round(float(close_price), 2)
        return None
        
    except Exception:
        return None


def get_stocks_to_enrich(session, exchange: str, limit: int) -> list:
    """
    Get stocks from database that need enrichment (sector/price are NULL/0).
    
    Args:
        session: SQLAlchemy session
        exchange: Exchange name (for filtering if needed)
        limit: Max number of stocks to return
    
    Returns:
        List of (symbol, stock_id) tuples
    """
    try:
        # Get stocks where Sector is NULL or Price is 0
        # Prioritize stocks with missing data
        result = session.execute(
            text("""
            SELECT Symbol, StockID 
            FROM stocks 
            WHERE (Sector IS NULL OR Price = 0)
            ORDER BY StockID 
            LIMIT :limit
            """),
            {"limit": limit}
        ).fetchall()
        
        return [(row[0], row[1]) for row in result]
        
    except SQLAlchemyError as e:
        print(f"  ❌ Error fetching stocks: {e}")
        return []


def update_stock_data(session, stock_id: int, sector: str, subsector: str, price: float):
    """
    Update stock with sector, subsector, and price data.
    
    Args:
        session: SQLAlchemy session
        stock_id: Stock ID to update
        sector: Sector value
        subsector: SubSector (industry) value
        price: Price value
    """
    try:
        current_time = datetime.now(timezone.utc)
        
        session.execute(
            text("""
            UPDATE stocks 
            SET Sector = :sector, 
                SubSector = :subsector, 
                Price = :price,
                LastUpdated = :last_updated
            WHERE StockID = :stock_id
            """),
            {
                "sector": sector,
                "subsector": subsector,
                "price": price if price is not None else 0,
                "last_updated": current_time,
                "stock_id": stock_id
            }
        )
        return True
        
    except SQLAlchemyError as e:
        print(f"\n  ⚠ Error updating stock {stock_id}: {e}")
        return False


def main():
    """
    Main execution function.
    Enriches stocks with sector, subsector, and price data.
    """
    print("=" * 60)
    print("TWELVE DATA STOCK ENRICHMENT")
    print("=" * 60)
    print(f"API Keys available: {len(API_KEYS)}")
    print(f"Stocks per exchange: {STOCKS_PER_EXCHANGE}")
    print(f"Total stocks to enrich: {STOCKS_PER_EXCHANGE * len(EXCHANGES)}")
    print(f"Batch size: {BATCH_SIZE}")
    print(f"Rate limit: {REQUEST_DELAY}s between requests")
    print("=" * 60)
    print()
    
    # Setup database connection
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    
    total_processed = 0
    total_success = 0
    total_api_calls = 0
    
    for idx, exchange in enumerate(EXCHANGES):
        print(f"\n[{idx + 1}/{len(EXCHANGES)}] Processing {exchange}")
        print("-" * 60)
        
        session = Session()
        
        # Get stocks to enrich for this exchange
        stocks = get_stocks_to_enrich(session, exchange, STOCKS_PER_EXCHANGE)
        
        if not stocks:
            print(f"  No stocks to enrich for {exchange}")
            session.close()
            continue
        
        print(f"  Found {len(stocks)} stocks to enrich")
        
        processed_count = 0
        
        try:
            for i, (symbol, stock_id) in enumerate(stocks, 1):
                # Fetch profile data
                print(f"  [{i}/{len(stocks)}] Fetching data for {symbol}...", end=" ", flush=True)
                profile = get_stock_profile(symbol)
                time.sleep(REQUEST_DELAY)  # Rate limit
                
                # Fetch quote data
                price = get_stock_quote(symbol)
                time.sleep(REQUEST_DELAY)  # Rate limit
                
                total_api_calls += 2
                
                # Update database
                success = update_stock_data(
                    session,
                    stock_id,
                    profile["sector"],
                    profile["industry"],
                    price
                )
                
                if success:
                    processed_count += 1
                    print("✓")
                else:
                    print("✗")
                
                # Commit in batches
                if i % BATCH_SIZE == 0:
                    session.commit()
                    print(f"  Committed batch {i // BATCH_SIZE} ({i}/{len(stocks)} stocks)")
            
            # Final commit
            session.commit()
            print(f"  ✓ Exchange {exchange}: {processed_count}/{len(stocks)} stocks enriched")
            
            total_processed += len(stocks)
            total_success += processed_count
            
        except Exception as e:
            session.rollback()
            print(f"  ❌ Error during batch processing: {e}")
        finally:
            session.close()
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total stocks processed: {total_processed}")
    print(f"Successfully enriched: {total_success}")
    print(f"Total API calls made: {total_api_calls}")
    print(f"API calls per key: {dict(calls_per_key)}")
    print("=" * 60)
    print("\nNext steps:")
    print("  • Remaining stocks can be enriched in future runs")
    print("  • Set up periodic price updates using /quote endpoint")
    print("=" * 60)


if __name__ == "__main__":
    main()
