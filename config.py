# config.py
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Flask settings
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-key-123'

    # File upload settings
    UPLOAD_FOLDER = os.path.join(Path(__file__).parent, 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

    # Database settings
    DATABASE_PATH = os.path.join(Path(__file__).parent, 'data', 'ipfs_mapper.db')

    # Bug report email settings
    BUG_REPORT_RECIPIENT = os.environ.get('BUG_REPORT_RECIPIENT', 'chrisbouy@gmail.com')
    BUG_REPORT_SMTP_HOST = os.environ.get('BUG_REPORT_SMTP_HOST') 
    BUG_REPORT_SMTP_PORT = int(os.environ.get('BUG_REPORT_SMTP_PORT') or 587)
    BUG_REPORT_SMTP_USER = os.environ.get('BUG_REPORT_SMTP_USER') 
    BUG_REPORT_SMTP_PASSWORD = os.environ.get('BUG_REPORT_SMTP_PASSWORD') or os.environ.get('EMAIL_HOST_PASSWORD', '')
    BUG_REPORT_SMTP_USE_TLS = os.environ.get('BUG_REPORT_SMTP_USE_TLS', 'true').lower() == 'true'
    BUG_REPORT_SMTP_TIMEOUT = int(os.environ.get('BUG_REPORT_SMTP_TIMEOUT', 15))

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
    os.makedirs(os.path.join(Path(__file__).parent, 'data'), exist_ok=True)
