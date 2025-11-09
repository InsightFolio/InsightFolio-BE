import os

class Config:
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        # MySQL example (PyMySQL):
        "mysql+pymysql://user:pass@localhost:3306/insightfolio?charset=utf8mb4"
        # Postgres example:
        # "postgresql+psycopg2://user:pass@localhost:5432/insightfolio"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
