import pandas as pd

from flask_app.app.build_qlib_dataset import (
    transform_to_qlib_format,
    write_per_instrument_csvs,
)


def test_transform_to_qlib_format_normalizes_columns():
    df = pd.DataFrame(
        {
            "ID": [1],
            "DateTime": ["2024-01-02 15:30:00"],
            "Instrument": ["AAA"],
            "Open": [1.0],
            "High": [2.0],
            "Low": [0.5],
            "Close": [1.5],
            "Volume": [100],
            "VWAP": [1.2],
            "Amount": [120.0],
            "Factor": [1.0],
            "Turnover": [0.2],
            "FloatShares": [200],
            "CreatedAt": ["2024-01-02 15:35:00"],
        }
    )

    transformed = transform_to_qlib_format(df)

    assert set(["instrument", "date", "time"]).issubset(transformed.columns)
    assert transformed.loc[0, "instrument"] == "AAA"
    assert transformed.loc[0, "date"] == "2024-01-02"
    assert transformed.loc[0, "time"] == "15:30:00"
    assert transformed.loc[0, "close"] == 1.5


def test_write_per_instrument_csvs_writes_files(tmp_path):
    df = pd.DataFrame(
        {
            "instrument": ["AAA", "AAA", "BBB"],
            "date": ["2024-01-01", "2024-01-02", "2024-01-01"],
            "time": ["00:00:00", "00:00:00", "00:00:00"],
            "open": [1, 2, 3],
            "close": [1.5, 2.5, 3.5],
            "volume": [10, 20, 30],
        }
    )

    write_per_instrument_csvs(df, tmp_path, ["open", "close", "volume"])

    aaa_file = tmp_path / "AAA.csv"
    bbb_file = tmp_path / "BBB.csv"
    assert aaa_file.exists()
    assert bbb_file.exists()

    aaa_lines = aaa_file.read_text().strip().splitlines()
    bbb_lines = bbb_file.read_text().strip().splitlines()
    assert len(aaa_lines) == 3  # header + 2 rows
    assert len(bbb_lines) == 2  # header + 1 row
