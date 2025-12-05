import pandas as pd
import sqlalchemy

engine = sqlalchemy.create_engine("mysql+pymysql://user:pass@host/db")

query = """
SELECT
    ID,
    DateTime,
    Instrument,
    Open,
    High,
    Low,
    Close,
    Volume,
    VWAP,
    Amount,
    Factor,
    Turnover,
    FloatShares,
    CreatedAt
FROM market_data
ORDER BY DateTime ASC;
"""

df = pd.read_sql(query, engine)
