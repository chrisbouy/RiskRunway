# app/__init__.py
from flask import Flask
from flask_cors import CORS
from config import Config

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    
    # Enable CORS for all routes (needed for Chrome extension)
    CORS(app)

    # Initialize database
    from app.database import init_db
    init_db()

    # Register blueprints
    from app import routes
    app.register_blueprint(routes.bp)

    return app