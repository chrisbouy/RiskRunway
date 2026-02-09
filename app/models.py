# app/models.py
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, Enum, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from werkzeug.security import generate_password_hash, check_password_hash
import enum

Base = declarative_base()


class UserRole(enum.Enum):
    ADMIN = "Admin"
    VIEWER = "Viewer"


class SubmissionStatus(enum.Enum):
    RECEIVED = "Received"
    IN_PROGRESS = "In Progress"
    CHOSEN = "Chosen"
    SENT_TO_FINANCE = "Sent to Finance"


class QuoteStatus(enum.Enum):
    RECEIVED = "Received"
    REVIEWED = "Reviewed"
    COMPARED = "Compared"
    CHOSEN = "Chosen"


class User(Base):
    """
    Represents a user with authentication and role-based access.
    """
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Relationships
    assigned_submissions = relationship("Submission", back_populates="assigned_user")

    def set_password(self, password):
        """Hash and set the user's password"""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Check if the provided password matches the hash"""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User(id={self.id}, username='{self.username}', role='{self.role.value}')>"

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'username': self.username,
            'full_name': self.full_name,
            'role': self.role.value,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Submission(Base):
    """
    Represents an insurance placement attempt for a specific insured.
    This is the anchor - everything hangs off this.
    """
    __tablename__ = 'submissions'

    id = Column(Integer, primary_key=True)
    insured_name = Column(String(255), nullable=False, index=True)
    effective_date = Column(String(10), nullable=False)  # YYYY-MM-DD format
    state = Column(String(2), nullable=True)  # Two-letter state code
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(Enum(SubmissionStatus), default=SubmissionStatus.RECEIVED, nullable=False)
    appetite_score = Column(Integer, nullable=True)  # PF appetite score 0-100
    assigned_to = Column(Integer, ForeignKey('users.id'), nullable=True)  # User assignment

    # Relationships
    quotes = relationship("Quote", back_populates="submission", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="submission", cascade="all, delete-orphan")
    assigned_user = relationship("User", back_populates="assigned_submissions")

    def __repr__(self):
        return f"<Submission(id={self.id}, insured='{self.insured_name}', effective_date='{self.effective_date}')>"

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'insured_name': self.insured_name,
            'effective_date': self.effective_date,
            'state': self.state,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'status': self.status.value if self.status else None,
            'quote_count': len(self.quotes) if self.quotes else 0,
            'appetite_score': self.appetite_score,
            'assigned_to': self.assigned_to,
            'assigned_user': self.assigned_user.to_dict() if self.assigned_user else None
        }


class Quote(Base):
    """
    Represents one carrier's response to a submission.
    Each PDF dropped creates one quote record.
    """
    __tablename__ = 'quotes'

    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey('submissions.id'), nullable=False, index=True)
    carrier_name = Column(String(255), nullable=True)  # Extracted from document
    raw_document_path = Column(String(500), nullable=False)  # Path to uploaded PDF
    extracted_json = Column(Text, nullable=True)  # Full JSON extraction result
    status = Column(Enum(QuoteStatus), default=QuoteStatus.RECEIVED, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    submission = relationship("Submission", back_populates="quotes")
    audit_logs = relationship("AuditLog", back_populates="quote", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Quote(id={self.id}, submission_id={self.submission_id}, carrier='{self.carrier_name}')>"

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'submission_id': self.submission_id,
            'carrier_name': self.carrier_name,
            'raw_document_path': self.raw_document_path,
            'extracted_json': self.extracted_json,
            'status': self.status.value if self.status else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class AuditLog(Base):
    """
    Tracks all actions on submissions and quotes.
    Provides enterprise-level audit trail.
    """
    __tablename__ = 'audit_logs'

    id = Column(Integer, primary_key=True)
    entity_type = Column(String(50), nullable=False)  # 'submission' or 'quote'
    entity_id = Column(Integer, nullable=False, index=True)
    action = Column(String(100), nullable=False)  # 'uploaded', 'parsed', 'chosen', 'exported', etc.
    user = Column(String(100), nullable=True)  # Username (can be added later)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    details = Column(Text, nullable=True)  # Optional JSON details
    
    # Optional foreign keys for easier querying
    submission_id = Column(Integer, ForeignKey('submissions.id'), nullable=True, index=True)
    quote_id = Column(Integer, ForeignKey('quotes.id'), nullable=True, index=True)
    
    # Relationships
    submission = relationship("Submission", back_populates="audit_logs")
    quote = relationship("Quote", back_populates="audit_logs")

    def __repr__(self):
        return f"<AuditLog(id={self.id}, entity_type='{self.entity_type}', action='{self.action}')>"

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'entity_type': self.entity_type,
            'entity_id': self.entity_id,
            'action': self.action,
            'user': self.user,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'details': self.details
        }

class AppetiteRule(Base):
    """
    Stores configurable PF appetite scoring rules.
    Allows dynamic adjustment of scoring criteria without code changes.
    """
    __tablename__ = 'appetite_rules'

    id = Column(Integer, primary_key=True)
    rule_type = Column(String(50), nullable=False, unique=True)  # 'premium_size', 'down_payment_pct', 'state_risk'
    rule_data = Column(Text, nullable=False)  # JSON-encoded rule configuration
    max_score = Column(Integer, nullable=False)  # Maximum points for this rule
    enabled = Column(Boolean, default=True, nullable=False)  # Whether this rule is active
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<AppetiteRule(id={self.id}, rule_type='{self.rule_type}', max_score={self.max_score}, enabled={self.enabled})>"

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'rule_type': self.rule_type,
            'rule_data': self.rule_data,
            'max_score': self.max_score,
            'enabled': self.enabled,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


