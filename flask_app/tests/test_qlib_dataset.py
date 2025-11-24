import os
import qlib
from qlib.data import D
# from flask_app.app.scoring import score_stocks  # uncomment + adjust when ready

provider_uri = os.path.expanduser("~/.qlib/qlib_data/us_data")

qlib.init(provider_uri=provider_uri, region="us")

# 1) See what instruments exist
inst = D.instruments("all")
inst_list = list(inst)
print("First 20 instruments:", inst_list[:20])

# 2) Pick a few to play with
sample_inst = inst_list[:3]  # e.g. 3 tickers

df = D.features(
    instruments=sample_inst,
    fields=["$close", "$volume"],
    start_time="2024-01-01",
    end_time="2024-01-31",
)

print("\nSample features:")
print(df.head())

# 3) When your scoring function is ready, plug it in:
# scores = score_stocks(df)
# print("\nScores:")
# print(scores.head())
