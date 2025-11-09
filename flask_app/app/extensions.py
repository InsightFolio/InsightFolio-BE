from flask_SQLAlchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_migrate import Migrate
from .models import User, Stock, Score, Log, Transaction

from flask import Flask
from config import Config
from extensions import db, bcrypt, migrate

db = SQLAlchemy()
bcrypt = Bcrypt()
migrate = Migrate()