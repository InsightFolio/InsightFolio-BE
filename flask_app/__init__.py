"""Flask application package for InsightFolio."""

# Re-export the factory and extensions from the app subpackage so callers can
# continue to import from `flask_app` if they prefer.
from flask_app.app import bcrypt, create_app, db, jwt  # noqa: F401
