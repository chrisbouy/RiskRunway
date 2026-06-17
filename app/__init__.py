# app/__init__.py
from flask import Flask, jsonify, request
from flask_cors import CORS
from config import Config
import traceback
from datetime import datetime, timedelta
import atexit
import json
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.exceptions import HTTPException

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Enable CORS for all routes (needed for Chrome extension)
    CORS(app)

    # Initialize database
    from app.database import init_db, set_tenant_for_request
    init_db()

    # Register blueprints
    from app import routes
    app.register_blueprint(routes.bp)

    from app.epic_routes import epic_bp
    app.register_blueprint(epic_bp)

    # Multi-tenant: resolve tenant from hostname on every request
    @app.before_request
    def resolve_tenant():
        tenant = set_tenant_for_request(request.host)
        print(f"[TENANT] host={request.host} tenant={tenant}")

    # Initialize email scraping scheduler if enabled
    def scrape_emails_task():
        """Background task to scrape emails"""
        # Run within Flask application context since this task runs in a background thread
        with app.app_context():
            scraping_mode = app.config.get('EMAIL_SCRAPING_MODE', 'oauth').lower()
            print(f"[EMAIL SCRAPER] Running in mode: {scraping_mode}")
            
            # Track results from both methods
            oauth_result = None
            imap_result = None
            
            # === OAuth MODE (Gmail/Outlook) ===
            if scraping_mode in ('oauth', 'auto'):
                try:
                    from app.database import get_session
                    from app.models import ConnectedAccount, ConnectedAccountStatus, EmailProvider
                    from app.oauth_services import get_oauth_service
                    
                    db_session = get_session()
                    
                    # Get all active connected accounts
                    accounts = db_session.query(ConnectedAccount).filter(
                        ConnectedAccount.status == ConnectedAccountStatus.ACTIVE
                    ).all()
                    
                    if accounts:
                        for account in accounts:
                            try:
                                # Get OAuth service for this provider
                                config = {
                                    'GMAIL_CLIENT_ID': app.config.get('GMAIL_CLIENT_ID', ''),
                                    'GMAIL_CLIENT_SECRET': app.config.get('GMAIL_CLIENT_SECRET', ''),
                                    'GMAIL_REDIRECT_URI': app.config.get('GMAIL_REDIRECT_URI', ''),
                                    'MICROSOFT_CLIENT_ID': app.config.get('MICROSOFT_CLIENT_ID', ''),
                                    'MICROSOFT_CLIENT_SECRET': app.config.get('MICROSOFT_CLIENT_SECRET', ''),
                                    'MICROSOFT_REDIRECT_URI': app.config.get('MICROSOFT_REDIRECT_URI', ''),
                                }
                                
                                provider_name = 'gmail' if account.provider == EmailProvider.GMAIL else 'outlook'
                                service = get_oauth_service(provider_name, config)
                                
                                # Get decrypted tokens (NOW WITHIN APP CONTEXT!)
                                tokens = account.get_decrypted_tokens()
                                access_token = tokens.get('access_token')
                                
                                if not access_token:
                                    print(f"[EMAIL SCRAPER] No access token for {account.email_address}, skipping")
                                    continue
                                
                                # Refresh token if needed
                                refresh_token = tokens.get('refresh_token')
                                if refresh_token:
                                    try:
                                        new_tokens = service.refresh_access_token(refresh_token)
                                        account.set_encrypted_tokens(new_tokens)
                                        access_token = new_tokens.get('access_token')
                                        db_session.commit()
                                    except Exception as refresh_err:
                                        print(f"[EMAIL SCRAPER] Token refresh failed for {account.email_address}: {refresh_err}")
                                
                                # Fetch emails from this account
                                from datetime import timedelta
                                since_date = datetime.now() - timedelta(hours=24)
                                emails = service.fetch_emails(access_token, max_results=50, since_date=since_date)
                                
                                print(f"[EMAIL SCRAPER] OAuth fetched {len(emails)} emails from {account.email_address}")
                                
                                # Process emails (match to submissions, save to DB)
                                # For now just count - full integration would reuse email_client.py logic
                                oauth_result = {'success': True, 'accounts': len(accounts), 'emails': len(emails)}
                                
                            except Exception as account_err:
                                print(f"[EMAIL SCRAPER] Error processing account {account.email_address}: {account_err}")
                        
                        db_session.close()
                    else:
                        print("[EMAIL SCRAPER] No connected OAuth accounts found")
                        if scraping_mode == 'oauth':
                            return  # OAuth mode but no accounts
                        # auto mode will fall through to IMAP
                except Exception as oauth_err:
                    print(f"[EMAIL SCRAPER] OAuth scrape error: {oauth_err}")
                    if scraping_mode == 'oauth':
                        return  # OAuth mode - don't try IMAP
            
            # === IMAP MODE (fallback or direct) ===
            if scraping_mode in ('imap', 'auto') and app.config.get('IMAP_PASSWORD'):
                try:
                    from app.email_scraper import EmailScraper
                    
                    scraper = EmailScraper(
                        imap_server=app.config['IMAP_SERVER'],
                        email_address=app.config['IMAP_EMAIL'],
                        password=app.config['IMAP_PASSWORD'],
                        use_ssl=app.config['IMAP_USE_SSL']
                    )
                    
                    # Scrape emails from last 24 hours
                    since_date = datetime.now() - timedelta(hours=24)
                    imap_result = scraper.scrape_emails(since_date)
                    
                    print(f"[EMAIL SCRAPER] IMAP scrape completed: {imap_result}")
                    
                except Exception as imap_err:
                    print(f"[EMAIL SCRAPER] IMAP scrape error: {imap_err}")
                    imap_result = {'success': False, 'error': str(imap_err)}
            elif scraping_mode in ('imap', 'auto') and not app.config.get('IMAP_PASSWORD'):
                print("[EMAIL SCRAPER] IMAP configured but no password set")
            
            # Log results
            from app.database import get_session, log_action
            from app.models import AuditLog
            db_session = get_session()
            try:
                result_summary = {
                    'mode': scraping_mode,
                    'oauth': oauth_result,
                    'imap': imap_result
                }
                log_action(
                    entity_type='system',
                    entity_id=0,
                    action='email_scrape_background',
                    details=json.dumps(result_summary)
                )
                db_session.commit()
            except Exception as log_error:
                print(f"[EMAIL SCRAPER] Failed to log action: {log_error}")
            finally:
                db_session.close()

    # Start scheduler if email polling is enabled
    if app.config.get('EMAIL_POLLING_ENABLED', False):
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
        
        print(f"[EMAIL SCRAPER] Polling scheduler started - runs every {app.config.get('EMAIL_SCRAPE_INTERVAL_MINUTES', 5)} minutes")

    @app.errorhandler(HTTPException)
    def handle_http_exception(e):
        """Let normal HTTP errors behave normally, without noisy tracebacks."""
        if request.path.startswith('/api/') or request.accept_mimetypes.best == 'application/json':
            return jsonify({
                'success': False,
                'error': e.description
            }), e.code

        return e

    # Global error handler to return JSON instead of HTML
    @app.errorhandler(Exception)
    def handle_exception(e):
        if isinstance(e, HTTPException):
            return handle_http_exception(e)

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
