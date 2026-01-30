# app/__init__.py
from flask import Flask
from config import Config

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Initialize database
    from app.database import init_db
    init_db()

    # Register blueprints
    from app import routes
    app.register_blueprint(routes.bp)

    return app