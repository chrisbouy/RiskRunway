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


class DocumentType(enum.Enum):
    APPLICATION = "Application"
    SOV = "SOV"
    LOSS_RUN = "Loss Run"
    QUOTE = "Quote"
    BINDER = "Binder"
    FINANCE_AGREEMENT = "Finance Agreement"
    OTHER = "Other"


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
    brokers = relationship("Broker", back_populates="user", cascade="all, delete-orphan")

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
    status_label = Column(String(255), nullable=True)
    appetite_score = Column(Integer, nullable=True)  # PF appetite score 0-100
    assigned_to = Column(Integer, ForeignKey('users.id'), nullable=True)  # User assignment

    # Relationships
    quotes = relationship("Quote", back_populates="submission", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="submission", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="submission", cascade="all, delete-orphan")
    assigned_user = relationship("User", back_populates="assigned_submissions")

    def __repr__(self):
        return f"<Submission(id={self.id}, insured='{self.insured_name}', effective_date='{self.effective_date}')>"

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        # Determine api_status for frontend compatibility
        status_name = self.status.name if self.status else None
        api_status_map = {
            'RECEIVED': 'submission',
            'IN_PROGRESS': 'in_progress',
            'CHOSEN': 'chosen',
            'SENT_TO_FINANCE': 'bound'
        }
        api_status = api_status_map.get(status_name, 'submission')
        
        return {
            'id': self.id,
            'insured_name': self.insured_name,
            'effective_date': self.effective_date,
            'state': self.state,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'status': self.status.value if self.status else None,
            'api_status': api_status,
            'status_label': self.status_label,
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
    extracted_json = Column(Text, nullable=True)  # Full JSON extraction result (Pass 2 normalized data)

    # Three-pass processing data
    pass1_layout_json = Column(Text, nullable=True)  # Pass 1: OCR and layout extraction
    # pass3_intent_json = Column(Text, nullable=True)  # Pass 3: Quote intent classification
    # quote_intent = Column(String(50), nullable=True)  # Quick access: new_coverage, competing_quote, renewal, etc.
    # comparison_group = Column(String(100), nullable=True)  # Quick access: GL, WC, Auto, etc.

    status = Column(Enum(QuoteStatus), default=QuoteStatus.RECEIVED, nullable=False)
    quote_outcome = Column(String(20), nullable=True)  # WON or LOST (set when moving to bind)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    submission = relationship("Submission", back_populates="quotes")
    documents = relationship("Document", back_populates="quote", cascade="all, delete-orphan")
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
            'pass1_layout_json': self.pass1_layout_json,
            # 'pass3_intent_json': self.pass3_intent_json,
            # 'quote_intent': self.quote_intent,
            # 'comparison_group': self.comparison_group,
            'status': self.status.value if self.status else None,
            'quote_outcome': self.quote_outcome,
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


class Document(Base):
    """
    Generic document metadata linked to a submission (and optionally a quote).
    Supports versioning and active/inactive binder state by term.
    """
    __tablename__ = 'documents'

    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey('submissions.id'), nullable=False, index=True)
    quote_id = Column(Integer, ForeignKey('quotes.id'), nullable=True, index=True)
    document_type = Column(Enum(DocumentType), nullable=False, index=True)
    carrier = Column(String(255), nullable=True, index=True)
    term_key = Column(String(50), nullable=True, index=True)  # e.g. 2026-02-10_2027-02-10
    version = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True)
    storage_provider = Column(String(20), nullable=False, default='local')  # local|s3
    storage_key = Column(String(1024), nullable=False)  # object key/path
    original_filename = Column(String(500), nullable=False)
    content_type = Column(String(100), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    uploaded_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    submission = relationship("Submission", back_populates="documents")
    quote = relationship("Quote", back_populates="documents")

    def to_dict(self):
        return {
            'id': self.id,
            'submission_id': self.submission_id,
            'quote_id': self.quote_id,
            'document_type': self.document_type.value if self.document_type else None,
            'carrier': self.carrier,
            'term_key': self.term_key,
            'version': self.version,
            'is_active': self.is_active,
            'storage_provider': self.storage_provider,
            'storage_key': self.storage_key,
            'original_filename': self.original_filename,
            'content_type': self.content_type,
            'size_bytes': self.size_bytes,
            'uploaded_by': self.uploaded_by,
            'created_at': self.created_at.isoformat() if self.created_at else None
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


class Broker(Base):
    """
    Represents a broker that an agent can send submissions to.
    Each user can configure their own list of brokers.
    Brokers can be email-based or portal-based.
    """
    __tablename__ = 'brokers'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    name = Column(String(255), nullable=True)  # Optional broker name
    email = Column(String(255), nullable=True)  # Email address (for email brokers)
    portal_name = Column(String(255), nullable=True)  # Portal site name (for portal brokers)
    is_portal = Column(Boolean, default=False, nullable=False)  # True if portal-based, False if email-based
    is_enabled = Column(Boolean, default=True, nullable=False)  # Whether this broker is active
    letterhead = Column(Text, nullable=True)  # Custom letterhead/signature for emails
    email_body = Column(Text, nullable=True)  # Custom email body template
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    user = relationship("User", back_populates="brokers")

    def __repr__(self):
        broker_type = "Portal" if self.is_portal else "Email"
        return f"<Broker(id={self.id}, user_id={self.user_id}, name='{self.name}', type='{broker_type}')>"

    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'name': self.name,
            'email': self.email,
            'portal_name': self.portal_name,
            'is_portal': self.is_portal,
            'is_enabled': self.is_enabled,
            'letterhead': self.letterhead,
            'email_body': self.email_body,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class EmailMessage(Base):
    """
    Represents an email message scraped from IMAP that matches a submission.
    """
    __tablename__ = 'email_messages'

    id = Column(Integer, primary_key=True)
    submission_id = Column(Integer, ForeignKey('submissions.id'), nullable=True, index=True)
    message_id = Column(String(500), unique=True, nullable=False, index=True)  # Email Message-ID header
    from_email = Column(String(255), nullable=False)
    from_name = Column(String(255), nullable=True)
    to_email = Column(String(255), nullable=True)
    subject = Column(String(1000), nullable=True)
    body_text = Column(Text, nullable=True)
    body_html = Column(Text, nullable=True)
    received_date = Column(DateTime, nullable=False, index=True)
    has_attachments = Column(Boolean, default=False, nullable=False)
    attachment_count = Column(Integer, default=0, nullable=False)
    is_read = Column(Boolean, default=False, nullable=False, index=True)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)  # Mark as deleted so won't reappear on scrape
    matched_insured_name = Column(Boolean, default=False, nullable=False)
    matched_quote_attachment = Column(Boolean, default=False, nullable=False)
    matched_keywords = Column(String(500), nullable=True)  # Comma-separated keywords that matched
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    submission = relationship('Submission', backref='emails')
    attachments = relationship('EmailAttachment', back_populates='email', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'submission_id': self.submission_id,
            'message_id': self.message_id,
            'from_email': self.from_email,
            'from_name': self.from_name,
            'to_email': self.to_email,
            'subject': self.subject,
            'body_text': self.body_text,
            'body_html': self.body_html,
            'received_date': self.received_date.isoformat() if self.received_date else None,
            'has_attachments': self.has_attachments,
            'attachment_count': self.attachment_count,
            'is_read': self.is_read,
            'is_deleted': self.is_deleted,
            'matched_insured_name': self.matched_insured_name,
            'matched_quote_attachment': self.matched_quote_attachment,
            'matched_keywords': self.matched_keywords,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'attachments': [att.to_dict() for att in self.attachments] if self.attachments else []
        }


class EmailAttachment(Base):
    """
    Represents an attachment from an email message.
    """
    __tablename__ = 'email_attachments'

    id = Column(Integer, primary_key=True)
    email_id = Column(Integer, ForeignKey('email_messages.id'), nullable=False, index=True)
    filename = Column(String(500), nullable=False)
    content_type = Column(String(100), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    file_path = Column(String(1000), nullable=True)  # Path to saved file on disk
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    email = relationship('EmailMessage', back_populates='attachments')

    def to_dict(self):
        return {
            'id': self.id,
            'email_id': self.email_id,
            'filename': self.filename,
            'content_type': self.content_type,
            'size_bytes': self.size_bytes,
            'file_path': self.file_path,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
