import os

# Lowercase + normalize
df = df.rename(columns={
    "Instrument": "instrument",
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
    "VWAP": "vwap",
    "Amount": "amount",
    "Factor": "factor",
    "Turnover": "turnover",
    "FloatShares": "floatshares"
})

# Split timestamp
df["date"] = pd.to_datetime(df["DateTime"]).dt.strftime("%Y-%m-%d")
df["time"] = pd.to_datetime(df["DateTime"]).dt.strftime("%H:%M:%S")
