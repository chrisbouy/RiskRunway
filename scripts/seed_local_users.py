#!/usr/bin/env python
"""
One-off local maintenance script.

Goal: make the three switchable local DBs (dev / use_cases / test) contain the
SAME three users with the SAME ids, so switching DBs in-app without re-login
never hits a missing assigned_to FK.

Target users (identical across all three DBs):
  id=2  chrisbouy   ADMIN  (keeps existing id so current session stays valid)
  id=3  AgentSmith  AGENT
  id=5  brookebouy  ADMIN

Steps:
  dev:
    - reassign brokers.user_id from 1 -> 2 (orphan cleanup before delete)
    - ensure chrisbouy(2), brookebouy(5) exist; create AgentSmith(3)
    - delete every other user
  use_cases + test:
    - wipe users table, insert the same 3 rows (copying hashes from dev)
    - reset id sequence

Run directly with the local Postgres. Does NOT touch prod/RDS.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash
from sqlalchemy import create_engine, text

LOCAL = "postgresql://riskrunway:localpass123@localhost:5432/"
DEV = LOCAL + "riskrunway_dev"
USE_CASES = LOCAL + "riskrunway_use_cases"
TEST = LOCAL + "riskrunway_test"

KEEP = {"chrisbouy", "AgentSmith", "brookebouy"}

AGENT_SMITH_PASSWORD = "PaulinInsurance2026!"


def clean_dev():
    engine = create_engine(DEV)
    with engine.begin() as conn:
        # 1. Reassign orphan brokers (user_id=1 admin1) to chrisbouy(2) before deleting.
        conn.execute(text("UPDATE brokers SET user_id = 2 WHERE user_id = 1"))

        # 2. Create AgentSmith at id=3 if not present.
        exists = conn.execute(
            text("SELECT 1 FROM users WHERE username = 'AgentSmith'")
        ).fetchone()
        if not exists:
            conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, full_name, email, role, created_at, is_active, ams_agent_installed, sms_alerts_enabled) "
                    "VALUES (3, 'AgentSmith', :ph, 'Agent Smith', NULL, 'AGENT', NOW(), true, false, false)"
                ),
                {"ph": generate_password_hash(AGENT_SMITH_PASSWORD)},
            )

        # 3. Delete every user not in KEEP.
        conn.execute(
            text("DELETE FROM users WHERE username NOT IN ('chrisbouy','AgentSmith','brookebouy')")
        )

        # 4. Fix sequence so new inserts don't collide.
        conn.execute(text("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))"))

    rows = read_users(DEV)
    print(f"[dev] users after cleanup: {rows}")
    return rows


def read_users(url):
    engine = create_engine(url)
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id, username, password_hash, full_name, email, role, is_active FROM users ORDER BY id")
        ).fetchall()


def mirror_to(url, source_rows, label):
    engine = create_engine(url)
    with engine.begin() as conn:
        # Wipe dependent broker rows first (FK), then users. Data not a priority here.
        conn.execute(text("DELETE FROM brokers"))
        conn.execute(text("DELETE FROM users"))
        for r in source_rows:
            conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, full_name, email, role, created_at, is_active, ams_agent_installed, sms_alerts_enabled) "
                    "VALUES (:id, :username, :ph, :full_name, :email, :role, NOW(), :is_active, false, false)"
                ),
                {
                    "id": r.id,
                    "username": r.username,
                    "ph": r.password_hash,
                    "full_name": r.full_name,
                    "email": r.email,
                    "role": r.role,
                    "is_active": r.is_active,
                },
            )
        conn.execute(text("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))"))
    print(f"[{label}] users after mirror: {read_users(url)}")


if __name__ == "__main__":
    dev_rows = clean_dev()
    mirror_to(USE_CASES, dev_rows, "use_cases")
    mirror_to(TEST, dev_rows, "test")
    print("\nDone. All three DBs now share the same 3 users.")
