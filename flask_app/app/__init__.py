"""Application factory and shared extensions for the Flask app."""

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask
from flask_bcrypt import Bcrypt
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
bcrypt = Bcrypt()
jwt = JWTManager()


def create_app() -> Flask:
    """Create and configure the Flask application."""
    load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
    app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")

    CORS(app)
    db.init_app(app)
    bcrypt.init_app(app)
    jwt.init_app(app)

    from .routes import routes_bp

    app.register_blueprint(routes_bp)
    return app


__all__ = ["create_app", "db", "bcrypt", "jwt"]
