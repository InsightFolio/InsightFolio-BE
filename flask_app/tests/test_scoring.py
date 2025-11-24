from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd

from flask_app.app.scoring import (
    QlibConfig,
    _parse_symbols,
    format_scores,
    load_config_from_env,
    run_scoring_workflow,
)


def test_parse_symbols_handles_string_and_default():
    assert _parse_symbols("AAPL,MSFT") == ["AAPL", "MSFT"]
    assert _parse_symbols(["AAPL"]) == ["AAPL"]
    assert _parse_symbols(None) == []


def test_load_config_from_env(monkeypatch):
    monkeypatch.setenv("QLIB_SYMBOLS", "SPY,QQQ")
    cfg = load_config_from_env()
    assert cfg.symbols == ["SPY", "QQQ"]
    assert cfg.region == "US"


@patch("flask_app.app.scoring.train_model")
@patch("flask_app.app.scoring.build_dataset")
@patch("flask_app.app.scoring.ensure_qlib_initialized")
def test_run_scoring_workflow(mock_init, mock_build_dataset, mock_train_model):
    dataset = MagicMock()
    mock_build_dataset.return_value = dataset
    model = MagicMock()
    mock_train_model.return_value = model
    model.predict.return_value = pd.Series(
        [0.1, 0.2], index=[("AAPL", datetime(2024, 1, 1)), ("MSFT", datetime(2024, 1, 1))]
    )

    config = QlibConfig(
        provider_uri="/tmp/qlib_data",
        region="US",
        symbols=["AAPL", "MSFT"],
        start_date="2020-01-01",
        end_date="2020-12-31",
        train_end_date="2020-06-30",
        cache_dir="/tmp/qlib_cache",
    )

    response = run_scoring_workflow(config)
    assert mock_init.called
    assert mock_build_dataset.called
    assert mock_train_model.called
    assert len(response["results"]) == 2


@patch("flask_app.app.scoring._serialize_timestamp", lambda value: "2024-01-01")
def test_format_scores_handles_series_and_dataframe():
    series = pd.Series([0.5], index=[("AAPL", datetime(2024, 1, 1))])
    df = pd.DataFrame({"score": [0.7]}, index=[("MSFT", datetime(2024, 1, 1))])

    series_scores = format_scores(series)
    df_scores = format_scores(df)

    assert series_scores[0]["symbol"] == "AAPL"
    assert series_scores[0]["score"] == 0.5
    assert df_scores[0]["symbol"] == "MSFT"
    assert df_scores[0]["score"] == 0.7
