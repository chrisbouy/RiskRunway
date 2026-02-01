# app/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from app.models import Base, Submission, Quote, AuditLog, AppetiteRule
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


# Global database instance
_db = None


def get_db():
    """Get the global database instance"""
    global _db
    if _db is None:
        _db = Database()
    return _db


def init_db():
    """Initialize the database (create tables)"""
    db = get_db()
    db.init_db()


def get_session():
    """Get a database session"""
    db = get_db()
    return db.get_session()


# Helper functions for common operations
def create_submission(insured_name, effective_date, state=None, user=None):
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
            status=SubmissionStatus.RECEIVED
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


def create_quote(submission_id, carrier_name, raw_document_path, extracted_json, user=None):
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
    """Get all submissions with quote counts (only returns submissions with at least 1 quote)"""
    from sqlalchemy.orm import joinedload

    session = get_session()
    try:
        submissions = session.query(Submission).options(
            joinedload(Submission.quotes)
        ).order_by(Submission.created_at.desc()).all()

        # Only return submissions that have at least one quote
        result = []
        for s in submissions:
            s_dict = s.to_dict()
            if s_dict['quote_count'] > 0:
                result.append(s_dict)

        return result
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

