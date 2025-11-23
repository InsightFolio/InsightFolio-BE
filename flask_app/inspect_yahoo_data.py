"""
Quick script to see ALL data that Yahoo Finance returns for a stock.
"""

import yfinance as yf
import json

# Change this to any stock symbol you want to check
SYMBOL = "AAPL"

print(f"Fetching all data for {SYMBOL} from Yahoo Finance...\n")
print("=" * 70)

ticker = yf.Ticker(SYMBOL)
info = ticker.info

# Pretty print all the data
print(json.dumps(info, indent=2, default=str))

print("\n" + "=" * 70)
print(f"\nTotal fields available: {len(info)}")
print("\nKey fields:")
print("-" * 70)
print(f"Company: {info.get('longName')}")
print(f"Sector: {info.get('sector')}")
print(f"Industry: {info.get('industry')}")
print(f"Country: {info.get('country')}")
print(f"Price: ${info.get('currentPrice') or info.get('regularMarketPrice')}")
print(f"Market Cap: ${info.get('marketCap'):,}" if info.get('marketCap') else "Market Cap: N/A")
print(f"Shares Outstanding: {info.get('sharesOutstanding'):,}" if info.get('sharesOutstanding') else "Shares Outstanding: N/A")
print(f"P/E Ratio: {info.get('trailingPE')}")
print(f"Dividend Yield: {info.get('dividendYield')}")
print(f"52-Week High: ${info.get('fiftyTwoWeekHigh')}")
print(f"52-Week Low: ${info.get('fiftyTwoWeekLow')}")
