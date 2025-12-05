import sys
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token

# Ensure the project root is on sys.path so ``flask_app`` can be imported when
# tests are run from nested directories.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask_app.app import create_app
from flask_app.app.extensions import db
from flask_app.app.models import User


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """Flask app configured with a temporary SQLite database."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("JWT_SECRET_KEY", "testing-secret")
    app = create_app()
    app.config.update({"TESTING": True})
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    with app.test_client() as client:
        yield client


@pytest.fixture()
def db_session(app):
    """Provide a clean session per test."""
    with app.app_context():
        yield db.session
        db.session.rollback()


@pytest.fixture()
def user_factory(db_session):
    """Factory to create users with hashed passwords."""
    def _create_user(username="user", email="user@example.com", password="password", **kwargs):
        user = User(username=username, email=email, **kwargs)
        user.set_password(password)
        db_session.add(user)
        db_session.commit()
        return user
    return _create_user


@pytest.fixture()
def auth_header(app):
    """Return a callable that builds an Authorization header for a user id."""
    def _make(user_id):
        with app.app_context():
            token = create_access_token(identity=str(user_id))
        return {"Authorization": f"Bearer {token}"}
    return _make
