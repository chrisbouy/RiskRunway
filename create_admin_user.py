#!/usr/bin/env python
"""
Script to create an admin user in the database.
Usage: python create_admin_user.py
"""
import sys
sys.path.insert(0, '.')

from app.database import init_db, get_session
from app.models import User, UserRole


def create_admin_user(username='admin1', password='123', full_name='Admin User'):
    """Create an admin user."""
    # Initialize database (creates tables if they don't exist)
    init_db()
    print("Database initialized.")
    
    session = get_session()
    try:
        # Check if user already exists
        existing = session.query(User).filter_by(username=username).first()
        if existing:
            print(f"User '{username}' already exists (id={existing.id})")
            return existing
        
        # Create new admin user
        user = User(
            username=username,
            full_name=full_name,
            role=UserRole.ADMIN
        )
        user.set_password(password)
        
        session.add(user)
        session.commit()
        
        print(f"Created user '{username}' with id={user.id}, role={user.role.value}")
        return user
        
    except Exception as e:
        session.rollback()
        print(f"Error creating user: {e}")
        raise
    finally:
        session.close()


if __name__ == '__main__':
    create_admin_user('admin1', '123', 'Admin User')