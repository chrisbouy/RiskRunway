# app/__init__.py
from flask import Flask, jsonify
from flask_cors import CORS
from config import Config
import traceback

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

    # Global error handler to return JSON instead of HTML
    @app.errorhandler(Exception)
    def handle_exception(e):
        # Log the full traceback
        print(f"[FLASK ERROR] Unhandled exception: {type(e).__name__}: {str(e)}")
        traceback.print_exc()

        # Return JSON error instead of HTML
        return jsonify({
            'success': False,
            'error': f'{type(e).__name__}: {str(e)}',
            'traceback': traceback.format_exc()
        }), 500

    return app