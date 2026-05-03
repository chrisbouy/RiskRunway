# app/database.py
import os
from contextvars import ContextVar
from sqlalchemy import create_engine, event, text, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool
from app.models import Base, Submission, Quote, AuditLog, AppetiteRule, Broker, EmailMessage, EmailAttachment, ConnectedAccount, UserRole, SubmissionStatus, QuoteStatus, EmailProvider, ConnectedAccountStatus, DocumentType


class Database:
    """Database manager for PostgreSQL (RDS)"""
    
    def __init__(self, db_url=None):
        """
        Initialize PostgreSQL database connection.
        
        Args:
            db_url: SQLAlchemy PostgreSQL URL (postgresql://...)
                    Must be provided via DATABASE_URL env var
        """
        db_url = db_url or os.environ.get('DATABASE_URL')
        if not db_url:
            raise ValueError(
                "DATABASE_URL environment variable is required. "
                "Example: postgresql://user:pass@host:5432/dbname"
            )
        
        # Connection pooling for PostgreSQL
        self.engine = create_engine(
            db_url,
            echo=False,
            poolclass=QueuePool,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,      # Verify connections before use
            pool_recycle=1800,       # Recycle after 30 minutes
            pool_use_lifo=True       # LIFO to reduce idle connections
        )
        
        # Create session factory
        self.Session = scoped_session(sessionmaker(bind=self.engine))
    
    def init_db(self):
        """Create all tables in the database"""
        Base.metadata.create_all(self.engine)
        _ensure_schema_updates(self.engine)
        print(f"Database initialized: {self.engine.url.database}")
    
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


# Global database instances, keyed by configured database name.
_db_cache = {}
_current_db_name = ContextVar('current_db_name', default='production')


def _is_truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _is_falsey(value):
    return str(value or '').strip().lower() in ('0', 'false', 'no', 'off')


def is_production_environment():
    """Return True when the app is running in a deployed/production environment."""
    env_name = (
        os.environ.get('FLASK_ENV')
        or os.environ.get('APP_ENV')
        or os.environ.get('ENVIRONMENT')
        or ''
    ).strip().lower()
    return env_name in ('production', 'prod') or _is_truthy(os.environ.get('RENDER'))


def is_database_switching_enabled():
    """Database switching is available in dev/local only, unless explicitly disabled."""
    if is_production_environment():
        return False
    return not _is_falsey(os.environ.get('ALLOW_DATABASE_SWITCHING'))


def _derive_database_url(base_url, suffix):
    """Derive a sibling Postgres database URL by appending a suffix to the DB name."""
    if not base_url:
        return None

    try:
        url = make_url(base_url)
    except Exception:
        return None

    if not url.get_backend_name().startswith('postgresql') or not url.database:
        return None

    return url.set(database=f"{url.database}_{suffix}").render_as_string(hide_password=False)


def get_configured_databases():
    """Return Postgres database targets configured for this environment."""
    production_url = os.environ.get('DATABASE_URL')
    databases = {
        'production': production_url,
        'development': (
            os.environ.get('DEVELOPMENT_DATABASE_URL')
            or os.environ.get('DEV_DATABASE_URL')
            or os.environ.get('LOCAL_DATABASE_URL')
            or _derive_database_url(production_url, 'dev')
        ),
        'use_cases': (
            os.environ.get('USE_CASES_DATABASE_URL')
            or os.environ.get('USE_CASE_DATABASE_URL')
            or _derive_database_url(production_url, 'use_cases')
        ),
        'test': (
            os.environ.get('TEST_DATABASE_URL')
            or _derive_database_url(production_url, 'test')
        ),
    }
    return {name: url for name, url in databases.items() if url}


def get_db():
    """Get the selected database instance for this request/context."""
    db_name = get_current_db_name()
    db_url = get_configured_databases().get(db_name)
    if not db_url:
        raise ValueError(f"Database is not configured: {db_name}")

    if db_name not in _db_cache:
        _db_cache[db_name] = Database(db_url)
        _db_cache[db_name].init_db()
    return _db_cache[db_name]


def get_session():
    """Get a database session (convenience function)"""
    return get_db().get_session()


def init_db():
    """Initialize the database (create tables)"""
    db = get_db()
    db.init_db()


def _ensure_schema_updates(engine):
    """
    Apply schema updates for PostgreSQL.
    Ensures ENUM types exist and adds any missing columns.
    """
    with engine.begin() as conn:
        inspector = inspect(engine)
        
        # Ensure ENUM types exist in PostgreSQL
        enum_types = {
            'userrole': UserRole,
            'submissionstatus': SubmissionStatus,
            'quotestatus': QuoteStatus,
            'emailprovider': EmailProvider,
            'connectedaccountstatus': ConnectedAccountStatus,
            'documenttype': DocumentType
        }
        
        for enum_name, enum_class in enum_types.items():
            # Check if enum type exists
            result = conn.execute(
                text("SELECT 1 FROM pg_type WHERE typname = :name"),
                {"name": enum_name}
            ).fetchone()
            
            if not result:
                # Create enum type
                values = [e.value for e in enum_class]
                values_str = ', '.join(f"'{v}'" for v in values)
                conn.execute(text(f"CREATE TYPE {enum_name} AS ENUM ({values_str})"))
                print(f"Created ENUM type: {enum_name}")
        
        # Add any missing columns
        _add_missing_columns(conn, inspector)


def _add_missing_columns(conn, inspector):
    """Add columns that were added in later schema versions."""
    
    # quotes.quote_outcome
    if 'quotes' in inspector.get_table_names():
        quote_columns = [c['name'] for c in inspector.get_columns('quotes')]
        if 'quote_outcome' not in quote_columns:
            conn.execute(text("ALTER TABLE quotes ADD COLUMN quote_outcome VARCHAR(20)"))
            print("Added column: quotes.quote_outcome")
    
    # submissions.status_label
    if 'submissions' in inspector.get_table_names():
        sub_columns = [c['name'] for c in inspector.get_columns('submissions')]
        if 'status_label' not in sub_columns:
            conn.execute(text("ALTER TABLE submissions ADD COLUMN status_label VARCHAR(255)"))
            print("Added column: submissions.status_label")
    
    # brokers: letterhead, email_body, created_at, updated_at
    if 'brokers' in inspector.get_table_names():
        broker_columns = [c['name'] for c in inspector.get_columns('brokers')]
        if 'letterhead' not in broker_columns:
            conn.execute(text("ALTER TABLE brokers ADD COLUMN letterhead TEXT"))
            print("Added column: brokers.letterhead")
        if 'email_body' not in broker_columns:
            conn.execute(text("ALTER TABLE brokers ADD COLUMN email_body TEXT"))
            print("Added column: brokers.email_body")
        if 'created_at' not in broker_columns:
            conn.execute(text("ALTER TABLE brokers ADD COLUMN created_at TIMESTAMP DEFAULT NOW()"))
            print("Added column: brokers.created_at")
        if 'updated_at' not in broker_columns:
            conn.execute(text("ALTER TABLE brokers ADD COLUMN updated_at TIMESTAMP DEFAULT NOW()"))
            print("Added column: brokers.updated_at")
    
    # email_messages: is_deleted, connected_account_id
    if 'email_messages' in inspector.get_table_names():
        email_columns = [c['name'] for c in inspector.get_columns('email_messages')]
        if 'is_deleted' not in email_columns:
            conn.execute(text("ALTER TABLE email_messages ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE"))
            print("Added column: email_messages.is_deleted")
        if 'connected_account_id' not in email_columns:
            conn.execute(text("ALTER TABLE email_messages ADD COLUMN connected_account_id INTEGER"))
            print("Added column: email_messages.connected_account_id")


def get_current_db_name():
    """Get the name of the currently active database."""
    if not is_database_switching_enabled():
        return 'production'
    db_name = _current_db_name.get()
    return db_name if db_name in get_available_databases() else 'production'


def set_current_db(db_name):
    """
    Select a configured Postgres database for this request/context.
    Production environments are always pinned to production.
    
    Returns:
        bool: True if successful, False if database name is invalid
    """
    if not is_database_switching_enabled():
        return db_name == 'production'

    if db_name not in get_available_databases():
        return False

    _current_db_name.set(db_name)
    return True


def get_available_databases():
    """Get list of available database names."""
    if not is_database_switching_enabled():
        return ['production']
    return list(get_configured_databases().keys())


# Helper functions for common operations
def create_submission(insured_name, effective_date, state=None, user=None, assigned_to=None):
    """Create a new submission and log the action."""
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


def create_quote(submission_id, carrier_name, raw_document_path, extracted_json, user=None,
                 pass1_layout_json=None):
    """Create a new quote and log the action."""
    session = get_session()
    try:
        quote = Quote(
            submission_id=submission_id,
            carrier_name=carrier_name,
            raw_document_path=raw_document_path,
            extracted_json=extracted_json,
            pass1_layout_json=pass1_layout_json,
            status=QuoteStatus.RECEIVED
        )
        session.add(quote)
        session.flush()  # Get the ID
        quote_id = quote.id
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
        update_submission_appetite_score(submission_id)
        return quote_id
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


def log_action(entity_type, entity_id, action, user=None, details=None, submission_id=None, quote_id=None):
    """Log an action to the audit trail."""
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
    """Get a submission by ID with all its quotes."""
    from sqlalchemy.orm import joinedload
    session = get_session()
    try:
        submission = session.query(Submission).options(
            joinedload(Submission.quotes)
        ).filter_by(id=submission_id).first()
        if submission:
            result = submission.to_dict()
            result['quotes'] = [q.to_dict() for q in submission.quotes]
            return result
        return None
    finally:
        session.close()


def update_submission_appetite_score(submission_id):
    """Calculate and update the PF appetite score for a submission."""
    from app.appetite_scoring import calculate_appetite_score
    session = get_session()
    try:
        submission = session.query(Submission).filter_by(id=submission_id).first()
        if not submission:
            return
        submission_data = submission.to_dict()
        quotes_data = [q.to_dict() for q in submission.quotes]
        score_result = calculate_appetite_score(submission_data, quotes_data)
        submission.appetite_score = score_result['total_score']
        session.commit()
        print(f"Updated appetite score for submission {submission_id}: {score_result['total_score']}/100 ({score_result['rating']})")
    except Exception as e:
        session.rollback()
        print(f"Error updating appetite score: {e}")
    finally:
        session.close()
