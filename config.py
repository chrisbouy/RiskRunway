# config.py
import os
from pathlib import Path

class Config:
    # Flask settings
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-key-123'

    # File upload settings
    UPLOAD_FOLDER = os.path.join(Path(__file__).parent, 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

    # Database settings
    DATABASE_PATH = os.path.join(Path(__file__).parent, 'data', 'ipfs_mapper.db')

    # Create necessary folders if they don't exist
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(Path(__file__).parent, 'data'), exist_ok=True)