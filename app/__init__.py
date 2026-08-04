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

    # from app.sms_routes import sms_bp
    # app.register_blueprint(sms_bp)

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
            print(f"[EMAIL SCRAPER] Running background OAuth poll")
            
            oauth_result = None
            
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
                            
                            # Process emails: filter by broker OR insured name, then trigger SMS alerts
                            from app.models import Broker, Submission
                            
                            # Get broker emails for this user
                            user_brokers = db_session.query(Broker).filter(
                                Broker.user_id == account.user_id,
                                Broker.is_enabled == True,
                                Broker.email.isnot(None)
                            ).all()
                            broker_email_set = set(b.email.strip().lower() for b in user_brokers if b.email)
                            
                            # Get insured name variants for this user's submissions
                            user_submissions = db_session.query(Submission).filter(
                                Submission.assigned_to == account.user_id
                            ).all()
                            
                            insured_variants = set()
                            for sub in user_submissions:
                                if sub.insured_name:
                                    # Simple variant: lowercase full name and significant words
                                    name_lower = sub.insured_name.strip().lower()
                                    insured_variants.add(name_lower)
                                    for word in name_lower.split():
                                        if len(word) > 3:
                                            insured_variants.add(word)
                            
                            # Filter emails: from a broker OR mentions an insured name
                            own_email = (account.email_address or '').strip().lower()
                            matched_emails = []
                            for em in emails:
                                from_email = (em.from_email or '').strip().lower() if hasattr(em, 'from_email') else ''
                                
                                # Skip emails from self
                                if from_email == own_email:
                                    continue
                                
                                # Check if from a broker
                                is_from_broker = from_email in broker_email_set
                                
                                # Check if subject/body mentions an insured name
                                subject = (em.subject or '').lower() if hasattr(em, 'subject') else ''
                                body = (em.body_text or '').lower() if hasattr(em, 'body_text') else ''
                                combined = f"{subject} {body}"
                                mentions_insured = any(v in combined for v in insured_variants if len(v) > 3)
                                
                                if is_from_broker or mentions_insured:
                                    matched_emails.append(em)
                            
                            print(f"[EMAIL SCRAPER] {len(matched_emails)} emails matched (broker or insured name) out of {len(emails)}")
                            oauth_result = {'success': True, 'accounts': len(accounts), 'emails': len(emails), 'matched': len(matched_emails)}
                            
                            # Send SMS alerts for matched emails
                            if matched_emails and app.config.get('SMS_ALERTS_ENABLED', False):
                                try:
                                    from app.sms_client import create_sms_client, build_email_alert_text
                                    from app.models import User, SmsAlert
                                    
                                    sms = create_sms_client(app.config)
                                    if sms:
                                        user = db_session.query(User).filter_by(id=account.user_id).first()
                                        if user and user.phone_number and user.sms_alerts_enabled:
                                            # Dedup: check which provider_message_ids we've already alerted on
                                            already_alerted_msg_ids = set(
                                                row[0] for row in db_session.query(SmsAlert.provider_message_id).filter(
                                                    SmsAlert.user_id == user.id,
                                                    SmsAlert.provider_message_id.isnot(None)
                                                ).all()
                                            )
                                            
                                            alerts_sent = 0
                                            for em in matched_emails:
                                                from_name = em.from_name if hasattr(em, 'from_name') else ''
                                                from_email_addr = em.from_email if hasattr(em, 'from_email') else ''
                                                subject = em.subject if hasattr(em, 'subject') else ''
                                                att_count = len(em.attachments) if hasattr(em, 'attachments') else 0
                                                msg_id = em.message_id if hasattr(em, 'message_id') else None
                                                
                                                # Skip if we already alerted on this exact email
                                                if msg_id and msg_id in already_alerted_msg_ids:
                                                    continue
                                                
                                                # Find which submission this matches (if any)
                                                matched_sub = None
                                                subj_lower = (subject or '').lower()
                                                for sub in user_submissions:
                                                    if sub.insured_name and sub.insured_name.lower() in subj_lower:
                                                        matched_sub = sub
                                                        break
                                                
                                                # Build alert text
                                                parts = []
                                                if matched_sub:
                                                    parts.append(f"📬 {matched_sub.insured_name}")
                                                else:
                                                    parts.append("📬 New email")
                                                parts.append(f"From: {from_name or from_email_addr}")
                                                if subject:
                                                    subj_short = subject[:60] + "..." if len(subject) > 60 else subject
                                                    parts.append(f"Re: {subj_short}")
                                                if att_count > 0:
                                                    parts.append(f"({att_count} attachment{'s' if att_count > 1 else ''})")
                                                parts.append("")
                                                parts.append("Reply:")
                                                parts.append("1) Process")
                                                parts.append("2) Skip")
                                                alert_text = '\n'.join(parts)
                                                
                                                sms.send_alert(
                                                    user=user,
                                                    message=alert_text,
                                                    submission_id=matched_sub.id if matched_sub else None,
                                                    provider_message_id=msg_id,
                                                    connected_account_id=account.id
                                                )
                                                already_alerted_msg_ids.add(msg_id)
                                                alerts_sent += 1
                                            
                                            if alerts_sent > 0:
                                                print(f"[SMS ALERTS] Sent {alerts_sent} text alert(s) to {user.phone_number}")
                                except Exception as sms_err:
                                    print(f"[SMS ALERTS] Error in background alert: {sms_err}")
                            
                        except Exception as account_err:
                            print(f"[EMAIL SCRAPER] Error processing account {account.email_address}: {account_err}")
                    
                    db_session.close()
                else:
                    print("[EMAIL SCRAPER] No connected OAuth accounts found")
                    return
            except Exception as oauth_err:
                print(f"[EMAIL SCRAPER] OAuth scrape error: {oauth_err}")
                return
            
            # Log results
            from app.database import get_session, log_action
            from app.models import AuditLog
            db_session = get_session()
            try:
                result_summary = {
                    'oauth': oauth_result,
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
