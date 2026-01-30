# app/models.py
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
import enum

Base = declarative_base()


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
    
    # Relationships
    quotes = relationship("Quote", back_populates="submission", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="submission", cascade="all, delete-orphan")

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
            'quote_count': len(self.quotes) if self.quotes else 0
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

