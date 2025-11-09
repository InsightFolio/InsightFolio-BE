"""
Database Reset Script
Run this to drop all existing tables and create new ones based on models.py
"""
from app import create_app, db

app = create_app()

with app.app_context():
    print("Dropping all existing tables...")
    db.drop_all()
    print("✓ All tables dropped successfully!")
    
    print("\nCreating new tables based on models.py...")
    db.create_all()
    print("✓ All tables created successfully!")
    
    print("\n✅ Database reset complete!")
    print("\nNew tables created:")
    print("  - users")
    print("  - stocks")
    print("  - scores")
    print("  - logs")
    print("  - transactions")
