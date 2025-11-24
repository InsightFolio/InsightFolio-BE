import json
from unittest.mock import patch

import pytest

from flask_app.app import create_app


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("JWT_SECRET_KEY", "testing")
    app = create_app()
    app.config.update({"TESTING": True})
    with app.test_client() as client:
        yield client


@patch("flask_app.app.routes.run_scoring_workflow")
def test_score_endpoint_uses_payload_config(mock_run_scoring_workflow, client):
    mock_run_scoring_workflow.return_value = {
        "results": [{"symbol": "AAPL", "score": 0.1, "timestamp": "2024-01-01"}],
        "config": {"symbols": ["AAPL"], "region": "US"},
    }

    response = client.post(
        "/score",
        data=json.dumps({"config": {"symbols": ["AAPL"], "region": "US"}}),
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["results"][0]["symbol"] == "AAPL"
    assert mock_run_scoring_workflow.called
