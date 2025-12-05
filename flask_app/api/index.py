# api/index.py

import os

# Adjust import depending on your __init__.py design
from app import create_app  # if you use factory style
# from app import app as flask_app  # if you use global app

# Create Flask app for Vercel
app = create_app()  # factory style
# app = flask_app   # global style
