OUT_DIR = os.path.expanduser("~/.qlib/csv_data/my_data/")
os.makedirs(OUT_DIR, exist_ok=True)

cols = [
    "instrument", "date", "time",
    "open", "high", "low", "close", "volume",
    "vwap", "amount", "factor", "turnover", "floatshares"
]

for instr, sub in df.groupby("instrument"):
    sub[cols].to_csv(os.path.join(OUT_DIR, f"{instr}.csv"), index=False)

print("Done exporting CSV files for Qlib")
