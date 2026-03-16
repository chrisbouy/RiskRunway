# app/__init__.py
from flask import Flask, jsonify
from flask_cors import CORS
from config import Config
import traceback
from datetime import datetime, timedelta
import atexit
from apscheduler.schedulers.background import BackgroundScheduler

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

    # Initialize email scraping scheduler if enabled
    def scrape_emails_task():
        """Background task to scrape emails"""
        if app.config.get('EMAIL_SCRAPING_ENABLED', False) and app.config.get('IMAP_PASSWORD'):
            try:
                from app.email_scraper import EmailScraper
                from datetime import timedelta, datetime
                import json
                
                scraper = EmailScraper(
                    imap_server=app.config['IMAP_SERVER'],
                    email_address=app.config['IMAP_EMAIL'],
                    password=app.config['IMAP_PASSWORD'],
                    use_ssl=app.config['IMAP_USE_SSL']
                )
                
                # Scrape emails from last 24 hours
                since_date = datetime.now() - timedelta(hours=24)
                result = scraper.scrape_emails(since_date)
                
                print(f"[EMAIL SCRAPER] Background scrape completed: {result}")
                
                # Log to database
                from app.database import get_session, log_action
                from app.models import AuditLog
                db_session = get_session()
                try:
                    log_action(
                        entity_type='system',
                        entity_id=0,
                        action='email_scrape_background',
                        details=json.dumps(result)
                    )
                    db_session.commit()
                except Exception as log_error:
                    print(f"[EMAIL SCRAPER] Failed to log action: {log_error}")
                finally:
                    db_session.close()
                    
            except Exception as e:
                print(f"[EMAIL SCRAPER] Background scrape error: {str(e)}")
        else:
            print("[EMAIL SCRAPER] Email scraping disabled or not configured")

    # Start scheduler if email scraping is enabled
    if app.config.get('EMAIL_SCRAPING_ENABLED', False):
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            func=scrape_emails_task,
            trigger='interval',
            minutes=app.config.get('EMAIL_SCRAPE_INTERVAL_MINUTES', 5),
            id='email_scraper'
        )
        scheduler.start()
        
        # Register shutdown hook
        atexit.register(lambda: scheduler.shutdown())
        
        print(f"[EMAIL SCRAPER] Scheduler started - runs every {app.config.get('EMAIL_SCRAPE_INTERVAL_MINUTES', 5)} minutes")
        
        # Run initial scrape after 30 seconds
        scheduler.add_job(
            func=scrape_emails_task,
            trigger='date',
            run_date=datetime.now() + timedelta(seconds=30),
            id='email_scraper_initial'
        )

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