# app/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from app.models import Base, Submission, Quote, AuditLog, AppetiteRule, Broker
import os


class Database:
    """Database manager for the application"""
    
    def __init__(self, db_path=None):
        """
        Initialize database connection.
        
        Args:
            db_path: Path to SQLite database file. If None, uses config default.
        """
        if db_path is None:
            from config import Config
            db_path = Config.DATABASE_PATH
        
        # Ensure directory exists
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
        
        # Create engine
        self.engine = create_engine(f'sqlite:///{db_path}', echo=False)
        
        # Create session factory
        self.Session = scoped_session(sessionmaker(bind=self.engine))
    
    def init_db(self):
        """Create all tables in the database"""
        Base.metadata.create_all(self.engine)
        print(f"Database initialized successfully")
    
    def drop_all(self):
        """Drop all tables (use with caution!)"""
        Base.metadata.drop_all(self.engine)
        print("All tables dropped")
    
    def get_session(self):
        """Get a new database session"""
        return self.Session()
    
    def close_session(self):
        """Close the scoped session"""
        self.Session.remove()


# Global database instances
_db = None
_current_db_name = 'production'  # Default database
_db_instances = {}  # Cache for database instances


def get_current_db_name():
    """Get the name of the currently active database"""
    return _current_db_name


def set_current_db(db_name):
    """
    Switch to a different database.

    Args:
        db_name: Name of the database ('production', 'use_cases', 'test')

    Returns:
        bool: True if successful, False if database name is invalid
    """
    global _current_db_name, _db
    from config import Config

    if db_name not in Config.DATABASES:
        return False

    _current_db_name = db_name

    # Get or create database instance
    if db_name not in _db_instances:
        _db_instances[db_name] = Database(db_path=Config.DATABASES[db_name])
        _db_instances[db_name].init_db()  # Ensure tables exist

    _db = _db_instances[db_name]
    return True


def get_db():
    """Get the current database instance"""
    global _db
    if _db is None:
        # Initialize with default database
        set_current_db(_current_db_name)
    return _db


def get_available_databases():
    """Get list of available database names"""
    from config import Config
    return list(Config.DATABASES.keys())


def init_db():
    """Initialize the database (create tables)"""
    db = get_db()
    db.init_db()
    _ensure_schema_updates(db.engine)


def _ensure_schema_updates(engine):
    """Apply lightweight schema updates for existing SQLite DBs."""
    with engine.begin() as conn:
        quote_columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(quotes)").fetchall()]
        if 'quote_outcome' not in quote_columns:
            conn.exec_driver_sql("ALTER TABLE quotes ADD COLUMN quote_outcome VARCHAR(20)")
            print("Applied schema update: added quotes.quote_outcome")

        submission_columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(submissions)").fetchall()]
        if 'status_label' not in submission_columns:
            conn.exec_driver_sql("ALTER TABLE submissions ADD COLUMN status_label VARCHAR(255)")
            print("Applied schema update: added submissions.status_label")

        broker_columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(brokers)").fetchall()]
        if 'letterhead' not in broker_columns:
            conn.exec_driver_sql("ALTER TABLE brokers ADD COLUMN letterhead TEXT")
            print("Applied schema update: added brokers.letterhead")
        if 'email_body' not in broker_columns:
            conn.exec_driver_sql("ALTER TABLE brokers ADD COLUMN email_body TEXT")
            print("Applied schema update: added brokers.email_body")
        if 'created_at' not in broker_columns:
            conn.exec_driver_sql("ALTER TABLE brokers ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP")
            print("Applied schema update: added brokers.created_at")
        if 'updated_at' not in broker_columns:
            conn.exec_driver_sql("ALTER TABLE brokers ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP")
            print("Applied schema update: added brokers.updated_at")


def get_session():
    """Get a database session"""
    db = get_db()
    return db.get_session()


# Helper functions for common operations
def create_submission(insured_name, effective_date, state=None, user=None, assigned_to=None):
    """
    Create a new submission and log the action.

    Returns:
        int: The ID of the created submission
    """
    from app.models import Submission, SubmissionStatus, AuditLog

    session = get_session()
    try:
        submission = Submission(
            insured_name=insured_name,
            effective_date=effective_date,
            state=state,
            status=SubmissionStatus.RECEIVED,
            assigned_to=assigned_to
        )
        session.add(submission)
        session.flush()  # Get the ID

        submission_id = submission.id

        # Create audit log
        audit = AuditLog(
            entity_type='submission',
            entity_id=submission_id,
            submission_id=submission_id,
            action='created',
            user=user,
            details=f"Created submission for {insured_name}"
        )
        session.add(audit)

        session.commit()

        return submission_id
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


# def create_quote(submission_id, carrier_name, raw_document_path, extracted_json, user=None,
#                  pass1_layout_json=None, pass3_intent_json=None, quote_intent=None, comparison_group=None):
def create_quote(submission_id, carrier_name, raw_document_path, extracted_json, user=None,
                 pass1_layout_json=None):
   
    
    """
    Create a new quote and log the action.

    Returns:
        int: The ID of the created quote
    """
    from app.models import Quote, QuoteStatus, AuditLog

    session = get_session()
    try:
        quote = Quote(
            submission_id=submission_id,
            carrier_name=carrier_name,
            raw_document_path=raw_document_path,
            extracted_json=extracted_json,
            pass1_layout_json=pass1_layout_json,
            # pass3_intent_json=pass3_intent_json,
            # quote_intent=quote_intent,
            # comparison_group=comparison_group,
            status=QuoteStatus.RECEIVED
        )
        session.add(quote)
        session.flush()  # Get the ID

        quote_id = quote.id

        # Create audit log
        audit = AuditLog(
            entity_type='quote',
            entity_id=quote_id,
            submission_id=submission_id,
            quote_id=quote_id,
            action='uploaded',
            user=user,
            details=f"Uploaded quote from {carrier_name or 'unknown carrier'}"
        )
        session.add(audit)

        session.commit()

        # Update appetite score for the submission
        update_submission_appetite_score(submission_id)

        return quote_id
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


def log_action(entity_type, entity_id, action, user=None, details=None, submission_id=None, quote_id=None):
    """
    Log an action to the audit trail.
    
    Args:
        entity_type: 'submission' or 'quote'
        entity_id: ID of the entity
        action: Action performed (e.g., 'parsed', 'chosen', 'exported')
        user: Username (optional)
        details: Additional details (optional)
        submission_id: Related submission ID (optional)
        quote_id: Related quote ID (optional)
    """
    from app.models import AuditLog
    
    session = get_session()
    try:
        audit = AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            user=user,
            details=details,
            submission_id=submission_id,
            quote_id=quote_id
        )
        session.add(audit)
        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


def get_all_submissions():
    """Get all submissions with quote counts."""
    from sqlalchemy.orm import joinedload

    session = get_session()
    try:
        submissions = session.query(Submission).options(
            joinedload(Submission.quotes)
        ).order_by(Submission.created_at.desc()).all()

        return [s.to_dict() for s in submissions]
    finally:
        session.close()


def get_submission_by_id(submission_id):
    """
    Get a submission by ID with all its quotes.

    Returns:
        dict: Submission data with quotes, or None if not found
    """
    from sqlalchemy.orm import joinedload

    session = get_session()
    try:
        submission = session.query(Submission).options(
            joinedload(Submission.quotes)
        ).filter_by(id=submission_id).first()

        if submission:
            # Convert to dict while session is still open
            result = submission.to_dict()
            # Add full quote data
            result['quotes'] = [q.to_dict() for q in submission.quotes]
            return result
        return None
    finally:
        session.close()


def update_submission_appetite_score(submission_id):
    """
    Calculate and update the PF appetite score for a submission.

    Args:
        submission_id: ID of the submission to update
    """
    from app.appetite_scoring import calculate_appetite_score

    session = get_session()
    try:
        # Get submission
        submission = session.query(Submission).filter_by(id=submission_id).first()
        if not submission:
            return

        # Get submission data
        submission_data = submission.to_dict()

        # Get all quotes for this submission
        quotes_data = [q.to_dict() for q in submission.quotes]

        # Calculate appetite score
        score_result = calculate_appetite_score(submission_data, quotes_data)

        # Update submission
        submission.appetite_score = score_result['total_score']

        session.commit()

        print(f"Updated appetite score for submission {submission_id}: {score_result['total_score']}/100 ({score_result['rating']})")

    except Exception as e:
        session.rollback()
        print(f"Error updating appetite score: {e}")
    finally:
        session.close()
