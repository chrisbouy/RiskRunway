#!/usr/bin/env python3
"""
Database Manager Utility

Manage multiple databases (production, use_cases, test) for RiskRunway Mapper.
Provides commands to:
- Initialize databases
- Seed test data
- Switch between databases
- Clear databases
"""

import sys
import os
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from app.database import Database, set_current_db
from app.models import User, Submission, Quote, SubmissionStatus, QuoteStatus, UserRole
from datetime import datetime, timedelta


def init_database(db_name):
    """Initialize a database with tables"""
    if db_name not in Config.DATABASES:
        print(f"❌ Invalid database name: {db_name}")
        print(f"Available databases: {', '.join(Config.DATABASES.keys())}")
        return False
    
    db_path = Config.DATABASES[db_name]
    print(f"Initializing {db_name} database at: {db_path}")
    
    db = Database(db_path=db_path)
    db.init_db()
    print(f"✅ {db_name} database initialized successfully")
    return True


def seed_use_cases_db():
    """Seed the use_cases database with test scenarios"""
    print("Seeding use_cases database with test scenarios...")
    
    db_path = Config.DATABASES['use_cases']
    db = Database(db_path=db_path)
    session = db.get_session()
    
    try:
        # Create test user if not exists
        test_user = session.query(User).filter_by(username='test_user').first()
        if not test_user:
            test_user = User(
                username='test_user',
                full_name='Test User',
                role=UserRole.ADMIN,
                is_active=True
            )
            test_user.set_password('test123')
            session.add(test_user)
            session.commit()
            print("✅ Created test user (username: test_user, password: test123)")
        
        # Create test scenarios
        scenarios = [
            {
                'insured_name': 'Test Scenario 1: Simple GL Quote',
                'effective_date': (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d'),
                'state': 'CA',
                'status': SubmissionStatus.IN_PROGRESS,
                'status_label': 'Waiting on GL quote from carrier'
            },
            {
                'insured_name': 'Test Scenario 2: Multi-Coverage Package',
                'effective_date': (datetime.now() + timedelta(days=45)).strftime('%Y-%m-%d'),
                'state': 'TX',
                'status': SubmissionStatus.CHOSEN,
                'status_label': 'Package quote selected, ready to bind'
            },
            {
                'insured_name': 'Test Scenario 3: Workers Comp Renewal',
                'effective_date': (datetime.now() + timedelta(days=60)).strftime('%Y-%m-%d'),
                'state': 'NY',
                'status': SubmissionStatus.SENT_TO_FINANCE,
                'status_label': 'Renewal quote sent to PF'
            },
            {
                'insured_name': 'Test Scenario 4: Cyber Liability',
                'effective_date': (datetime.now() + timedelta(days=15)).strftime('%Y-%m-%d'),
                'state': 'FL',
                'status': SubmissionStatus.RECEIVED,
                'status_label': 'New cyber quote request'
            },
            {
                'insured_name': 'Test Scenario 5: Commercial Auto',
                'effective_date': (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%d'),
                'state': 'IL',
                'status': SubmissionStatus.IN_PROGRESS,
                'status_label': 'Comparing auto quotes from 3 carriers'
            }
        ]
        
        for scenario in scenarios:
            # Check if scenario already exists
            existing = session.query(Submission).filter_by(
                insured_name=scenario['insured_name']
            ).first()
            
            if not existing:
                submission = Submission(
                    insured_name=scenario['insured_name'],
                    effective_date=scenario['effective_date'],
                    state=scenario['state'],
                    status=scenario['status'],
                    status_label=scenario['status_label'],
                    assigned_to=test_user.id
                )
                session.add(submission)
                print(f"✅ Created: {scenario['insured_name']}")
        
        session.commit()
        print(f"✅ Use cases database seeded successfully")
        
    except Exception as e:
        session.rollback()
        print(f"❌ Error seeding database: {e}")
        raise
    finally:
        session.close()


def clear_database(db_name):
    """Clear all data from a database"""
    if db_name not in Config.DATABASES:
        print(f"❌ Invalid database name: {db_name}")
        return False
    
    if db_name == 'production':
        confirm = input("⚠️  WARNING: You are about to clear the PRODUCTION database. Type 'YES' to confirm: ")
        if confirm != 'YES':
            print("❌ Aborted")
            return False
    
    db_path = Config.DATABASES[db_name]
    print(f"Clearing {db_name} database at: {db_path}")
    
    db = Database(db_path=db_path)
    db.drop_all()
    db.init_db()
    print(f"✅ {db_name} database cleared and reinitialized")
    return True


def list_databases():
    """List all available databases"""
    print("\n📊 Available Databases:")
    print("-" * 60)
    for name, path in Config.DATABASES.items():
        exists = "✅" if os.path.exists(path) else "❌"
        size = ""
        if os.path.exists(path):
            size_bytes = os.path.getsize(path)
            size = f"({size_bytes / 1024:.1f} KB)"
        print(f"{exists} {name:15} {path} {size}")
    print("-" * 60)


def main():
    """Main CLI interface"""
    if len(sys.argv) < 2:
        print("""
Database Manager for RiskRunway Mapper

Usage:
    python utils/db_manager.py <command> [args]

Commands:
    list                    - List all available databases
    init <db_name>          - Initialize a database
    seed use_cases          - Seed use_cases database with test scenarios
    clear <db_name>         - Clear and reinitialize a database
    
Examples:
    python utils/db_manager.py list
    python utils/db_manager.py init use_cases
    python utils/db_manager.py seed use_cases
    python utils/db_manager.py clear test
        """)
        return
    
    command = sys.argv[1]
    
    if command == 'list':
        list_databases()
    
    elif command == 'init':
        if len(sys.argv) < 3:
            print("❌ Usage: python utils/db_manager.py init <db_name>")
            return
        db_name = sys.argv[2]
        init_database(db_name)
    
    elif command == 'seed':
        if len(sys.argv) < 3:
            print("❌ Usage: python utils/db_manager.py seed <db_name>")
            return
        db_name = sys.argv[2]
        if db_name == 'use_cases':
            seed_use_cases_db()
        else:
            print(f"❌ No seed data available for {db_name}")
    
    elif command == 'clear':
        if len(sys.argv) < 3:
            print("❌ Usage: python utils/db_manager.py clear <db_name>")
            return
        db_name = sys.argv[2]
        clear_database(db_name)
    
    else:
        print(f"❌ Unknown command: {command}")
        print("Run without arguments to see usage")


if __name__ == '__main__':
    main()

