#!/usr/bin/env python3
"""
Setup script for Paulin Insurance Agency tenant.

This script:
1. Creates the riskrunway_paulin database on the same RDS/local PostgreSQL instance
2. Initializes the schema (all tables)
3. Creates user accounts for Bryce Paulin and Alissa Paulin

Run from project root:
    python scripts/setup_tenant_paulin.py

Prerequisites:
    - PostgreSQL running with the 'riskrunway' role having CREATEDB privilege
    - .env loaded (or DATABASE_URL set)
"""
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def get_admin_connection_url():
    """Get a connection URL that can create databases (connect to 'postgres' db)."""
    base_url = os.environ.get('DATABASE_URL', '')
    if not base_url:
        print("ERROR: DATABASE_URL not set in environment")
        sys.exit(1)
    url = make_url(base_url)
    # Connect to 'postgres' database to run CREATE DATABASE
    return url.set(database='postgres').render_as_string(hide_password=False)


def get_paulin_database_url():
    """Get the Paulin tenant database URL."""
    base_url = os.environ.get('DATABASE_URL', '')
    url = make_url(base_url)
    return url.set(database='riskrunway_paulin').render_as_string(hide_password=False)


def create_database():
    """Create the riskrunway_paulin database if it doesn't exist."""
    admin_url = get_admin_connection_url()
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")

    with engine.connect() as conn:
        # Check if database already exists
        result = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = 'riskrunway_paulin'")
        ).fetchone()

        if result:
            print("✓ Database 'riskrunway_paulin' already exists")
        else:
            conn.execute(text("CREATE DATABASE riskrunway_paulin"))
            print("✓ Created database 'riskrunway_paulin'")

    engine.dispose()


def initialize_schema():
    """Create all tables in the Paulin database."""
    from app.models import Base
    from app.database import Database

    paulin_url = get_paulin_database_url()
    db = Database(paulin_url)
    db.init_db()
    print("✓ Schema initialized (all tables created)")


def create_users():
    """Create Bryce Paulin and Alissa Paulin user accounts."""
    from app.models import User, UserRole
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy import create_engine

    paulin_url = get_paulin_database_url()
    engine = create_engine(paulin_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    users_to_create = [
        {
            'username': 'brycepaulin',
            'full_name': 'Bryce Paulin',
            'email': 'bryce@paulininsurance.com',
            'role': UserRole.ADMIN,
            'password': 'PaulinInsurance2026!',
        },
        {
            'username': 'alissapaulin',
            'full_name': 'Alissa Paulin',
            'email': 'alissa@paulininsurance.com',
            'role': UserRole.AGENT,
            'password': 'PaulinInsurance2026!',
        },
    ]

    for user_data in users_to_create:
        existing = session.query(User).filter_by(username=user_data['username']).first()
        if existing:
            print(f"  ⚠ User '{user_data['username']}' already exists (id={existing.id}), skipping")
            continue

        user = User(
            username=user_data['username'],
            full_name=user_data['full_name'],
            email=user_data['email'],
            role=user_data['role'],
            is_active=True,
        )
        user.set_password(user_data['password'])
        session.add(user)
        print(f"  ✓ Created user: {user_data['full_name']} ({user_data['username']})")

    session.commit()
    session.close()
    engine.dispose()


def main():
    print("=" * 60)
    print("  Paulin Insurance Agency - Tenant Setup")
    print("=" * 60)
    print()

    print("[1/3] Creating database...")
    create_database()
    print()

    print("[2/3] Initializing schema...")
    initialize_schema()
    print()

    print("[3/3] Creating user accounts...")
    create_users()
    print()

    print("=" * 60)
    print("  Setup complete!")
    print()
    print("  Tenant: paulin")
    print("  Subdomain: paulin.risk-runway.com")
    print("  Database: riskrunway_paulin")
    print()
    print("  Users created:")
    print("    - brycepaulin / PaulinInsurance2026!")
    print("    - alissapaulin / PaulinInsurance2026!")
    print()
    print("  Next steps:")
    print("    1. Add DNS CNAME: paulin.risk-runway.com → your ALB")
    print("    2. Add paulin.risk-runway.com to your ALB/ACM certificate")
    print("    3. Update ECS task env vars with TENANT_DATABASE_MAP")
    print("    4. Deploy")
    print("=" * 60)


if __name__ == '__main__':
    main()
