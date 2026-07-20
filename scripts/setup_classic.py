#!/usr/bin/env python
"""
Initialize riskrunway_classic database and create users for Classic Insurance.
Forces connection to the remote RDS riskrunway_classic database directly.
"""
import os
import sys

# MUST set these BEFORE any app imports to override .env
os.environ['FLASK_ENV'] = 'production'
os.environ['DATABASE_URL'] = 'postgresql://riskrunway:RiskRunway2026!@riskrunway-db.cu54eyu4cy2j.us-east-1.rds.amazonaws.com:5432/riskrunway_classic'

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Prevent dotenv from overriding what we just set
import dotenv
dotenv.load_dotenv(override=False)

from app.database import Database
from app.models import User, UserRole

# Connect directly — bypass all the get_db() routing logic
db = Database(os.environ['DATABASE_URL'])
db.init_db()

session = db.get_session()

users = [
    ('chris', 'Chris', 'chris@classicinsurance.com'),
    ('brooke', 'Brooke', 'brooke@classicinsurance.com'),
    ('korrin', 'Korrin', 'korrin@classicinsurance.com'),
]

for username, full_name, email in users:
    existing = session.query(User).filter_by(username=username).first()
    if existing:
        print(f"  Already exists: {username} (id={existing.id})")
        continue
    user = User(
        username=username,
        full_name=full_name,
        email=email,
        role=UserRole.ADMIN,
    )
    user.set_password('Classic2026!')
    session.add(user)
    print(f"  Created: {username}")

session.commit()
session.close()
print("\nDone.")
