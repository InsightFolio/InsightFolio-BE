#!/usr/bin/env python3
"""
End-to-end utility to build a Qlib provider from the `market_data` table.

Pipeline:
1) Read market data from the configured database
2) Normalize it into the CSV layout expected by Qlib
3) Call qlib.scripts.dump_bin to build the binary provider
4) Smoke-test the provider by querying a couple of instruments

Configuration:
  - `DATABASE_URL` env var (or --db-url) for SQLAlchemy
  - `QLIB_CSV_OUT_DIR` to override CSV output dir
  - `QLIB_DATA_PATH` to override Qlib provider dir
  - `QLIB_FREQ`, `QLIB_REGION`, `QLIB_INCLUDE_FIELDS` also available
"""

from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd
from qlib.data import D
from sqlalchemy import create_engine
from qlib.config import REG_CN, REG_US

# Default SQL if no --query-file is provided
DEFAULT_SQL = """
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


@dataclass
class Settings:
    db_url: str
    sql_query: str
    csv_dir: Path
    provider_dir: Path
    freq: str
    region: str
    include_fields: List[str]
    dry_run: bool
    skip_dump: bool
    skip_test: bool


def _log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}")


def parse_args(argv: Optional[Iterable[str]] = None) -> Settings:
    parser = argparse.ArgumentParser(
        description="Build a Qlib provider from the market_data table."
    )

    parser.add_argument(
        "--db-url",
        default="mysql+pymysql://root:sboNmmSTdZZFaTUFbNDVSBHqUOiSFlln@ballast.proxy.rlwy.net:31990/railway",
        help="SQLAlchemy DB URL; defaults to DATABASE_URL env or sqlite:///app.db",
    )
    parser.add_argument(
        "--query-file",
        type=Path,
        help="Optional path to a .sql file. If omitted, a default SELECT over market_data is used.",
    )
    parser.add_argument(
        "--csv-dir",
        type=Path,
        default=Path(
            os.getenv("QLIB_CSV_OUT_DIR", os.path.expanduser("~/.qlib/csv_data/my_data"))
        ),
        help="Directory where per-instrument CSVs will be written.",
    )
    parser.add_argument(
        "--provider-dir",
        type=Path,
        default=Path(
            os.getenv("QLIB_DATA_PATH", os.path.expanduser("~/.qlib/qlib_data/my_data"))
        ),
        help="Directory for the generated Qlib provider (QLIB_DATA_PATH is respected).",
    )
    parser.add_argument(
        "--freq",
        default=os.getenv("QLIB_FREQ", "day"),
        help="Data frequency passed to dump_bin.py (default: day). Use '1min' for intraday.",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("QLIB_REGION", "my"),
        help="Region string passed to qlib.init for the smoke test (default: 'my').",
    )
    parser.add_argument(
        "--include-fields",
        default=os.getenv(
            "QLIB_INCLUDE_FIELDS",
            "open,high,low,close,volume,vwap,amount,factor,turnover,floatshares",
        ),
        help="Comma-separated fields to pass to dump_bin.py.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only fetch + transform; skip writing CSVs and building provider.",
    )
    parser.add_argument(
        "--skip-dump",
        action="store_true",
        help="Skip calling qlib.scripts.dump_bin (CSV export still runs).",
    )
    parser.add_argument(
        "--skip-test",
        action="store_true",
        help="Skip the post-build provider smoke test.",
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    db_url = args.db_url or "sqlite:///app.db"
    sql_query = args.query_file.read_text() if args.query_file else DEFAULT_SQL
    include_fields = [
        field.strip() for field in args.include_fields.split(",") if field.strip()
    ]
    region = args.region  # allow arbitrary region labels, e.g. "my", "custom", etc.

    return Settings(
        db_url=db_url,
        sql_query=sql_query,
        csv_dir=args.csv_dir,
        provider_dir=args.provider_dir,
        freq=args.freq,
        region=region,
        include_fields=include_fields,
        dry_run=args.dry_run,
        skip_dump=args.skip_dump,
        skip_test=args.skip_test,
    )


def fetch_market_data(db_url: str, sql_query: str) -> pd.DataFrame:
    _log(f"Connecting to DB ({db_url})...")
    engine = create_engine(db_url)
    _log("Running market_data query...")
    df = pd.read_sql(sql_query, engine)
    _log(f"Fetched {len(df):,} rows.")
    return df


def transform_to_qlib_format(df: pd.DataFrame) -> pd.DataFrame:
    _log("Transforming columns to Qlib format...")

    df = df.rename(
        columns={
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
            "FloatShares": "floatshares",
        }
    )

    dt = pd.to_datetime(df["DateTime"])
    df["date"] = dt.dt.strftime("%Y-%m-%d")
    df["time"] = dt.dt.strftime("%H:%M:%S")

    cols = [
        "instrument",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "amount",
        "factor",
        "turnover",
        "floatshares",
        "ID",
        "CreatedAt",
        "DateTime",
    ]
    df = df[[col for col in cols if col in df.columns]]

    _log("Column transform complete.")
    return df


def write_per_instrument_csvs(
    df: pd.DataFrame, out_dir: Path, include_fields: List[str]
) -> None:
    _log(f"Writing per-instrument CSVs to {out_dir} ...")
    out_dir.mkdir(parents=True, exist_ok=True)

    missing_fields = [field for field in include_fields if field not in df.columns]
    if missing_fields:
        _log(f"Warning: skipping missing fields {missing_fields}")

    columns = ["instrument", "date", "time"] + [
        field for field in include_fields if field in df.columns
    ]
    if not columns:
        raise ValueError(
            "No columns available to write. Check your include_fields and input data."
        )

    n_instruments = 0
    for instr, sub in df.groupby("instrument"):
        n_instruments += 1
        out_path = out_dir / f"{instr}.csv"
        sub[columns].to_csv(out_path, index=False)

    _log(f"Wrote CSVs for {n_instruments} instruments.")


def run_dump_bin(csv_dir: Path, provider_dir: Path, freq: str, include_fields: List[str]) -> None:
    dump_bin_path = os.path.expanduser("~/projects/qlib/scripts/dump_bin.py")

    if not os.path.exists(dump_bin_path):
        raise FileNotFoundError(f"dump_bin.py not found at: {dump_bin_path}")

    include = ",".join(include_fields)

    cmd = [
        "python",
        dump_bin_path,
        "dump_all",
        "--data_path", str(csv_dir),          # <-- was --csv_path
        "--qlib_dir", str(provider_dir),
        "--file_suffix", ".csv",              # we wrote .csv files
        "--symbol_field_name", "instrument",  # our column name
        "--date_field_name", "date",          # our column name
        "--freq", freq,
        "--include_fields", include,
    ]

    _log("Running dump_bin.py from cloned Qlib repo...")
    _log(" ".join(cmd))
    subprocess.run(cmd, check=True)
    _log("dump_bin.py finished successfully.")



def test_qlib_provider(
    provider_dir: Path,
    region: str,
    freq: str,
    start_date: Optional[str],
    end_date: Optional[str],
) -> None:
    from qlib.config import REG_CN, REG_US
    import qlib

    _log(f"Initializing Qlib provider at {provider_dir} with region={region} ...")

    # Map simple string -> actual region config
    r = region.upper() if isinstance(region, str) else region
    if r == "US":
        region_conf = REG_US
    elif r == "CN":
        region_conf = REG_CN
    else:
        _log(f"Unknown region '{region}', defaulting to US config.")
        region_conf = REG_US

    qlib.init(
        provider_uri=str(provider_dir),
        region=region_conf,
        redis_port=None,
        expression_cache_dir=None,
        dataset_cache_dir=None,
    )

    instruments = list(D.instruments("all"))
    _log(f"Found {len(instruments)} instruments.")
    sample = instruments[:3]
    _log(f"Sample instruments: {sample}")
    if not sample:
        _log("No instruments available to test.")
        return

    df = D.features(
        instruments=sample,
        fields=["$close", "$volume"],
        start_time=start_date,
        end_time=end_date,
        freq=freq,
    )
    _log("Sample features:")
    print(df.head())



def main(argv: Optional[Iterable[str]] = None) -> None:
    settings = parse_args(argv)

    df_raw = fetch_market_data(settings.db_url, settings.sql_query)
    if df_raw.empty:
        _log("No rows returned from the query. Nothing to do.")
        return

    df_qlib = transform_to_qlib_format(df_raw)

    if settings.dry_run:
        _log("Dry run enabled; skipping file export and provider build.")
        return

    write_per_instrument_csvs(df_qlib, settings.csv_dir, settings.include_fields)

    if not settings.skip_dump:
        run_dump_bin(
            settings.csv_dir,
            settings.provider_dir,
            settings.freq,
            settings.include_fields,
        )
    else:
        _log("Skipping dump_bin step.")

    if not settings.skip_test:
        start_date = df_qlib["date"].min() if "date" in df_qlib else None
        end_date = df_qlib["date"].max() if "date" in df_qlib else None
        test_qlib_provider(
            settings.provider_dir,
            settings.region,
            settings.freq,
            start_date,
            end_date,
        )
    else:
        _log("Skipping provider smoke test.")


if __name__ == "__main__":
    main()
