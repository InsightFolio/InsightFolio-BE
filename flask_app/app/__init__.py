from flask import Flask
from .extensions import db, bcrypt, jwt
from flask_cors import CORS
from dotenv import load_dotenv
import os
from pathlib import Path


def create_app(register_routes: bool = True, database_url: str | None = None) -> Flask:
    """
    Create and configure the Flask application.

    Args:
        register_routes: Whether to register the main blueprint (set False for offline scripts).
        database_url: Optional SQLAlchemy database URL override (defaults to env or local SQLite).
    """
    load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

    app = Flask(__name__)
    default_sqlite = Path(__file__).resolve().parents[2] / "app.db"
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        database_url or os.getenv("DATABASE_URL") or f"sqlite:///{default_sqlite}"
    )
    app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")

    CORS(app)
    db.init_app(app)
    bcrypt.init_app(app)
    jwt.init_app(app)

    if register_routes:
        from .routes import routes_bp
        app.register_blueprint(routes_bp)
    return app
