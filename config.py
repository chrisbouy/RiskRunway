# config.py
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Flask settings
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-key-123'
    # Session cookie settings - required for OAuth cross-site redirects
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = False  # Set to True in production with HTTPS
    SESSION_COOKIE_HTTPONLY = True
    # File upload settings
    UPLOAD_FOLDER = os.path.join(Path(__file__).parent, 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

    # Database settings
    DATABASE_PATH = os.environ.get('DATABASE_PATH') or os.path.join(Path(__file__).parent, 'data', 'ipfs_mapper.db')
    SQLALCHEMY_EXPIRE_ON_COMMIT = False

    # Multiple database support
    DATABASES = {
        'production': os.environ.get('DATABASE_PATH') or os.path.join(Path(__file__).parent, 'data', 'ipfs_mapper.db'),
        'use_cases': os.environ.get('USE_CASE_DB_PATH') or os.path.join(Path(__file__).parent, 'data', 'use_cases.db'),
        'test': '/tmp/ipfs_mapper_test.db'
    }

    # Email settings (using SendGrid HTTP API for both bug reports and broker submissions)
    BUG_REPORT_RECIPIENT = os.environ.get('BUG_REPORT_RECIPIENT', 'chrisbouy@gmail.com')
    BUG_REPORT_SENDER = os.environ.get('BUG_REPORT_SENDER', 'chrisbouy@gmail.com')
    SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')

    # OAuth Email Integration Configuration
    # Gmail OAuth (configure via Google Cloud Console)
    GMAIL_CLIENT_ID = os.environ.get('GMAIL_CLIENT_ID', '')
    GMAIL_CLIENT_SECRET = os.environ.get('GMAIL_CLIENT_SECRET', '')
    GMAIL_REDIRECT_URI = os.environ.get('GMAIL_REDIRECT_URI', 'http://localhost:5000/oauth/gmail/callback')
    
    # Microsoft Outlook OAuth (configure via Azure Portal)
    MICROSOFT_CLIENT_ID = os.environ.get('MICROSOFT_CLIENT_ID', '')
    MICROSOFT_CLIENT_SECRET = os.environ.get('MICROSOFT_CLIENT_SECRET', '')
    MICROSOFT_REDIRECT_URI = os.environ.get('MICROSOFT_REDIRECT_URI', 'http://localhost:5000/oauth/outlook/callback')
    MICROSOFT_TENANT_ID = os.environ.get('MICROSOFT_TENANT_ID', 'common')
    
    # IMAP Email Scraping Configuration (active)
    IMAP_SERVER = os.environ.get('IMAP_SERVER', 'imap.gmail.com')
    IMAP_EMAIL = os.environ.get('IMAP_EMAIL', '')
    IMAP_PASSWORD = os.environ.get('IMAP_PASSWORD', '')
    IMAP_USE_SSL = os.environ.get('IMAP_USE_SSL', 'true').lower() == 'true'
    
    # Email Processing
    EMAIL_SCRAPING_ENABLED = os.environ.get('EMAIL_SCRAPING_ENABLED', 'false').lower() == 'true'
    EMAIL_SCRAPING_MODE = os.environ.get('EMAIL_SCRAPING_MODE', 'oauth').lower()  # oauth | imap | auto
    # 'oauth' - use OAuth (Gmail/Outlook) only
    # 'imap' - use IMAP only
    # 'auto' - try OAuth first, fall back to IMAP if OAuth not configured
    EMAIL_SCRAPE_INTERVAL_MINUTES = int(os.environ.get('EMAIL_SCRAPE_INTERVAL_MINUTES', '5'))
    
    # Token encryption key (generate a new one for production)
    TOKEN_ENCRYPTION_KEY = os.environ.get('TOKEN_ENCRYPTION_KEY', '')
    if not TOKEN_ENCRYPTION_KEY:
        import secrets
        TOKEN_ENCRYPTION_KEY = secrets.token_hex(32)

    # Document storage settings
    STORAGE_PROVIDER = os.environ.get('STORAGE_PROVIDER', 'local')  # local | s3
    S3_BUCKET = os.environ.get('S3_BUCKET', '')
    S3_REGION = os.environ.get('S3_REGION', 'us-east-1')
    S3_ENDPOINT_URL = os.environ.get('S3_ENDPOINT_URL', '')
    DOCUMENTS_LOCAL_FOLDER = os.path.join(UPLOAD_FOLDER, 'documents')

    # Premium Finance Appetite Scoring Rules
    # Score range: 0-100 (higher = better appetite)
    PF_APPETITE_RULES = {
        # Premium size scoring (40 points max)
        'premium_size': {
            'ranges': [
                {'min': 0, 'max': 5000, 'score': 10, 'label': 'Too Small'},
                {'min': 5000, 'max': 25000, 'score': 25, 'label': 'Small'},
                {'min': 25000, 'max': 100000, 'score': 40, 'label': 'Sweet Spot'},
                {'min': 100000, 'max': 500000, 'score': 35, 'label': 'Large'},
                {'min': 500000, 'max': float('inf'), 'score': 20, 'label': 'Very Large'},
            ]
        },
        # Down payment percentage scoring (30 points max)
        'down_payment_pct': {
            'ranges': [
                {'min': 0, 'max': 10, 'score': 5, 'label': 'Low Down'},
                {'min': 10, 'max': 20, 'score': 20, 'label': 'Standard'},
                {'min': 20, 'max': 30, 'score': 30, 'label': 'Good'},
                {'min': 30, 'max': 100, 'score': 25, 'label': 'High Down'},
            ]
        },
        # State risk scoring (30 points max)
        # Based on typical PF risk by state
        'state_risk': {
            'low_risk': {'score': 30, 'states': ['CA', 'NY', 'TX', 'FL', 'IL', 'PA', 'OH']},
            'medium_risk': {'score': 20, 'states': ['NJ', 'GA', 'NC', 'VA', 'WA', 'MA', 'AZ', 'TN', 'IN', 'MO']},
            'high_risk': {'score': 10, 'states': ['LA', 'MS', 'AL', 'WV', 'AR', 'NM']},
            'default': {'score': 15}  # Unknown or other states
        }
    }

    # Create necessary folders if they don't exist
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(DOCUMENTS_LOCAL_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(Path(__file__).parent, 'data'), exist_ok=True)
