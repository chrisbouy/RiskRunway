# app/routes.py
from flask import Blueprint, render_template, request, jsonify, current_app, session, redirect, url_for, send_file
import os
import json
import re
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, List
import requests
import base64
import uuid
import shutil
from functools import wraps
from werkzeug.utils import secure_filename
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.parsers.two_pass_parser import process_quote_two_pass
from app.parsers.application_parser import process_application_two_pass
from app.database import (
    get_all_submissions,
    get_submission_by_id,
    create_submission,
    create_quote,
    update_submission_appetite_score,
    log_action,
    get_session,
    get_current_db_name,
    set_current_db,
    get_available_databases,
    is_database_switching_enabled
)
from app.models import Submission, Quote, SubmissionStatus, QuoteStatus, User, UserRole, AuditLog, Document, DocumentType, Broker, EmailMessage, EmailAttachment, ConnectedAccount, EmailProvider, ConnectedAccountStatus, AmsExportJob, AppetiteRule, SmsAlert
from app.email_client import EmailClient, create_email_client
from app.oauth_services import get_oauth_service

logger = logging.getLogger(__name__)

bp = Blueprint('main', __name__)


@bp.before_app_request
def select_database_for_request():
    """Apply the session's selected database before route handlers run.
    In production, tenant routing (hostname-based) is handled by app/__init__.py.
    This only applies to local dev database switching."""
    if not is_database_switching_enabled():
        session.pop('current_database', None)
        set_current_db('production')
        return

    db_name = session.get('current_database', 'development')
    if not set_current_db(db_name):
        session['current_database'] = 'development'
        set_current_db('development')

# Health check endpoint for load balancers
@bp.route('/health', methods=['GET'])
def health():
    """Health check endpoint for ALB / Kubernetes probes"""
    try:
        from app.database import get_db
        db = get_db()
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return jsonify({'status': 'healthy', 'database': 'connected'}), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500


# ============================================================================
# PUBLIC PAGES (no login required)
# ============================================================================

@bp.route('/privacy', methods=['GET'])
def privacy_policy():
    """Main application privacy policy"""
    return render_template('privacy.html')


@bp.route('/sms-consent', methods=['GET'])
def sms_consent():
    """Public SMS opt-in page for Twilio verification reviewers"""
    return render_template('sms_consent.html')


@bp.route('/extension/privacy', methods=['GET'])
def extension_privacy_policy():
    """Chrome extension privacy policy"""
    return render_template('extension_privacy.html')


# Server-side OAuth flow cache — avoids Flask cookie 4KB size limit
# The MSAL flow object (with PKCE verifier) is too large for cookie-based sessions
import time
_oauth_flow_cache = {}

def _store_flow(state: str, flow: dict, user_id: int = None):
    """Store MSAL flow object server-side, keyed by state. Also store user_id."""
    _oauth_flow_cache[state] = {'flow': flow, 'user_id': user_id, 'ts': time.time()}

def _get_flow(state: str) -> tuple:
    """Retrieve and remove MSAL flow object. Returns (flow, user_id) or (None, None) if missing or expired."""
    entry = _oauth_flow_cache.pop(state, None)
    if entry and time.time() - entry['ts'] < 300:  # 5 minute expiry
        return entry['flow'], entry.get('user_id')
    return None, None


def _storage_upload(local_path, object_key, content_type=None):
    """
    Upload file to configured object storage.
    Falls back to local file storage if S3 is unavailable or not configured.
    """
    provider = (current_app.config.get('STORAGE_PROVIDER') or 'local').lower()
    if provider == 's3':
        bucket = current_app.config.get('S3_BUCKET')
        if bucket:
            try:
                import boto3
                extra_args = {}
                if content_type:
                    extra_args['ContentType'] = content_type
                client = boto3.client(
                    's3',
                    region_name=current_app.config.get('S3_REGION') or None,
                    endpoint_url=current_app.config.get('S3_ENDPOINT_URL') or None
                )
                client.upload_file(local_path, bucket, object_key, ExtraArgs=extra_args or None)
                return 's3', object_key
            except Exception as err:
                print(f"[DOC STORAGE] S3 upload failed, falling back to local: {err}")

    local_root = current_app.config.get('UPLOAD_FOLDER')
    final_path = os.path.join(local_root, object_key)
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    shutil.copy2(local_path, final_path)
    return 'local', object_key


def _build_storage_key(submission_id, document_type, filename, user_id=None, insured_name=None):
    safe_name = secure_filename(filename) or 'document.bin'
    provider = (current_app.config.get('STORAGE_PROVIDER') or 'local').lower()
    
    # S3: tenant/user_id/insured_name/...
    # Local: insured_name/...
    if provider == 's3':
        from app.database import get_current_tenant
        tenant = get_current_tenant()
        base = f"{tenant}/" if tenant and tenant != 'default' else ""
        if user_id:
            base = f"{base}{user_id}/"
    else:
        base = ""
    
    safe_insured = secure_filename(insured_name or 'unknown_insured').replace(' ', '_')
    # base = f"{base}/{safe_insured}" if base else safe_insured
    
    print(f"base: {base}, safe_insured: {safe_insured}, safe_name: {safe_name}")
    return f"{base}{safe_insured}/{document_type}_{safe_name}"

def _document_download_url(document_id):
    return url_for('main.download_document', document_id=document_id)


def _send_bug_report_email(subject, body_text, screenshot_bytes, screenshot_filename, screenshot_subtype='png'):
    """Send bug report email with screenshot attachment using Resend API."""
    import base64
    import requests as http_requests

    api_key = current_app.config.get('RESEND_API_KEY')
    from_email = current_app.config.get('RESEND_FROM_EMAIL', 'noreply@risk-runway.com')
    recipient = current_app.config.get('BUG_REPORT_RECIPIENT', 'chrisbouy@gmail.com')

    # Debug logging
    print(f"[BUG REPORT EMAIL] Config:")
    print(f"  API Key: {'*' * 20 if api_key else 'NOT SET'}")
    print(f"  From: {from_email}")
    print(f"  Recipient: {recipient}")

    if not api_key:
        error_msg = "RESEND_API_KEY is not configured. Set RESEND_API_KEY environment variable."
        print(f"[BUG REPORT EMAIL] ERROR: {error_msg}")
        raise ValueError(error_msg)

    # Encode screenshot as base64 for attachment
    encoded_file = base64.b64encode(screenshot_bytes).decode()

    # Send via Resend API
    print(f"[BUG REPORT EMAIL] Sending via Resend API...")
    try:
        response = http_requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'from': f'Risk Runway <{from_email}>',
                'to': [recipient],
                'subject': subject,
                'text': body_text,
                'attachments': [
                    {
                        'filename': screenshot_filename,
                        'content': encoded_file,
                        'content_type': f'image/{screenshot_subtype}'
                    }
                ]
            }
        )

        if response.status_code not in (200, 201):
            print(f"[BUG REPORT EMAIL] FAILED: Resend API error {response.status_code} - {response.text}")
            raise ValueError(f"Failed to send email: {response.text}")

        print(f"[BUG REPORT EMAIL] Success! Status code: {response.status_code}")
        return response
    except ValueError:
        raise
    except Exception as e:
        print(f"[BUG REPORT EMAIL] FAILED: {type(e).__name__}: {str(e)}")
        raise


# ============================================================================
# CHROME EXTENSION API - Parse PDF from URL
# ============================================================================

@bp.route('/api/parse', methods=['POST'])
def parse_pdf_from_url():
    """
    Chrome Extension endpoint: Parse a PDF from a URL.
    Expects JSON: { "pdf_url": "https://..." or "file:///path/to/file.pdf" }
    """
    try:
        # Try to get JSON data
        try:
            data = request.get_json(silent=True)
        except Exception as e:
            return jsonify({'success': False, 'error': f'Invalid JSON: {str(e)}'}), 400
        
        if not data:
            return jsonify({'success': False, 'error': 'No data provided. Ensure Content-Type is application/json'}), 400
        
        pdf_url = data.get('pdf_url')
        
        if not pdf_url:
            return jsonify({'success': False, 'error': 'pdf_url is required'}), 400
        
        temp_filepath = None
        
        # Handle file:// URLs (local files)
        if pdf_url.startswith('file://'):
            # Convert file:// URL to file path
            import urllib.parse
            filepath = urllib.parse.unquote(pdf_url.replace('file://', ''))
            
            if not os.path.exists(filepath):
                return jsonify({'success': False, 'error': f'File not found: {filepath}'}), 400
            
            if not filepath.lower().endswith('.pdf'):
                return jsonify({'success': False, 'error': 'File is not a PDF'}), 400
            
            temp_filepath = filepath
        else:
            # Handle HTTP/HTTPS URLs - download the PDF
            try:
                response = requests.get(pdf_url, timeout=30)
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                return jsonify({'success': False, 'error': f'Failed to download PDF: {str(e)}'}), 400
            
            # Check content type or magic bytes
            content_type = response.headers.get('Content-Type', '')
            if 'pdf' not in content_type.lower() and not response.content[:4] == b'%PDF':
                return jsonify({'success': False, 'error': 'URL does not point to a PDF file'}), 400
            
            # Save to temporary file
            import uuid
            
            temp_filename = f"{uuid.uuid4()}.pdf"
            temp_filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], temp_filename)
            
            with open(temp_filepath, 'wb') as f:
                f.write(response.content)
        
        try:
            # Process the PDF with three-pass parser
            three_pass_result = process_quote_two_pass(temp_filepath, [])
            
            # Extract data from passes
            parsed_data = three_pass_result['pass2_normalized']
            
            return jsonify({
                'success': True,
                'parsed_data': parsed_data,
                'processing_metadata': three_pass_result['processing_metadata']
            })
            
        finally:
            # Clean up temp file only if it was created from downloaded content
            if temp_filepath and temp_filepath.startswith(current_app.config['UPLOAD_FOLDER']):
                if os.path.exists(temp_filepath):
                    os.remove(temp_filepath)
                
    except Exception as e:
        return jsonify({'success': False, 'error': f'Processing error: {str(e)}'}), 500


# ============================================================================
# AUTHENTICATION DECORATOR
# ============================================================================

def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('main.login'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Decorator to require admin role"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('main.login'))
        if session.get('user_role') != 'ADMIN':
            return jsonify({'success': False, 'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function


# ============================================================================
# AUTHENTICATION ROUTES
# ============================================================================

@bp.route('/login', methods=['GET', 'POST'])
def login():
    """Login page and authentication"""
    if request.method == 'GET':
        # If already logged in, redirect to kanban
        if 'user_id' in session:
            return redirect(url_for('main.kanban'))
        return render_template('login.html')

    # POST - handle login
    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')

        if not username or not password:
            return jsonify({'success': False, 'error': 'Username and password required'}), 400

        # Get user from database
        db_session = get_session()
        try:
            user = db_session.query(User).filter_by(username=username).first()

            if not user or not user.check_password(password):
                return jsonify({'success': False, 'error': 'Invalid username or password'}), 401

            if not user.is_active:
                return jsonify({'success': False, 'error': 'Account is inactive'}), 401

            # Set session
            session['user_id'] = user.id
            session['username'] = user.username
            session['full_name'] = user.full_name
            session['user_role'] = user.role.name

            # Record login timestamp
            user.last_login_at = datetime.utcnow()
            db_session.commit()

            # Restore database selection if it was set
            if 'current_database' in session:
                set_current_db(session['current_database'])

            return jsonify({
                'success': True,
                'user': user.to_dict()
            })
        finally:
            db_session.close()

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/logout', methods=['POST'])
def logout():
    """Logout and clear session"""
    session.clear()
    return jsonify({'success': True})


# ============================================================================
# PASSWORD RESET ROUTES
# ============================================================================

@bp.route('/forgot-password', methods=['GET'])
def forgot_password_page():
    """Display the forgot password form"""
    return render_template('forgot_password.html')


@bp.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    """Send a password reset email"""
    import secrets
    try:
        data = request.get_json()
        email = (data.get('email') or '').strip().lower()

        if not email:
            return jsonify({'success': False, 'error': 'Email is required'}), 400

        db_session = get_session()
        try:
            user = db_session.query(User).filter_by(email=email, is_active=True).first()

            # Always return success to prevent email enumeration
            if not user:
                return jsonify({'success': True, 'message': 'If an account with that email exists, a reset link has been sent.'})

            # Generate reset token
            token = secrets.token_urlsafe(32)
            user.password_reset_token = token
            user.password_reset_expires = datetime.utcnow() + timedelta(hours=1)
            db_session.commit()

            # Send reset email via Resend
            base_url = current_app.config.get('APP_BASE_URL', 'http://localhost:5001')
            reset_url = f"{base_url}/reset-password?token={token}"

            _send_password_reset_email(user.email, user.full_name, reset_url)

            return jsonify({'success': True, 'message': 'If an account with that email exists, a reset link has been sent.'})
        finally:
            db_session.close()

    except Exception as e:
        logger.error(f"Forgot password error: {e}")
        return jsonify({'success': False, 'error': 'An error occurred. Please try again.'}), 500


@bp.route('/reset-password', methods=['GET'])
def reset_password_page():
    """Display the reset password form"""
    token = request.args.get('token')
    if not token:
        return redirect(url_for('main.login'))
    return render_template('reset_password.html', token=token)


@bp.route('/api/reset-password', methods=['POST'])
def reset_password():
    """Reset password using token"""
    try:
        data = request.get_json()
        token = data.get('token')
        new_password = data.get('password')

        if not token or not new_password:
            return jsonify({'success': False, 'error': 'Token and password are required'}), 400

        # Validate password
        is_valid, error_msg = User.validate_password(new_password)
        if not is_valid:
            return jsonify({'success': False, 'error': error_msg}), 400

        db_session = get_session()
        try:
            user = db_session.query(User).filter_by(password_reset_token=token).first()

            if not user:
                return jsonify({'success': False, 'error': 'Invalid or expired reset link'}), 400

            if user.password_reset_expires and user.password_reset_expires < datetime.utcnow():
                return jsonify({'success': False, 'error': 'Reset link has expired. Please request a new one.'}), 400

            # Set new password and clear token
            user.set_password(new_password)
            user.password_reset_token = None
            user.password_reset_expires = None
            db_session.commit()

            log_action(
                entity_type='user',
                entity_id=user.id,
                action='password_reset',
                details=f"Password reset completed for {user.username}"
            )

            return jsonify({'success': True, 'message': 'Password has been reset. You can now log in.'})
        finally:
            db_session.close()

    except Exception as e:
        logger.error(f"Reset password error: {e}")
        return jsonify({'success': False, 'error': 'An error occurred. Please try again.'}), 500


def _send_password_reset_email(to_email, user_name, reset_url):
    """Send password reset email via Resend API"""
    import requests as http_requests

    api_key = current_app.config.get('RESEND_API_KEY')
    from_email = current_app.config.get('RESEND_FROM_EMAIL', 'noreply@risk-runway.com')

    if not api_key:
        raise ValueError("RESEND_API_KEY is not configured")

    html_body = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px;">
        <h2 style="color: #1a1a1a; margin-bottom: 24px;">Reset Your Password</h2>
        <p style="color: #4a4a4a; font-size: 16px; line-height: 1.5;">
            Hi {user_name},
        </p>
        <p style="color: #4a4a4a; font-size: 16px; line-height: 1.5;">
            We received a request to reset your password. Click the button below to choose a new one:
        </p>
        <div style="text-align: center; margin: 32px 0;">
            <a href="{reset_url}" style="background-color: #2563eb; color: white; padding: 12px 32px; text-decoration: none; border-radius: 6px; font-size: 16px; font-weight: 500;">
                Reset Password
            </a>
        </div>
        <p style="color: #6b7280; font-size: 14px; line-height: 1.5;">
            This link expires in 1 hour. If you didn't request this, you can safely ignore this email.
        </p>
        <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 32px 0;" />
        <p style="color: #9ca3af; font-size: 12px;">
            Risk Runway — The submission-to-bind pipeline tool
        </p>
    </div>
    """

    response = http_requests.post(
        'https://api.resend.com/emails',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        },
        json={
            'from': f'Risk Runway <{from_email}>',
            'to': [to_email],
            'subject': 'Reset Your Password - Risk Runway',
            'html': html_body
        }
    )

    if response.status_code not in (200, 201):
        logger.error(f"Resend API error: {response.status_code} - {response.text}")
        raise ValueError(f"Failed to send email: {response.text}")

    logger.info(f"Password reset email sent to {to_email}")


# ============================================================================
# KANBAN BOARD - Landing Page
# ============================================================================

@bp.route('/', methods=['GET'])
@login_required
def kanban():
    """Display the Kanban board with all submissions.
    Auto-detects mobile browsers and serves the mobile-optimized view."""
    ua = request.headers.get('User-Agent', '').lower()
    is_mobile = any(kw in ua for kw in ['iphone', 'android', 'mobile', 'ipod'])
    if is_mobile:
        return render_template('mobile.html')
    return render_template('kanban.html')


@bp.route('/mobile', methods=['GET'])
@login_required
def mobile_kanban():
    """Mobile-optimized Kanban board (accessible directly via /mobile)"""
    return render_template('mobile.html')


def _days_until_renewal(effective_date):
    if not effective_date:
        return None
    try:
        renewal_date = datetime.strptime(str(effective_date)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None
    return (renewal_date - datetime.now().date()).days


def _board_stage_key(submission):
    status = str(submission.get('status') or '').strip().lower()

    if status == 'received':
        return 'submission'
    if status == 'in progress':
        return 'quoting'
    return 'bind'


@bp.route('/api/database/current', methods=['GET'])
@login_required
def get_current_database():
    """Get the currently active database name"""
    try:
        return jsonify({
            'success': True,
            'current_database': get_current_db_name(),
            'available_databases': get_available_databases(),
            'switching_enabled': is_database_switching_enabled()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/database/switch', methods=['POST'])
@login_required
def switch_database():
    """Switch to a different database"""
    try:
        data = request.get_json()
        db_name = data.get('database')

        if not db_name:
            return jsonify({'success': False, 'error': 'Database name required'}), 400

        if set_current_db(db_name):
            # Store in session for persistence
            session['current_database'] = db_name
            return jsonify({
                'success': True,
                'current_database': db_name,
                'message': f'Switched to {db_name} database'
            })
        else:
            return jsonify({'success': False, 'error': f'Invalid database name: {db_name}'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/submissions', methods=['GET'])
@login_required
def get_submissions():
    """API endpoint to get all submissions for the Kanban board"""
    try:
        # Check if filtering by assigned user
        filter_assigned = request.args.get('assigned_to_me', 'false').lower() == 'true'

        submissions = get_all_submissions()

        # Filter by assigned user if requested
        if filter_assigned and 'user_id' in session:
            submissions = [s for s in submissions if s.get('assigned_to') == session['user_id']]

        # Attach document summaries and email counts for kanban dropdown and bound indicator.
        submission_ids = [s['id'] for s in submissions]
        docs_by_submission = {sid: [] for sid in submission_ids}
        email_counts_by_submission = {sid: {'sent': 0, 'received': 0} for sid in submission_ids}
        active_binder_submission_ids = set()
        if submission_ids:
            db_session = get_session()
            try:
                # Get documents
                docs = db_session.query(Document).filter(Document.submission_id.in_(submission_ids)).order_by(Document.created_at.desc()).all()
                for doc in docs:
                    docs_by_submission.setdefault(doc.submission_id, []).append({
                        'id': doc.id,
                        'document_type': doc.document_type.value if doc.document_type else None,
                        'name': doc.original_filename,
                        'carrier': doc.carrier,
                        'term_key': doc.term_key,
                        'is_active': doc.is_active
                    })
                    if doc.document_type == DocumentType.BINDER and doc.is_active:
                        active_binder_submission_ids.add(doc.submission_id)
                
                # Get email messages for email counts
                from app.models import EmailMessage
                emails = db_session.query(EmailMessage).filter(EmailMessage.submission_id.in_(submission_ids)).all()
                for email in emails:
                    if email.submission_id in email_counts_by_submission:
                        email_counts_by_submission[email.submission_id]['received'] += 1
                
                # Get sent emails (broker submissions) from audit log
                sent_emails = db_session.query(AuditLog).filter(
                    AuditLog.submission_id.in_(submission_ids),
                    AuditLog.action == 'broker_submission_sent'
                ).all()
                for sent_email in sent_emails:
                    if sent_email.submission_id in email_counts_by_submission:
                        email_counts_by_submission[sent_email.submission_id]['sent'] += 1
                        
            finally:
                db_session.close()

        for sub in submissions:
            sub['documents'] = docs_by_submission.get(sub['id'], [])
            sub['is_bound'] = sub['id'] in active_binder_submission_ids
            sub['email_counts'] = email_counts_by_submission.get(sub['id'], {'sent': 0, 'received': 0})

        return jsonify({'success': True, 'submissions': submissions})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/users', methods=['GET'])
@login_required
def get_users():
    """API endpoint to get all active users"""
    try:
        db_session = get_session()
        try:
            users = db_session.query(User).filter_by(is_active=True).all()

            # Get current user info
            current_user = None
            if 'user_id' in session:
                current_user = db_session.query(User).filter_by(id=session['user_id']).first()

            return jsonify({
                'success': True,
                'users': [u.to_dict() for u in users],
                'current_user': current_user.to_dict() if current_user else None
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# SUBMISSION DETAIL PAGE
# ============================================================================

@bp.route('/submission/<int:submission_id>', methods=['GET'])
@login_required
def submission_detail(submission_id):
    """Display the submission detail page with all quotes"""
    submission = get_submission_by_id(submission_id)
    if not submission:
        return "Submission not found", 404
    stage_key = _board_stage_key(submission)
    print(f"[DEBUG] Submission {submission_id}: status='{submission.get('status')}', stage_key='{stage_key}'")

    # Mobile detection
    ua = request.headers.get('User-Agent', '').lower()
    is_mobile = any(kw in ua for kw in ['iphone', 'android', 'mobile', 'ipod'])
    if is_mobile:
        return render_template(
            'mobile_submission.html',
            submission_id=submission_id,
            stage_key=stage_key,
            is_admin=session.get('user_role') == 'ADMIN'
        )

    return render_template(
        'submission.html',
        submission_id=submission_id,
        stage_key=stage_key,
        is_admin=session.get('user_role') == 'ADMIN'
    )


@bp.route('/api/submission/<int:submission_id>', methods=['GET'])
@login_required
def get_submission_detail(submission_id):
    """API endpoint to get submission details with all quotes"""
    try:
        submission = get_submission_by_id(submission_id)
        if not submission:
            return jsonify({'success': False, 'error': 'Submission not found'}), 404

        # submission is now a dict with 'quotes' already included
        quotes = submission.get('quotes', [])

        # Parse extracted_json for each quote
        for quote in quotes:
            if quote['extracted_json']:
                try:
                    quote['parsed_data'] = json.loads(quote['extracted_json'])
                except:
                    quote['parsed_data'] = None

        db_session = get_session()
        try:
            # Read submission_intake from the column (preferred)
            # Fallback to audit log for submissions created before the column existed
            if not submission.get('submission_intake'):
                intake_log = db_session.query(AuditLog).filter(
                    AuditLog.submission_id == submission_id,
                    AuditLog.action.in_(['submission_intake_parsed', 'submission_created_manual'])
                ).order_by(AuditLog.timestamp.desc()).first()
                if intake_log and intake_log.details:
                    try:
                        submission['submission_intake'] = json.loads(intake_log.details)
                    except Exception:
                        submission['submission_intake'] = None
                else:
                    submission['submission_intake'] = None

            docs = db_session.query(Document).filter(Document.submission_id == submission_id).order_by(Document.created_at.desc()).all()
            submission['documents'] = []
            submission['is_bound'] = False
            for doc in docs:
                item = doc.to_dict()
                item['download_url'] = _document_download_url(doc.id)
                submission['documents'].append(item)
                if doc.document_type == DocumentType.BINDER and doc.is_active:
                    submission['is_bound'] = True
        finally:
            db_session.close()

        return jsonify({
            'success': True,
            'submission': submission,
            'quotes': quotes
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/submission/<int:submission_id>/report_bug', methods=['POST'])
@login_required
def report_submission_bug(submission_id):
    """Create and send a bug report email for a submission detail view."""
    try:
        data = request.get_json() or {}
        quote_id = data.get('quote_id')
        description = (data.get('description') or '').strip()
        screenshot_data_url = data.get('screenshot_data_url')
        page_url = data.get('page_url', '')

        if not quote_id:
            return jsonify({'success': False, 'error': 'quote_id is required'}), 400

        if not screenshot_data_url:
            return jsonify({'success': False, 'error': 'A screenshot is required'}), 400

        screenshot_subtype = None
        if screenshot_data_url.startswith('data:image/png;base64,'):
            screenshot_subtype = 'png'
        elif screenshot_data_url.startswith('data:image/jpeg;base64,'):
            screenshot_subtype = 'jpeg'
        else:
            return jsonify({'success': False, 'error': 'Screenshot must be PNG or JPEG'}), 400

        db_session = get_session()
        try:
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            quote = db_session.query(Quote).filter_by(id=quote_id, submission_id=submission_id).first()
            if not quote:
                return jsonify({'success': False, 'error': 'Quote not found for this submission'}), 404

            quote_data = {}
            if quote.extracted_json:
                try:
                    quote_data = json.loads(quote.extracted_json)
                except Exception:
                    quote_data = {}

            quote_numbers = []
            for policy in quote_data.get('policies', []) if isinstance(quote_data, dict) else []:
                policy_number = policy.get('policy_number')
                if policy_number:
                    quote_numbers.append(policy_number)

            screenshot_b64 = screenshot_data_url.split(',', 1)[1]
            screenshot_bytes = base64.b64decode(screenshot_b64)

            timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
            reporter = session.get('username', 'unknown')
            report_body = (
                f"Bug reported by: {reporter}\n"
                f"Reported at: {timestamp}\n"
                f"Page URL: {page_url}\n\n"
                f"Submission ID: {submission.id}\n"
                f"Insured: {submission.insured_name}\n"
                f"Effective Date: {submission.effective_date}\n\n"
                f"Quote ID: {quote.id}\n"
                f"Quote File: {os.path.basename(quote.raw_document_path)}\n"
                f"Carrier: {quote.carrier_name or 'N/A'}\n"
                f"Quote Number: {(quote_data.get('quote_number') if isinstance(quote_data, dict) else None) or 'N/A'}\n"
                f"Account Number: {(quote_data.get('account_number') if isinstance(quote_data, dict) else None) or 'N/A'}\n"
                f"Policy Numbers: {', '.join(quote_numbers) if quote_numbers else 'N/A'}\n\n"
                f"Bug Description:\n{description or '(none provided)'}\n"
            )

            subject = f"[RiskRunway Mapper Bug] Submission {submission.id} / Quote {quote.id}"
            extension = 'jpg' if screenshot_subtype == 'jpeg' else 'png'
            screenshot_filename = f"submission_{submission.id}_quote_{quote.id}_bug.{extension}"
            try:
                print(f"[BUG REPORT] Attempting to send bug report email...")
                _send_bug_report_email(
                    subject,
                    report_body,
                    screenshot_bytes,
                    screenshot_filename,
                    screenshot_subtype=screenshot_subtype
                )
                print(f"[BUG REPORT] Email sent successfully!")
            except ValueError as e:
                error_msg = f'Configuration error: {str(e)}'
                print(f"[BUG REPORT] ERROR: {error_msg}")
                return jsonify({'success': False, 'error': error_msg}), 500
            except Exception as e:
                error_msg = f'Email send failed: {type(e).__name__}: {str(e)}'
                print(f"[BUG REPORT] ERROR: {error_msg}")
                import traceback
                traceback.print_exc()
                return jsonify({'success': False, 'error': error_msg}), 500

            log_action(
                entity_type='submission',
                entity_id=submission.id,
                action='bug_reported',
                submission_id=submission.id,
                quote_id=quote.id,
                details=f"Bug reported by {reporter}"
            )

            return jsonify({'success': True})
        finally:
            db_session.close()

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# GENERAL BUG REPORT (from Kanban board or other non-submission pages)
# ============================================================================

@bp.route('/api/report_bug', methods=['POST'])
@login_required
def report_general_bug():
    """Create and send a general bug report email (not tied to a specific submission)."""
    try:
        data = request.get_json() or {}
        description = (data.get('description') or '').strip()
        page_url = data.get('page_url', '')
        page = data.get('page', 'unknown')

        if not description:
            return jsonify({'success': False, 'error': 'Description is required'}), 400

        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        reporter = session.get('username', 'unknown')

        report_body = (
            f"Bug reported by: {reporter}\n"
            f"Reported at: {timestamp}\n"
            f"Page: {page}\n"
            f"Page URL: {page_url}\n\n"
            f"Bug Description:\n{description}\n"
        )

        subject = f"[RiskRunway Bug] General - {page} - {reporter}"

        # Send without screenshot
        import requests as http_requests
        api_key = current_app.config.get('RESEND_API_KEY')
        from_email = current_app.config.get('RESEND_FROM_EMAIL', 'noreply@risk-runway.com')
        recipient = current_app.config.get('BUG_REPORT_RECIPIENT', 'chrisbouy@gmail.com')

        if not api_key:
            return jsonify({'success': False, 'error': 'RESEND_API_KEY is not configured'}), 500

        response = http_requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            },
            json={
                'from': f'Risk Runway <{from_email}>',
                'to': [recipient],
                'subject': subject,
                'text': report_body,
            }
        )

        if response.status_code not in (200, 201):
            return jsonify({'success': False, 'error': f'Email send failed: {response.text}'}), 500

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# SUBMISSION CREATION
# ============================================================================

@bp.route('/api/submission/create', methods=['POST'])
@login_required
def create_submission_entry():
    """
    Create a new submission from either:
    1) Manually-entered insured name, or
    2) Uploaded application document (parsed for stage-1 info).
    """
    try:
        print(f"Received submission creation request with form data: {request.form} and files: {request.files}")
        insured_name = (request.form.get('insured_name') or '').strip()
        coverage_types_list = request.form.getlist('coverage_types')
        # Backward compat: fall back to single value field
        if not coverage_types_list:
            single = (request.form.get('coverage_type_requested') or '').strip()
            coverage_types_list = [single] if single else []
        else:
            coverage_types_list = [ct.strip() for ct in coverage_types_list if ct.strip()]
        file = request.files.get('file')
        has_file = bool(file and file.filename)

        if not insured_name and not has_file:
            return jsonify({'success': False, 'error': 'Provide insured name or upload an application'}), 400

        intake_data = None
        effective_date = datetime.now().strftime('%Y-%m-%d')
        state = None

        if has_file:
            if not allowed_file(file.filename):
                return jsonify({'success': False, 'error': 'Invalid file type'}), 400

            filename = secure_filename(file.filename)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            unique_filename = f"{timestamp}_{filename}"
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)
            file.save(filepath)

            application_result = process_application_two_pass(filepath)
            parsed_data = application_result['pass2_normalized']

            parsed_insured_name = (parsed_data.get('insured') or {}).get('name')
            if not insured_name and parsed_insured_name:
                insured_name = parsed_insured_name.strip()

            state = (parsed_data.get('insured') or {}).get('address', {}).get('state')
            submission_fields = parsed_data.get('submission') or {}
            effective_date = submission_fields.get('effective_date') or effective_date
            coverage_types = submission_fields.get('coverage_types_needed') or []

            # Stage-1 intake intentionally excludes wholesale broker.
            intake_data = {
                'source': 'application',
                'application_filename': filename,
                'insured': parsed_data.get('insured'),
                'retail_agent': parsed_data.get('retail_agent'),
                'quote_number': parsed_data.get('quote_number'),
                'account_number': parsed_data.get('account_number'),
                'coverage_types': coverage_types,
                'effective_date': effective_date,
                'processing_metadata': application_result.get('processing_metadata', {})
            }
        else:
            from app.parsers.coverage_normalizer import normalize_coverage_list
            coverage_types_list = normalize_coverage_list(coverage_types_list)
            intake_data = {
                'source': 'manual',
                'insured': {'name': insured_name, 'address': None},
                'retail_agent': None,
                'quote_number': None,
                'account_number': None,
                'coverage_types': coverage_types_list,
                'effective_date': effective_date
            }

        if not insured_name:
            return jsonify({'success': False, 'error': 'Could not determine insured name from application'}), 400

        submission_id = create_submission(
            insured_name=insured_name,
            effective_date=effective_date,
            state=state,
            user=session.get('username'),
            assigned_to=session.get('user_id')
        )

        if has_file:
            object_key = _build_storage_key(submission_id, DocumentType.APPLICATION.name, filename,session.get('user_id'),insured_name)
            storage_provider, storage_key = _storage_upload(filepath, object_key, file.content_type)
            db_session = get_session()
            try:
                app_doc = Document(
                    submission_id=submission_id,
                    quote_id=None,
                    document_type=DocumentType.APPLICATION,
                    carrier=None,
                    term_key=effective_date,
                    version=1,
                    is_active=True,
                    storage_provider=storage_provider,
                    storage_key=storage_key,
                    original_filename=filename,
                    content_type=file.content_type,
                    size_bytes=os.path.getsize(filepath) if os.path.exists(filepath) else None,
                    uploaded_by=session.get('username')
                )
                db_session.add(app_doc)
                db_session.commit()
            finally:
                db_session.close()
                # Clean up temp file after processing
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception as e:
                print(f"Warning: Could not delete temp file {filepath}: {e}")
    
        log_action(
            entity_type='submission',
            entity_id=submission_id,
            action='submission_intake_parsed' if has_file else 'submission_created_manual',
            user=session.get('username'),
            submission_id=submission_id,
            details=json.dumps(intake_data)
        )

        # Persist intake data on the submission record itself
        db_session = get_session()
        try:
            sub = db_session.query(Submission).filter_by(id=submission_id).first()
            if sub:
                sub.submission_intake = json.dumps(intake_data)
                # Generate abbreviated name for mobile display
                from app.short_name import generate_short_name
                sub.short_name = generate_short_name(insured_name)
                db_session.commit()
        finally:
            db_session.close()

        print(f"Created submission {submission_id} with intake data: {intake_data}")
        return jsonify({
            'success': True,
            'submission_id': submission_id,
            'submission_intake': intake_data
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# DOCUMENT MANAGEMENT
# ============================================================================

@bp.route('/api/submission/<int:submission_id>/documents', methods=['GET'])
@login_required
def list_submission_documents(submission_id):
    """List submission documents, optionally filtered by document_type."""
    try:
        document_type = (request.args.get('document_type') or '').strip()

        db_session = get_session()
        try:
            query = db_session.query(Document).filter(Document.submission_id == submission_id)
            if document_type:
                try:
                    enum_type = DocumentType[document_type.upper()]
                    query = query.filter(Document.document_type == enum_type)
                except KeyError:
                    return jsonify({'success': False, 'error': 'Invalid document_type'}), 400

            documents = query.order_by(Document.created_at.desc()).all()
            payload = []
            for doc in documents:
                item = doc.to_dict()
                item['download_url'] = _document_download_url(doc.id)
                payload.append(item)
            return jsonify({'success': True, 'documents': payload})
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/submission/<int:submission_id>/documents', methods=['POST'])
@login_required
def upload_submission_document(submission_id):
    """Upload a document linked to a submission and persist metadata."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file part'}), 400
        file = request.files['file']
        if not file.filename:
            return jsonify({'success': False, 'error': 'No selected file'}), 400
        if not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Invalid file type'}), 400

        document_type_raw = (request.form.get('document_type') or '').strip()
        if not document_type_raw:
            return jsonify({'success': False, 'error': 'document_type is required'}), 400
        try:
            document_type = DocumentType[document_type_raw.upper()]
        except KeyError:
            return jsonify({'success': False, 'error': 'Invalid document_type'}), 400

        carrier = (request.form.get('carrier') or '').strip() or None
        quote_id = request.form.get('quote_id', type=int)
        term_key = (request.form.get('term_key') or '').strip() or None
        insured_name = (request.form.get('insured_name') or '').strip() or None

        # Save temp file locally first
        filename = secure_filename(file.filename)
        temp_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}_{filename}"
        temp_path = os.path.join(current_app.config['UPLOAD_FOLDER'], temp_name)
        file.save(temp_path)

        db_session = get_session()
        try:
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            if quote_id:
                quote = db_session.query(Quote).filter_by(id=quote_id, submission_id=submission_id).first()
                if not quote:
                    return jsonify({'success': False, 'error': 'Quote not found for this submission'}), 404

            if not term_key:
                term_key = submission.effective_date or datetime.now().strftime('%Y-%m-%d')

            # Versioning support: increment within same submission/type/carrier/term.
            latest = db_session.query(Document).filter(
                Document.submission_id == submission_id,
                Document.document_type == document_type,
                Document.carrier == carrier,
                Document.term_key == term_key
            ).order_by(Document.version.desc()).first()
            next_version = (latest.version + 1) if latest else 1

            # Single active binder per term.
            if document_type == DocumentType.BINDER:
                db_session.query(Document).filter(
                    Document.submission_id == submission_id,
                    Document.document_type == DocumentType.BINDER,
                    Document.term_key == term_key,
                    Document.is_active == True
                ).update({'is_active': False}, synchronize_session=False)

            object_key = _build_storage_key(submission_id, document_type.name, filename, session.get('user_id'), insured_name)
            storage_provider, storage_key = _storage_upload(temp_path, object_key, file.content_type)

            doc = Document(
                submission_id=submission_id,
                quote_id=quote_id,
                document_type=document_type,
                carrier=carrier,
                term_key=term_key,
                version=next_version,
                is_active=True,
                storage_provider=storage_provider,
                storage_key=storage_key,
                original_filename=filename,
                content_type=file.content_type,
                size_bytes=os.path.getsize(temp_path) if os.path.exists(temp_path) else None,
                uploaded_by=session.get('username')
            )
            db_session.add(doc)

            # Binder upload marks submission as bound-facing card status.
            if document_type == DocumentType.BINDER:
                submission.status = SubmissionStatus.SENT_TO_FINANCE

            db_session.commit()

            # Capture doc data while session is still open
            doc_id = doc.id
            doc_dict = {
                'id': doc_id,
                'submission_id': submission_id,
                'quote_id': quote_id,
                'document_type': document_type.value if hasattr(document_type, 'value') else document_type.name,
                'carrier': carrier,
                'term_key': term_key,
                'version': next_version,
                'is_active': True,
                'original_filename': filename,
                'content_type': file.content_type,
                'uploaded_by': session.get('username'),
                'storage_provider': storage_provider,
                'storage_key': storage_key
            }

            log_action(
                entity_type='submission',
                entity_id=submission_id,
                action='document_uploaded',
                user=session.get('username'),
                submission_id=submission_id,
                quote_id=quote_id,
                details=json.dumps({
                    'document_id': doc_id,
                    'document_type': document_type.name,
                    'carrier': carrier,
                    'term_key': term_key,
                    'version': next_version
                })
            )

            doc_dict['download_url'] = _document_download_url(doc_id)
            return jsonify({'success': True, 'document': doc_dict})
        finally:
            db_session.close()
            # Clean up temp file after processing
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception as e:
            print(f"Warning: Could not delete temp file {filepath}: {e}")
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/documents/<int:document_id>/download', methods=['GET'])
@login_required
def download_document(document_id):
    """Download or redirect to document object storage."""
    db_session = get_session()
    try:
        doc = db_session.query(Document).filter_by(id=document_id).first()
        if not doc:
            return "Document not found", 404

        if doc.storage_provider == 's3':
            try:
                import boto3
                from io import BytesIO
                bucket = current_app.config.get('S3_BUCKET')
                client = boto3.client(
                    's3',
                    region_name=current_app.config.get('S3_REGION') or None,
                    endpoint_url=current_app.config.get('S3_ENDPOINT_URL') or None
                )
                # For JSON/text correspondence files, stream through server to avoid CORS issues
                if doc.content_type in ('application/json', 'text/plain'):
                    obj = client.get_object(Bucket=bucket, Key=doc.storage_key)
                    file_data = obj['Body'].read()
                    return send_file(
                        BytesIO(file_data),
                        as_attachment=False,
                        download_name=doc.original_filename,
                        mimetype=doc.content_type
                    )
                # For other files (PDFs etc.), redirect to presigned URL
                signed_url = client.generate_presigned_url(
                    ClientMethod='get_object',
                    Params={
                        'Bucket': bucket,
                        'Key': doc.storage_key,
                        'ResponseContentDisposition': f'inline; filename="{doc.original_filename}"'
                    },
                    ExpiresIn=300
                )
                return redirect(signed_url)
            except Exception as err:
                return f"S3 download failed: {err}", 500

        # local storage provider
        if doc.storage_key.startswith(current_app.config['UPLOAD_FOLDER']):
            local_path = doc.storage_key
        else:
            local_path = os.path.join(current_app.config.get('DOCUMENTS_LOCAL_FOLDER', current_app.config['UPLOAD_FOLDER']), doc.storage_key)
        if not os.path.exists(local_path):
            return "Document file missing", 404

        return send_file(local_path, as_attachment=False, download_name=doc.original_filename, mimetype=doc.content_type)
    finally:
        db_session.close()


@bp.route('/api/documents/<int:document_id>', methods=['DELETE'])
@login_required
def delete_document(document_id):
    """Delete a document by ID, removing the stored file and DB record."""
    db_session = get_session()
    try:
        doc = db_session.query(Document).filter_by(id=document_id).first()
        if not doc:
            return jsonify({'success': False, 'error': 'Document not found'}), 404

        submission_id = doc.submission_id
        doc_type = doc.document_type.value if doc.document_type else None
        doc_name = doc.original_filename

        # Remove stored file
        if doc.storage_provider == 's3':
            try:
                import boto3
                bucket = current_app.config.get('S3_BUCKET')
                client = boto3.client(
                    's3',
                    region_name=current_app.config.get('S3_REGION') or None,
                    endpoint_url=current_app.config.get('S3_ENDPOINT_URL') or None
                )
                client.delete_object(Bucket=bucket, Key=doc.storage_key)
            except Exception as err:
                print(f"[DOC DELETE] S3 delete failed: {err}")
        else:
            local_root = current_app.config.get('DOCUMENTS_LOCAL_FOLDER', current_app.config['UPLOAD_FOLDER'])
            if doc.storage_key.startswith(current_app.config['UPLOAD_FOLDER']):
                local_path = doc.storage_key
            else:
                local_path = os.path.join(local_root, doc.storage_key)
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
            except Exception as err:
                print(f"[DOC DELETE] Local file delete failed: {err}")

        db_session.delete(doc)
        db_session.commit()

        log_action(
            entity_type='submission',
            entity_id=submission_id,
            action='document_deleted',
            user=session.get('username'),
            submission_id=submission_id,
            details=json.dumps({
                'document_id': document_id,
                'document_type': doc_type,
                'filename': doc_name
            })
        )

        return jsonify({'success': True})
    except Exception as e:
        db_session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db_session.close()


@bp.route('/api/quote/<int:quote_id>/file', methods=['GET'])
@login_required
def view_quote_file(quote_id):
    """Open the original uploaded quote file via the Document storage system."""
    db_session = get_session()
    try:
        quote = db_session.query(Quote).filter_by(id=quote_id).first()
        if not quote:
            return "Quote not found", 404

        # First try the Document table (the permanent storage record)
        doc = db_session.query(Document).filter_by(
            quote_id=quote_id,
            document_type=DocumentType.QUOTE
        ).order_by(Document.version.desc()).first()

        if doc:
            if doc.storage_provider == 's3':
                try:
                    import boto3
                    from io import BytesIO
                    bucket = current_app.config.get('S3_BUCKET')
                    client = boto3.client(
                        's3',
                        region_name=current_app.config.get('S3_REGION') or None,
                        endpoint_url=current_app.config.get('S3_ENDPOINT_URL') or None
                    )
                    obj = client.get_object(Bucket=bucket, Key=doc.storage_key)
                    file_data = obj['Body'].read()
                    return send_file(
                        BytesIO(file_data),
                        as_attachment=False,
                        download_name=doc.original_filename,
                        mimetype=doc.content_type or 'application/pdf'
                    )
                except Exception as err:
                    print(f"[QUOTE VIEW] S3 download failed: {err}")
                    return "Quote file download failed", 500

            # Local storage
            local_root = current_app.config.get('UPLOAD_FOLDER')
            if doc.storage_key.startswith(local_root):
                local_path = doc.storage_key
            else:
                local_path = os.path.join(local_root, doc.storage_key)
            if os.path.exists(local_path):
                return send_file(local_path, as_attachment=False, download_name=doc.original_filename, mimetype=doc.content_type or 'application/pdf')

        # Fallback: try legacy raw_document_path directly
        if quote.raw_document_path and os.path.exists(quote.raw_document_path):
            return send_file(quote.raw_document_path, as_attachment=False, download_name=os.path.basename(quote.raw_document_path))

        return "Quote file missing", 404
    finally:
        db_session.close()


# ============================================================================
# EMAIL SCRAPING
# ============================================================================

@bp.route('/api/email/scrape', methods=['POST'])
@login_required
def trigger_email_scrape():
    """Trigger email scrape via OAuth connected accounts"""
    try:
        if not current_app.config.get('EMAIL_SCRAPING_ENABLED', False):
            return jsonify({'success': False, 'error': 'Email scraping is disabled'}), 400
        else:
            print("Email scraping is enabled")
        user_id = session.get('user_id')
        db_session = get_session()
        print(f"Got database session for user {user_id}")
        # print(f"Current app config: {current_app.config}")
        
        try:
            # Check for connected OAuth accounts first
            oauth_accounts = db_session.query(ConnectedAccount).filter(
                ConnectedAccount.user_id == user_id,
                ConnectedAccount.status == ConnectedAccountStatus.ACTIVE
            ).all()
            print(f"Found {len(oauth_accounts)} connected OAuth accounts")
            
            results = {
                'success': True,
                'processed': 0,
                'matched': 0,
                'new_emails': 0,
                'accounts_checked': [],
                'emails': []
            }
            
            # Try OAuth accounts first
            if oauth_accounts:
                print(f"Processing {len(oauth_accounts)} OAuth accounts")
                needs_reauth = False
                reauth_provider = None
                for account in oauth_accounts:
                    try:
                        result = _scrape_emails_with_oauth(account, db_session, user_id)
                        print(f"OAuth result: {result}")
                        if result.get('success'):
                            results['processed'] += result.get('processed', 0)
                            results['new_emails'] += result.get('new_emails', 0)
                            results['accounts_checked'].append(f"{account.provider.value}: {account.email_address}")
                            results['source'] = 'OAuth'
                            # Collect emails for frontend display (stateless — no DB)
                            results['emails'].extend(result.get('emails', []))
                        elif result.get('needs_reauth'):
                            needs_reauth = True
                            reauth_provider = result.get('provider', account.provider.value.lower())
                    except Exception as oauth_error:
                        logger.error(f"OAuth email scraping failed for {account.email_address}: {oauth_error}")
                        results['accounts_checked'].append(f"{account.provider.value}: {account.email_address} (failed: {str(oauth_error)})")
                
                # If re-auth is needed and no accounts succeeded, tell the frontend
                if needs_reauth and not results.get('source'):
                    db_session.close()
                    return jsonify({
                        'success': False,
                        'needs_reauth': True,
                        'provider': reauth_provider,
                        'error': 'Email account token expired. Re-authenticating...'
                    })
                
                # If we successfully processed at least one OAuth account, return success
                if results['accounts_checked']:
                    db_session.close()
                    
                    # Log the action
                    log_action(
                        entity_type='system',
                        entity_id=0,
                        action='email_scrape_triggered',
                        user=session.get('username'),
                        details=json.dumps(results)
                    )
                    
                    return jsonify(results)
            
            # No connected OAuth accounts — tell user to connect
            if not oauth_accounts:
                results['success'] = False
                results['error'] = 'No email account connected. Please connect your email account first.'
                results['needs_connect'] = True
            
            db_session.close()
            
            # Log the action
            log_action(
                entity_type='system',
                entity_id=0,
                action='email_scrape_triggered',
                user=session.get('username'),
                details=json.dumps(results)
            )
            
            return jsonify(results)
            
        finally:
            db_session.close()
        
    except Exception as e:
        logger.error(f"Email scraping error: {str(e)}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500




def _get_user_broker_emails(db_session: Session, user_id: int) -> List[str]:
    """Get all active broker email addresses for a user"""
    broker_emails = []
    try:
        brokers = db_session.query(Broker).filter(
            Broker.user_id == user_id,
            Broker.is_enabled == True,
            Broker.email.isnot(None)
        ).all()
        broker_emails = [b.email.strip().lower() for b in brokers if b.email]
    except Exception as e:
        logger.warning(f"Failed to get broker emails for user {user_id}: {e}")
    return broker_emails


def _expand_insured_name_variants(name: str) -> List[str]:
    """
    Generate all subject-search variants for an insured name.

    Examples:
        'LTR Holdings, LLC dba Wolf Disposals' produces:
            ['ltr holdings, llc dba wolf disposals',
             'ltr holdings, llc',
             'ltr holdings',
             'ltr',
             'wolf disposals',
             'wolf']
    """
    import re

    if not name:
        return []

    ENTITY_SUFFIXES = [
        'l.l.c.', 'llc',
        'l.l.p.', 'llp',
        'p.l.l.c.', 'pllc',
        'p.l.c.', 'plc',
        'p.c.', 'pc',
        'l.p.', 'lp',
        'inc.', 'inc', 'incorporated',
        'corp.', 'corp', 'corporation',
        'co.', 'co', 'company',
        'ltd.', 'ltd', 'limited',
    ]

    variants = set()
    full = name.strip().lower()
    if not full:
        return []
    variants.add(full)

    # Split on dba / d/b/a / d.b.a.
    dba_parts = re.split(r'\s+(?:dba|d/b/a|d\.b\.a\.?)\s+', full)
    parts_to_process = [p.strip() for p in dba_parts if p.strip()]

    # Always include each dba half as a variant
    for part in parts_to_process:
        variants.add(part)

    for part in parts_to_process:
        # Strip trailing entity suffix (with optional comma before it)
        core = part
        # Remove trailing entity suffixes iteratively (handles 'Foo Holdings, Inc.')
        changed = True
        while changed:
            changed = False
            for suffix in ENTITY_SUFFIXES:
                # Match suffix at the end, optionally preceded by comma+space or just space
                pattern = r'(?:,\s*|\s+)' + re.escape(suffix) + r'\s*$'
                new_core = re.sub(pattern, '', core).strip()
                if new_core and new_core != core:
                    core = new_core
                    changed = True
        if core:
            variants.add(core)
            # First word (e.g. 'ltr' from 'ltr holdings')
            first_word = core.split()[0] if core.split() else ''
            if first_word:
                variants.add(first_word)

    # Ampersand <-> 'and' swaps
    extra = set()
    for v in variants:
        if '&' in v:
            extra.add(re.sub(r'\s*&\s*', ' and ', v).strip())
        if re.search(r'\s+and\s+', v):
            extra.add(re.sub(r'\s+and\s+', ' & ', v).strip())
    variants.update(extra)

    # Clean up: collapse whitespace, drop empties
    cleaned = set()
    for v in variants:
        v = re.sub(r'\s+', ' ', v).strip()
        if v:
            cleaned.add(v)

    return list(cleaned)


def _get_user_quote_subjects(db_session: Session, user_id: int) -> List[str]:
    """Get insured-name search variants from submissions assigned to user.

    Generates loose variants (entity suffix stripped, dba parts split out, etc.)
    so emails with any common form of the name in the subject get matched.
    """
    quote_subjects = []
    try:
        # Get submissions assigned to this user (any status)
        submissions = db_session.query(Submission).filter(
            Submission.assigned_to == user_id
        ).all()

        names = set()
        for sub in submissions:
            if sub.insured_name:
                for variant in _expand_insured_name_variants(sub.insured_name):
                    names.add(variant)

        quote_subjects = list(names)
    except Exception as e:
        logger.warning(f"Failed to get quote subjects for user {user_id}: {e}")
    return quote_subjects


def _scrape_emails_with_oauth(account: ConnectedAccount, db_session: Session, user_id: int) -> Dict:
    """
    Fetch emails using OAuth credentials from a connected account.
    Returns email metadata directly — does NOT store anything in the database.
    Deduplication is handled by only fetching unread emails from the provider.
    """
    from datetime import timedelta
    from app.oauth_services import get_oauth_service
    
    try:
        # Get the provider config
        config = {
            'GMAIL_CLIENT_ID': current_app.config.get('GMAIL_CLIENT_ID'),
            'GMAIL_CLIENT_SECRET': current_app.config.get('GMAIL_CLIENT_SECRET'),
            'GMAIL_REDIRECT_URI': current_app.config.get('GMAIL_REDIRECT_URI'),
            'MICROSOFT_CLIENT_ID': current_app.config.get('MICROSOFT_CLIENT_ID'),
            'MICROSOFT_CLIENT_SECRET': current_app.config.get('MICROSOFT_CLIENT_SECRET'),
            'MICROSOFT_REDIRECT_URI': current_app.config.get('MICROSOFT_REDIRECT_URI'),
            'MICROSOFT_TENANT_ID': current_app.config.get('MICROSOFT_TENANT_ID', 'common')
        }
        
        # Get tokens
        tokens = account.get_decrypted_tokens()
        access_token = tokens.get('access_token') if tokens else None
        refresh_token = tokens.get('refresh_token') if tokens else None
        
        # Get OAuth service
        provider_str = account.provider.value.lower()
        print(f"Getting OAuth service for {provider_str}")
        oauth_service = get_oauth_service(provider_str, config)
        
        # Auto-refresh token if expired or missing
        if not access_token or (account.expires_at and account.expires_at < datetime.utcnow()):
            if not refresh_token:
                logger.warning(f"No refresh token for OAuth account {account.email_address} - re-auth required")
                return {
                    'success': False,
                    'error': f'Re-authentication required for {account.email_address}',
                    'needs_reauth': True,
                    'provider': provider_str
                }
            
            logger.info(f"Access token expired for {account.email_address}, refreshing...")
            try:
                new_tokens = oauth_service.refresh_access_token(refresh_token)
                account.set_encrypted_tokens(new_tokens)
                expires_in = new_tokens.get('expires_in', 3600)
                account.expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
                account.status = ConnectedAccountStatus.ACTIVE
                account.last_error = None
                db_session.commit()
                access_token = new_tokens.get('access_token')
                logger.info(f"Token refreshed successfully for {account.email_address}")
            except Exception as refresh_err:
                logger.error(f"Token refresh failed for {account.email_address}: {refresh_err}")
                account.status = ConnectedAccountStatus.ERROR
                account.last_error = f"Token refresh failed: {str(refresh_err)}"
                db_session.commit()
                return {
                    'success': False,
                    'error': f'Token refresh failed for {account.email_address}. Please re-connect your email account.',
                    'needs_reauth': True,
                    'provider': provider_str
                }
        
        # Fetch emails with default filters (broker emails + insured names + has attachments)
        broker_emails = _get_user_broker_emails(db_session, user_id)
        quote_subjects = _get_user_quote_subjects(db_session, user_id)
        since_date = datetime.now() - timedelta(days=24)

        print(f"[EMAIL SCRAPER] Running in mode: oauth")
        unified_emails = oauth_service.fetch_emails(
            access_token=access_token,
            max_results=50,
            since_date=since_date,
            broker_emails=broker_emails if broker_emails else None,
            quote_subjects=quote_subjects if quote_subjects else None,
            require_attachments=True
        )
        
        print(f"[EMAIL SCRAPER] OAuth fetched {len(unified_emails) if unified_emails else 0} emails from {account.email_address}")
        # Log match info
        filter_info = f"brokers={len(broker_emails)}, subjects={len(quote_subjects)}"
        matched_count = len(unified_emails) if unified_emails else 0
        print(f"[EMAIL SCRAPER] {matched_count} emails matched (broker or insured name) out of {matched_count}")
        
        if not unified_emails:
            return {
                'success': True,
                'processed': 0,
                'new_emails': 0,
                'emails': []
            }

        # Exclude emails sent from the user's own address (Graph API ne filter
        # doesn't work on nested from/emailAddress/address, so filter client-side)
        own_email = (account.email_address or '').strip().lower()
        if own_email:
            before_count = len(unified_emails)
            unified_emails = [e for e in unified_emails if (e.from_email or '').strip().lower() != own_email]
            print(f"[EMAIL] Excluded {before_count - len(unified_emails)} emails from self ({own_email}). Senders were: {[(e.from_email or '').strip().lower() for e in unified_emails]}")

        if not unified_emails:
            return {
                'success': True,
                'processed': 0,
                'new_emails': 0,
                'emails': []
            }
        
        # Build email list for frontend (no DB writes)
        email_list = []
        for unified_email in unified_emails:
            email_data = {
                'message_id': unified_email.message_id,
                'from_email': unified_email.from_email,
                'from_name': unified_email.from_name,
                'subject': unified_email.subject or '(No subject)',
                'body_text': unified_email.body_text or '',
                'received_date': unified_email.date.isoformat() if unified_email.date else None,
                'has_attachments': len(unified_email.attachments) > 0,
                'attachment_count': len(unified_email.attachments),
                'provider': provider_str,
                'account_id': account.id,
                'attachments': [
                    {
                        'attachment_id': att.get('attachment_id', ''),
                        'filename': att.get('filename', ''),
                        'content_type': att.get('content_type', ''),
                        'size': att.get('size', 0)
                    }
                    for att in unified_email.attachments
                ]
            }
            email_list.append(email_data)
        
        return {
            'success': True,
            'processed': len(email_list),
            'new_emails': len(email_list),
            'emails': email_list,
            'provider': provider_str
        }
        
    except Exception as e:
        logger.error(f"OAuth email scraping error: {str(e)}", exc_info=True)
        raise



@bp.route('/api/email/status', methods=['GET'])
@login_required
def get_email_scrape_status():
    """Get current email scraping status and configuration"""
    try:
        status = {
            'enabled': current_app.config.get('EMAIL_SCRAPING_ENABLED', False),
            'configured': True,  # OAuth-based, configured via connected accounts
            'scrape_interval_minutes': current_app.config.get('EMAIL_SCRAPE_INTERVAL_MINUTES', 5)
        }
        
        # Get last scrape results from audit log
        db_session = get_session()
        try:
            last_scrape = db_session.query(AuditLog).filter(
                AuditLog.action == 'email_scrape_triggered'
            ).order_by(AuditLog.timestamp.desc()).first()
            
            if last_scrape and last_scrape.details:
                try:
                    last_result = json.loads(last_scrape.details)
                    status['last_scrape'] = {
                        'timestamp': last_scrape.timestamp.isoformat(),
                        'user': last_scrape.user,
                        'result': last_result
                    }
                except:
                    pass
        finally:
            db_session.close()
        
        return jsonify({'success': True, 'status': status})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/email/unread', methods=['GET'])
@login_required
def get_unread_emails():
    """Get all unread matched emails (not deleted)"""
    try:
        db_session = get_session()
        try:
            # Get unread emails that are matched to a submission
            # Try to filter by is_deleted, but if column doesn't exist, just return all unread
            try:
                emails = db_session.query(EmailMessage).filter(
                    EmailMessage.is_read == False,
                    # EmailMessage.submission_id != None,
                    EmailMessage.is_deleted == False
                ).order_by(EmailMessage.received_date.desc()).all()
            except Exception:
                # is_deleted column might not exist yet
                emails = db_session.query(EmailMessage).filter(
                    EmailMessage.is_read == False,
                    # EmailMessage.submission_id != None
                ).order_by(EmailMessage.received_date.desc()).all()
            
            email_list = []
            for email in emails:
                email_dict = email.to_dict()
                # Get submission info
                if email.submission_id:
                    submission = db_session.query(Submission).filter_by(id=email.submission_id).first()
                    if submission:
                        email_dict['submission_name'] = submission.insured_name
                email_list.append(email_dict)
            
            return jsonify({
                'success': True,
                'emails': email_list,
                'count': len(email_list)
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/email/<int:email_id>/read', methods=['PUT'])
@login_required
def mark_email_read(email_id):
    """Mark an email as read"""
    try:
        db_session = get_session()
        try:
            email = db_session.query(EmailMessage).filter_by(id=email_id).first()
            if not email:
                return jsonify({'success': False, 'error': 'Email not found'}), 404
            
            email.is_read = True
            db_session.commit()
            
            return jsonify({'success': True})
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/email/<int:email_id>', methods=['DELETE'])
@login_required
def delete_email(email_id):
    """Legacy endpoint — kept for backwards compatibility"""
    return jsonify({'success': True})


@bp.route('/api/email/dismiss', methods=['POST'])
@login_required
def dismiss_email():
    """
    Dismiss an email by marking it as read in the provider.
    Stateless — no database interaction for email storage.
    
    Request body:
    {
        "message_id": str,
        "account_id": int,
        "provider": str
    }
    """
    try:
        data = request.get_json()
        message_id = data.get('message_id')
        account_id = data.get('account_id')
        provider = data.get('provider')
        
        if not all([message_id, account_id, provider]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400
        
        print(f"Dismissing email: message_id={message_id}, account_id={account_id}, provider={provider}")
        
        db_session = get_session()
        try:
            account = db_session.query(ConnectedAccount).filter_by(id=account_id).first()
            if not account:
                return jsonify({'success': False, 'error': 'Connected account not found'}), 404
            
            from app.oauth_services import get_oauth_service
            oauth_service = get_oauth_service(provider, current_app.config)
            
            tokens = account.get_decrypted_tokens()
            access_token = tokens.get('access_token')
            
            # Auto-refresh if expired
            if not access_token or (account.expires_at and account.expires_at < datetime.utcnow()):
                from datetime import timedelta
                refresh_token = tokens.get('refresh_token')
                if not refresh_token:
                    return jsonify({'success': False, 'error': 'Token expired'}), 401
                new_tokens = oauth_service.refresh_access_token(refresh_token)
                account.set_encrypted_tokens(new_tokens)
                account.expires_at = datetime.utcnow() + timedelta(seconds=new_tokens.get('expires_in', 3600))
                db_session.commit()
                access_token = new_tokens.get('access_token')
            
            # Mark as read in provider
            oauth_service.mark_as_read(access_token, message_id)
            
            return jsonify({'success': True})
        finally:
            db_session.close()
    except Exception as e:
        logger.error(f"Email dismiss error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/email/draft_reply', methods=['POST'])
@login_required
def draft_reply():
    """
    Use AI to draft a reply to an email based on what the sender asked for.

    Request body:
    {
        "from_email": str,
        "from_name": str,
        "subject": str,
        "body_text": str
    }
    """
    try:
        data = request.get_json()
        from_name = data.get('from_name', '') or ''
        subject = data.get('subject', '') or ''
        body_text = data.get('body_text', '') or ''

        if not body_text.strip():
            return jsonify({'success': True, 'draft_body': ''})

        # Get user's signature
        db_session = get_session()
        try:
            user = db_session.query(User).filter_by(id=session.get('user_id')).first()
            signature = (user.signature or '').strip() if user else ''
        finally:
            db_session.close()

        # Cap input
        truncated = body_text[:20000]
        sender = from_name or data.get('from_email', 'the sender')

        prompt = f"""You are drafting a brief, professional email reply for an insurance brokerage employee.

The sender ({sender}) wrote the following email:
Subject: {subject}

Body:
{truncated}

Write a short, helpful reply that:
- Addresses what the sender asked for or mentioned
- Is professional but not overly formal
- Does NOT include a greeting line (no "Hi Robert,") — the user will add their own
- Does NOT include a sign-off or signature — that will be appended automatically
- Is 1-4 sentences max
- IMPORTANT: Assume the user IS attaching the requested documents to this reply RIGHT NOW. Do NOT say "I'll get those over shortly" or "I'll send those soon." Instead say "please see attached" or "attached as requested" or similar. The documents are being sent with this email.

Return ONLY the reply body text. No JSON, no quotes, no markdown.
"""

        try:
            from app.parsers.two_pass_parser import get_llm_client
            client = get_llm_client()
            # Use generate_json but we just want raw text — wrap/unwrap
            response = client.generate_json(f'{prompt}\n\nReturn as JSON: {{"reply": "your reply text"}}')
            draft = (response.get('reply') or '').strip()
        except Exception as e:
            logger.warning(f"AI draft reply failed: {e}")
            draft = ''

        # Append signature if available
        if draft and signature:
            draft = f"{draft}\n\n{signature}"

        return jsonify({'success': True, 'draft_body': draft})
    except Exception as e:
        logger.error(f"Draft reply error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/email/send_reply', methods=['POST'])
@login_required
def send_reply():
    """
    Send a reply email via OAuth, optionally with attachments.

    Request body:
    {
        "to_email": str,
        "subject": str,
        "body": str,
        "attachments": [{"filename": str, "content_base64": str, "content_type": str}, ...]
    }
    """
    try:
        data = request.get_json()
        to_email = data.get('to_email', '').strip()
        subject = data.get('subject', '').strip()
        body = data.get('body', '').strip()
        attachments = data.get('attachments') or None

        if not to_email or not body:
            return jsonify({'success': False, 'error': 'Recipient and body are required'}), 400

        _send_email_via_oauth(
            to_email=to_email,
            subject=subject,
            body=body,
            documents=None,
            raw_attachments=attachments
        )

        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Send reply error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/email/save_reply_attachments', methods=['POST'])
@login_required
def save_reply_attachments():
    """
    Save outgoing reply attachments as documents on a submission.

    Request body:
    {
        "submission_id": int,
        "attachments": [{"filename": str, "content_base64": str, "content_type": str}, ...]
    }
    """
    try:
        data = request.get_json()
        submission_id = data.get('submission_id')
        attachments = data.get('attachments', [])

        if not submission_id or not attachments:
            return jsonify({'success': False, 'error': 'submission_id and attachments required'}), 400

        db_session = get_session()
        try:
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            insured_name = submission.insured_name or 'unknown_insured'
            saved = []

            for att in attachments:
                filename = att.get('filename', 'attachment')
                content_base64 = att.get('content_base64', '')
                content_type = att.get('content_type', 'application/octet-stream')

                if not content_base64:
                    continue

                import base64 as b64mod
                file_data = b64mod.b64decode(content_base64)

                safe_filename = secure_filename(filename)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                unique_filename = f"{timestamp}_{safe_filename}"
                filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)

                with open(filepath, 'wb') as f:
                    f.write(file_data)

                # Determine document type from extension
                ext = os.path.splitext(filename)[1].lower()
                if ext == '.pdf':
                    doc_type = DocumentType.APPLICATION
                elif ext in ('.xlsx', '.xls'):
                    doc_type = DocumentType.SOV
                else:
                    doc_type = DocumentType.APPLICATION

                doc_key = _build_storage_key(
                    submission_id, doc_type.name, unique_filename,
                    session.get('user_id'), insured_name
                )
                storage_provider_val, storage_key = _storage_upload(filepath, doc_key, content_type)

                doc = Document(
                    submission_id=submission_id,
                    quote_id=None,
                    document_type=doc_type,
                    carrier=session.get('username', 'user'),
                    term_key=submission.effective_date or datetime.now().strftime('%Y-%m-%d'),
                    version=1,
                    is_active=True,
                    storage_provider=storage_provider_val,
                    storage_key=storage_key,
                    original_filename=safe_filename,
                    content_type=content_type,
                    size_bytes=len(file_data),
                    uploaded_by=session.get('username')
                )
                db_session.add(doc)
                saved.append(safe_filename)

                # Clean up temp file
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except Exception:
                    pass

            db_session.commit()

            return jsonify({
                'success': True,
                'message': f'Saved {len(saved)} attachment(s) to submission.',
                'saved_files': saved
            })
        finally:
            db_session.close()
    except Exception as e:
        logger.error(f"Save reply attachments error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/email/process_attachment', methods=['POST'])
@login_required
def process_single_attachment():
    """
    Process a single email attachment — either parse as quote or save as document.

    Request body:
    {
        "submission_id": int,
        "message_id": str,
        "account_id": int,
        "provider": str,
        "attachment_id": str,
        "filename": str,
        "action_type": "parse" | "save"
    }
    """
    try:
        data = request.get_json()
        submission_id = data.get('submission_id')
        message_id = data.get('message_id')
        account_id = data.get('account_id')
        provider = data.get('provider')
        attachment_id = data.get('attachment_id')
        filename = data.get('filename', 'attachment.pdf')
        action_type = data.get('action_type', 'parse')

        if not all([submission_id, message_id, account_id, provider, attachment_id]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        db_session = get_session()
        try:
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            account = db_session.query(ConnectedAccount).filter_by(id=account_id).first()
            if not account:
                return jsonify({'success': False, 'error': 'Connected account not found'}), 404

            from app.oauth_services import get_oauth_service
            oauth_service = get_oauth_service(provider, current_app.config)

            tokens = account.get_decrypted_tokens()
            access_token = tokens.get('access_token')

            # Auto-refresh if expired
            if not access_token or (account.expires_at and account.expires_at < datetime.utcnow()):
                refresh_token = tokens.get('refresh_token')
                if not refresh_token:
                    return jsonify({'success': False, 'error': 'Token expired, please reconnect email'}), 401
                new_tokens = oauth_service.refresh_access_token(refresh_token)
                account.set_encrypted_tokens(new_tokens)
                account.expires_at = datetime.utcnow() + timedelta(seconds=new_tokens.get('expires_in', 3600))
                db_session.commit()
                access_token = new_tokens.get('access_token')

            # Download the attachment
            attachment_data = oauth_service.fetch_attachments(
                access_token=access_token,
                message_id=message_id,
                attachment_id=attachment_id
            )

            if not attachment_data:
                return jsonify({'success': False, 'error': f'Failed to download attachment: {filename}'}), 500

            safe_filename = secure_filename(filename)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            unique_filename = f"{timestamp}_{safe_filename}"
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)

            with open(filepath, 'wb') as f:
                f.write(attachment_data)

            insured_name = submission.insured_name or 'unknown_insured'

            if action_type == 'parse':
                # Parse as quote
                try:
                    three_pass_result = process_quote_two_pass(filepath, [])
                    layout_data = three_pass_result['pass1_layout']
                    parsed_data = three_pass_result['pass2_normalized']

                    carrier_name = None
                    effective_date = None
                    if parsed_data.get('policies') and len(parsed_data['policies']) > 0:
                        first_policy = parsed_data['policies'][0]
                        carrier_name = first_policy.get('carrier')
                        effective_date = first_policy.get('effective_date')

                    if not effective_date:
                        effective_date = submission.effective_date

                    quote = Quote(
                        submission_id=submission_id,
                        carrier_name=carrier_name,
                        raw_document_path=filepath,
                        extracted_json=json.dumps(parsed_data),
                        pass1_layout_json=json.dumps(layout_data),
                        status=QuoteStatus.RECEIVED
                    )
                    db_session.add(quote)
                    db_session.flush()
                    quote_id = quote.id

                    # Save to documents table
                    content_type = 'application/pdf'
                    quote_doc_key = _build_storage_key(
                        submission_id, DocumentType.QUOTE.name, safe_filename,
                        session.get('user_id'), insured_name
                    )
                    storage_provider_val, storage_key = _storage_upload(filepath, quote_doc_key, content_type)
                    doc = Document(
                        submission_id=submission_id,
                        quote_id=quote_id,
                        document_type=DocumentType.QUOTE,
                        carrier=carrier_name,
                        term_key=effective_date,
                        version=1,
                        is_active=True,
                        storage_provider=storage_provider_val,
                        storage_key=storage_key,
                        original_filename=safe_filename,
                        content_type=content_type,
                        size_bytes=len(attachment_data),
                        uploaded_by=session.get('username')
                    )
                    db_session.add(doc)
                    db_session.commit()

                    # Auto-move to quoting if still in submission stage
                    if submission.status == SubmissionStatus.RECEIVED:
                        submission.status = SubmissionStatus.IN_PROGRESS
                        db_session.commit()

                    return jsonify({'success': True, 'message': f'Quote parsed from "{filename}" and added to submission.'})
                except Exception as parse_err:
                    logger.error(f"Failed to parse attachment as quote: {parse_err}", exc_info=True)
                    return jsonify({'success': False, 'error': f'Failed to parse quote: {str(parse_err)}'}), 500

            else:
                # Save as document (no parsing)
                content_type = 'application/pdf' if filename.lower().endswith('.pdf') else 'application/octet-stream'
                doc_key = _build_storage_key(
                    submission_id, DocumentType.OTHER.name, safe_filename,
                    session.get('user_id'), insured_name
                )
                storage_provider_val, storage_key = _storage_upload(filepath, doc_key, content_type)
                doc = Document(
                    submission_id=submission_id,
                    quote_id=None,
                    document_type=DocumentType.OTHER,
                    carrier=None,
                    term_key=submission.effective_date,
                    version=1,
                    is_active=True,
                    storage_provider=storage_provider_val,
                    storage_key=storage_key,
                    original_filename=safe_filename,
                    content_type=content_type,
                    size_bytes=len(attachment_data),
                    uploaded_by=session.get('username')
                )
                db_session.add(doc)
                db_session.commit()

                return jsonify({'success': True, 'message': f'"{filename}" saved to submission documents.'})

        finally:
            db_session.close()
            # Clean up temp file
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Process attachment error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def _download_attachment_on_demand(attachment: EmailAttachment, email: EmailMessage, db_session: Session) -> Optional[str]:
    """
    Download email attachment on-demand if not already downloaded.

    This implements lazy attachment loading - attachments are stored as metadata during
    email scraping, then downloaded only when the user clicks "Ingest Quote".

    Uses OAuth (Gmail/Outlook) with stored access_token and automatic refresh.

    Downloads the file, uploads to configured storage (S3 or local), and returns
    a local file path for processing. On S3, the file is downloaded to a temp path.

    Returns the local file path or None if download fails.
    """
    # If already stored in S3, download to a temp local path for processing
    if attachment.storage_provider == 's3' and attachment.storage_key:
        try:
            import boto3
            import tempfile
            bucket = current_app.config.get('S3_BUCKET')
            client = boto3.client(
                's3',
                region_name=current_app.config.get('S3_REGION') or None,
                endpoint_url=current_app.config.get('S3_ENDPOINT_URL') or None
            )
            # Download from S3 to a temp file for processing
            temp_dir = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), 'temp_email_attachments')
            os.makedirs(temp_dir, exist_ok=True)
            local_path = os.path.join(temp_dir, f"{attachment.id}_{attachment.filename}")
            client.download_file(bucket, attachment.storage_key, local_path)
            logger.info(f"Downloaded attachment {attachment.filename} from S3 key {attachment.storage_key}")
            return local_path
        except Exception as e:
            logger.error(f"Failed to download attachment from S3: {e}")
            return None

    # If already downloaded locally and file exists, return it
    if attachment.file_path and os.path.exists(attachment.file_path):
        logger.info(f"Attachment {attachment.filename} already downloaded at {attachment.file_path}")
        return attachment.file_path

    logger.info(f"Downloading attachment {attachment.filename} on-demand...")

    # Create temp directory for the download
    temp_dir = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'uploads'), 'temp_email_attachments')
    os.makedirs(temp_dir, exist_ok=True)
    file_path = os.path.join(temp_dir, f"{attachment.id}_{attachment.filename}")

    try:
        file_content = None

        # Download via OAuth (connected account)
        if email.connected_account_id:
            # OAuth path (Gmail or Outlook)
            account = db_session.query(ConnectedAccount).filter_by(id=email.connected_account_id).first()
            if not account:
                logger.error(f"Connected account {email.connected_account_id} not found")
                return None

            # Get OAuth service
            service = get_oauth_service(account.provider.value, current_app.config)

            # Get tokens and check if refresh needed
            tokens = account.get_decrypted_tokens()
            access_token = tokens.get('access_token')

            # Auto-refresh token if expired
            if account.expires_at and account.expires_at < datetime.utcnow():
                logger.info(f"Access token expired, refreshing...")
                try:
                    new_tokens = service.refresh_access_token(tokens.get('refresh_token'))
                    account.set_encrypted_tokens(new_tokens)
                    db_session.commit()
                    access_token = new_tokens.get('access_token')
                    logger.info(f"Token refreshed successfully")
                except Exception as e:
                    logger.error(f"Failed to refresh token: {e}")
                    account.status = ConnectedAccountStatus.ERROR
                    account.last_error = str(e)
                    db_session.commit()
                    return None

            # Download attachment using OAuth API
            file_content = service.fetch_attachments(
                access_token=access_token,
                message_id=attachment.message_id,
                attachment_id=attachment.attachment_id
            )

            if not file_content:
                logger.error(f"Failed to download OAuth attachment {attachment.filename}")
                return None

        else:
            # No connected account — this shouldn't happen for new emails (all use OAuth now)
            logger.error(f"Attachment {attachment.id} has no connected_account_id — cannot download (legacy IMAP email?)")
            return None

        # We have the file content - save locally then upload to storage
        with open(file_path, 'wb') as f:
            f.write(file_content)

        # Upload to configured storage (S3 in prod, local in dev)
        from app.database import get_current_tenant
        tenant = get_current_tenant() or 'default'
        safe_filename = secure_filename(attachment.filename) or 'attachment.bin'
        storage_object_key = f"{tenant}/email_attachments/{email.id}/{safe_filename}"

        storage_provider_val, storage_key = _storage_upload(
            file_path, storage_object_key, attachment.content_type
        )

        # Update database with storage info
        attachment.storage_provider = storage_provider_val
        attachment.storage_key = storage_key
        attachment.file_path = file_path  # Keep local path as fallback
        db_session.commit()

        logger.info(f"Downloaded attachment {attachment.filename} → {storage_provider_val}:{storage_key}")
        return file_path

    except Exception as e:
        logger.error(f"Error downloading attachment on-demand: {e}")
        import traceback
        traceback.print_exc()
        return None


@bp.route('/api/email/<int:email_id>/ingest_quote/<int:submission_id>', methods=['POST'])
@login_required
def ingest_quote_to_submission(email_id, submission_id):
    """Legacy endpoint — redirects to stateless ingest"""
    return jsonify({'success': False, 'error': 'Use /api/email/ingest_quote instead'}), 410


@bp.route('/api/email/ingest_quote', methods=['POST'])
@login_required
def ingest_quote_stateless():
    """
    Unified email ingest endpoint.

    Always saves the email body as a cleaned correspondence document on the
    selected submission. If quote-style attachments (PDF/Excel/Word) are
    present, also parses them and creates Quote records.

    Body must be cleaned with AI to strip signatures, footers, and boilerplate
    while preserving thread structure.

    Request body:
    {
        "submission_id": int,
        "message_id": str,        # provider message ID
        "account_id": int,        # ConnectedAccount ID
        "provider": str,          # "gmail" or "outlook"
        "from_name": str,
        "from_email": str,
        "subject": str,
        "body_text": str,
        "received_date": str,
        "attachments": [
            {"attachment_id": str, "filename": str, "content_type": str, "size": int}
        ]
    }
    """
    try:
        data = request.get_json()
        submission_id = data.get('submission_id')
        message_id = data.get('message_id')
        account_id = data.get('account_id')
        provider = data.get('provider')
        attachments_to_ingest = data.get('attachments', []) or []
        from_name = data.get('from_name', '') or ''
        from_email = data.get('from_email', '') or ''
        subject = data.get('subject', '(No subject)') or '(No subject)'
        body_text = data.get('body_text', '') or ''
        received_date = data.get('received_date', '') or ''

        if not all([submission_id, message_id, account_id, provider]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        # Identify quote-style attachments (parser-eligible)
        quote_extensions = ('.pdf', '.xlsx', '.xls', '.docx', '.doc')
        quote_attachments = [a for a in attachments_to_ingest if (a.get('filename') or '').lower().endswith(quote_extensions)]

        db_session = get_session()
        try:
            # Verify submission exists
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            # Get connected account for OAuth credentials
            account = db_session.query(ConnectedAccount).filter_by(id=account_id).first()
            if not account:
                return jsonify({'success': False, 'error': 'Connected account not found'}), 404

            # Get OAuth service and access token
            from app.oauth_services import get_oauth_service
            oauth_service = get_oauth_service(provider, current_app.config)

            tokens = account.get_decrypted_tokens()
            access_token = tokens.get('access_token')

            # Auto-refresh if expired
            if not access_token or (account.expires_at and account.expires_at < datetime.utcnow()):
                from datetime import timedelta
                refresh_token = tokens.get('refresh_token')
                if not refresh_token:
                    return jsonify({'success': False, 'error': 'Token expired, please reconnect email'}), 401
                new_tokens = oauth_service.refresh_access_token(refresh_token)
                account.set_encrypted_tokens(new_tokens)
                account.expires_at = datetime.utcnow() + timedelta(seconds=new_tokens.get('expires_in', 3600))
                db_session.commit()
                access_token = new_tokens.get('access_token')

            insured_name = submission.insured_name or 'unknown_insured'
            sender_label = from_name or from_email or 'unknown'

            # ============================================================
            # 1. Always save the email as cleaned correspondence
            # ============================================================
            ai_result = _clean_email_with_ai(
                subject=subject,
                from_label=sender_label,
                received_date=received_date,
                body_text=body_text
            )
            ai_messages = ai_result.get('messages') or []
            ai_title = ai_result.get('title') or f"Email with {sender_label}"

            # Drop messages with empty bodies — they would render as empty rows
            # and have no informational value beyond the headers.
            ai_messages = [m for m in ai_messages if (m.get('body') or '').strip()]

            correspondence_doc_id = None
            if not ai_messages:
                # Email has no meaningful body content (e.g. cover note for a
                # quote attachment). Skip creating a correspondence document.
                logger.info(f"Skipping correspondence doc for message {message_id}: no substantive body content")
            else:
                try:
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    # Store as JSON so the UI can render each message in its own frame
                    display_filename = f"{ai_title}.json"[:200]
                    unique_filename = f"{timestamp}_correspondence_{submission_id}.json"
                    filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)

                    payload = {
                        'title': ai_title,
                        'outer': {
                            'from': sender_label,
                            'subject': subject,
                            'date': received_date
                        },
                        'messages': ai_messages
                    }
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(payload, f, ensure_ascii=False, indent=2)

                    term_key = submission.effective_date or datetime.now().strftime('%Y-%m-%d')
                    doc_key = _build_storage_key(
                        submission_id, 'CORRESPONDENCE', unique_filename,
                        session.get('user_id'), insured_name
                    )
                    storage_provider_val, storage_key = _storage_upload(filepath, doc_key, 'application/json')

                    size_bytes = os.path.getsize(filepath) if os.path.exists(filepath) else None

                    correspondence_doc = Document(
                        submission_id=submission_id,
                        quote_id=None,
                        document_type=DocumentType.CORRESPONDENCE,
                        carrier=sender_label,
                        term_key=term_key,
                        version=1,
                        is_active=True,
                        storage_provider=storage_provider_val,
                        storage_key=storage_key,
                        original_filename=display_filename,
                        content_type='application/json',
                        size_bytes=size_bytes,
                        uploaded_by=session.get('username')
                    )
                    db_session.add(correspondence_doc)
                    db_session.flush()
                    correspondence_doc_id = correspondence_doc.id

                    db_session.add(AuditLog(
                        entity_type='submission',
                        entity_id=submission_id,
                        action='email_correspondence_added',
                        submission_id=submission_id,
                        user=session.get('username'),
                        details=json.dumps({
                            'message_id': message_id,
                            'subject': subject,
                            'from': sender_label,
                            'title': ai_title,
                            'message_count': len(ai_messages)
                        })
                    ))

                    # Clean up temp file
                    try:
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    except Exception as cleanup_err:
                        logger.warning(f"Could not delete temp correspondence file: {cleanup_err}")
                except Exception as corr_err:
                    logger.error(f"Failed to save cleaned correspondence: {corr_err}", exc_info=True)

            # ============================================================
            # 2. If quote-style attachments are present, parse and ingest them
            # ============================================================
            created_quotes = []
            for att in quote_attachments:
                attachment_id = att.get('attachment_id')
                filename = att.get('filename', 'attachment.pdf')
                content_type = att.get('content_type', 'application/pdf')
                size_bytes = att.get('size', 0)

                # Currently only PDF parsing is implemented
                if not filename.lower().endswith('.pdf'):
                    continue

                attachment_data = oauth_service.fetch_attachments(
                    access_token=access_token,
                    message_id=message_id,
                    attachment_id=attachment_id
                )

                if not attachment_data:
                    logger.warning(f"Failed to download attachment {filename}, skipping")
                    continue

                safe_filename = secure_filename(filename)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                unique_filename = f"{timestamp}_{safe_filename}"
                filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)

                with open(filepath, 'wb') as f:
                    f.write(attachment_data)

                try:
                    three_pass_result = process_quote_two_pass(filepath, [])
                    layout_data = three_pass_result['pass1_layout']
                    parsed_data = three_pass_result['pass2_normalized']

                    carrier_name = None
                    effective_date = None
                    if parsed_data.get('policies') and len(parsed_data['policies']) > 0:
                        first_policy = parsed_data['policies'][0]
                        carrier_name = first_policy.get('carrier')
                        effective_date = first_policy.get('effective_date')

                    if not effective_date:
                        effective_date = submission.effective_date

                    quote = Quote(
                        submission_id=submission_id,
                        carrier_name=carrier_name,
                        raw_document_path=filepath,
                        extracted_json=json.dumps(parsed_data),
                        pass1_layout_json=json.dumps(layout_data),
                        status=QuoteStatus.RECEIVED
                    )
                    db_session.add(quote)
                    db_session.flush()
                    quote_id = quote.id

                    db_session.add(AuditLog(
                        entity_type='quote',
                        entity_id=quote_id,
                        submission_id=submission_id,
                        quote_id=quote_id,
                        action='uploaded',
                        user=session.get('username'),
                        details=f"Uploaded quote from {carrier_name or 'unknown carrier'}"
                    ))

                    quote_doc_key = _build_storage_key(
                        submission_id,
                        DocumentType.QUOTE.name,
                        safe_filename,
                        session.get('user_id'),
                        submission.insured_name
                    )
                    storage_provider, storage_key = _storage_upload(filepath, quote_doc_key, content_type)
                    doc = Document(
                        submission_id=submission_id,
                        quote_id=quote_id,
                        document_type=DocumentType.QUOTE,
                        carrier=carrier_name,
                        term_key=effective_date,
                        version=1,
                        is_active=True,
                        storage_provider=storage_provider,
                        storage_key=storage_key,
                        original_filename=safe_filename,
                        content_type=content_type,
                        size_bytes=size_bytes,
                        uploaded_by=session.get('username')
                    )
                    db_session.add(doc)

                    created_quotes.append({
                        'quote_id': quote_id,
                        'filename': safe_filename,
                        'carrier': carrier_name
                    })

                    db_session.add(AuditLog(
                        entity_type='quote',
                        entity_id=quote_id,
                        action='email_quote_ingested',
                        submission_id=submission_id,
                        quote_id=quote_id,
                        details=json.dumps({'message_id': message_id, 'filename': safe_filename})
                    ))
                except Exception as e:
                    logger.error(f"Error processing quote {filename}: {e}", exc_info=True)
                    continue
                finally:
                    try:
                        if os.path.exists(filepath):
                            os.remove(filepath)
                    except Exception:
                        pass

            # ============================================================
            # 3. Move submission to quoting stage if a quote was ingested
            # ============================================================
            if created_quotes and submission.status != SubmissionStatus.IN_PROGRESS:
                previous_status = submission.status
                submission.status = SubmissionStatus.IN_PROGRESS
                db_session.add(AuditLog(
                    entity_type='submission',
                    entity_id=submission_id,
                    submission_id=submission_id,
                    action='status_changed',
                    user=session.get('username'),
                    details=f"Status changed from {previous_status.value if previous_status else 'unknown'} to In Progress (quote ingested from email)"
                ))

            db_session.commit()

            # Mark email as read in the provider
            try:
                oauth_service.mark_as_read(access_token, message_id)
            except Exception as e:
                logger.warning(f"Failed to mark email as read in provider: {e}")

            return jsonify({
                'success': True,
                'quotes': created_quotes,
                'correspondence_document_id': correspondence_doc_id,
                'correspondence_title': ai_title,
                'submission_id': submission_id
            })
        finally:
            db_session.close()
    except Exception as e:
        logger.error(f"Email ingest error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def _clean_email_with_ai(subject: str, from_label: str, received_date: str, body_text: str) -> dict:
    """
    Use the configured LLM to:
      1. Split a forwarded/replied email thread into individual messages.
      2. Strip signatures, footers, image placeholders, and boilerplate from
         each message body.
      3. Generate a short title for the whole thread.

    Returns:
      {
        "title": str,
        "messages": [
          {"from": str, "to": str, "cc": str, "subject": str, "date": str, "body": str},
          ...
        ]
      }

    On failure, returns a single-message structure with the raw body.
    """
    fallback_title = f"Email with {from_label}" if from_label else "Email correspondence"
    fallback = {
        'title': fallback_title,
        'messages': [{
            'from': from_label or '',
            'to': '',
            'cc': '',
            'subject': subject or '',
            'date': received_date or '',
            'body': (body_text or '').strip()
        }]
    }
    if not body_text or not body_text.strip():
        return fallback

    MAX_CHARS = 40000
    truncated_body = body_text[:MAX_CHARS]

    prompt = f"""You are processing an email thread for an insurance brokerage's correspondence log.

Outer (most recent) message metadata:
- Subject: {subject}
- From: {from_label}
- Received: {received_date}

The body below may contain a forwarded/replied thread with multiple distinct messages. Each new message in the thread typically starts with a header like:
  From: ...
  Sent: ... (or Date: ...)
  To: ...
  Cc: ...
  Subject: ...
followed by that message's body.

Your job:

1. SPLIT the thread into individual messages. The OUTER (top, most recent) message uses the metadata above. Each subsequent message uses its own header.

2. For EACH message body:
   - Strip ALL signatures and contact blocks (phone, address, title, website, social links).
   - Strip ALL boilerplate footers: confidentiality notices, virus disclaimers, "EXTERNAL EMAIL" warnings, Mimecast notices, "Sent from my iPhone", legal disclaimers.
   - Strip image placeholders like <image001.png>, <~WRD123.jpg>, [cid:...], inline image references.
   - Replace attachment references like <SomeFile.pdf> with [Attachment: SomeFile.pdf].
   - Collapse runs of blank lines to a single blank line.
   - Output plain text only — no markdown, no asterisks.
   - Do NOT summarize. Keep the actual content as written.

3. Order messages chronologically OLDEST FIRST.

4. Generate a short TOPIC PHRASE (3-6 words) describing what the thread is ABOUT — the actual subject matter or action being discussed. Lowercase except proper nouns. No leading "Re:" or "Fwd:".

   STRICT RULES — DO NOT include in the topic:
   - The insured/client name (the user already knows which submission they are looking at).
   - Person names (sender, recipient, anyone).
   - Email addresses.
   - The literal email subject line.
   - Generic words like "email", "thread", "correspondence", "discussion", "regarding".

   The topic should describe the topic, not name the parties or the insured.

   Good examples:
   - "finance agreement restructuring"
   - "binding subjectivities outstanding"
   - "loss runs requested"
   - "premium amendment for auto"
   - "revised auto quote"
   - "down payment percentage change"

   Bad examples (do NOT do these):
   - "LTR Holdings Wolf Disposals finance agreement"   (includes insured name)
   - "Mitzie requesting finance agreement"             (includes person name)
   - "Re: LTR Holdings Proposals"                      (echoes subject line)
   - "Email about finance agreement"                   (uses banned word "email")

Return ONLY valid JSON in this exact shape:
{{
  "title": "short descriptive title",
  "messages": [
    {{
      "from": "Name <email@example.com>",
      "to": "recipients",
      "cc": "cc list or empty string",
      "subject": "subject line",
      "date": "as written in the header",
      "body": "the cleaned message body"
    }}
  ]
}}

If the thread contains only one message, return a single-element array.
If you cannot identify clear message boundaries, return one message with the entire cleaned body.

Email thread to process:
---
{truncated_body}
---
"""

    try:
        from app.parsers.two_pass_parser import get_llm_client
        client = get_llm_client()
        result = client.generate_json(prompt)
        title = (result.get('title') or '').strip() or fallback_title
        messages = result.get('messages') or []

        # Sanitize each message
        cleaned_messages = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            cleaned_messages.append({
                'from': (m.get('from') or '').strip(),
                'to': (m.get('to') or '').strip(),
                'cc': (m.get('cc') or '').strip(),
                'subject': (m.get('subject') or '').strip(),
                'date': (m.get('date') or '').strip(),
                'body': (m.get('body') or '').strip()
            })

        if not cleaned_messages:
            return fallback

        return {'title': title, 'messages': cleaned_messages}
    except Exception as e:
        logger.warning(f"AI email cleaning failed, using raw body: {e}")
        return fallback


@bp.route('/api/email/<int:email_id>/add_correspondence/<int:submission_id>', methods=['POST'])
@login_required
def add_email_correspondence(email_id, submission_id):
    """Add email body as correspondence document to a submission"""
    try:
        db_session = get_session()
        try:
            # Get the email
            email = db_session.query(EmailMessage).filter_by(id=email_id).first()
            if not email:
                return jsonify({'success': False, 'error': 'Email not found'}), 404
            
            # Get the submission
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            insured_name = submission.insured_name if submission and submission.insured_name else 'unknown_insured'
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404
            
            # Create a text file with the email content
            filename = f"email_correspondence_{email_id}.txt"
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            unique_filename = f"{timestamp}_{filename}"
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)
            
            # Build email content
            email_content = f"""From: {email.from_name or email.from_email}
                                To: {email.to_email}
                                Subject: {email.subject}
                                Date: {email.received_date}

                                ---

                                {email.body_text or '(No text content)'}
                                """
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(email_content)
            
            # Create document record
            term_key = submission.effective_date or datetime.now().strftime('%Y-%m-%d')
            doc_key = _build_storage_key(submission_id, 'CORRESPONDENCE', filename, session.get('user_id'),insured_name)
            storage_provider, storage_key = _storage_upload(filepath, doc_key, 'text/plain')
            
            doc = Document(
                submission_id=submission_id,
                quote_id=None,
                document_type=DocumentType.OTHER,
                carrier=None,
                term_key=term_key,
                version=1,
                is_active=True,
                storage_provider=storage_provider,
                storage_key=storage_key,
                original_filename=filename,
                content_type='text/plain',
                size_bytes=os.path.getsize(filepath) if os.path.exists(filepath) else None,
                uploaded_by=session.get('username')
            )
            db_session.add(doc)
            
            # Mark email as read
            email.is_read = True
            db_session.commit()
            # Clean up temp file after processing
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception as e:
                print(f"Warning: Could not delete temp file {filepath}: {e}")
            # Log action
            log_action(
                entity_type='submission',
                entity_id=submission_id,
                action='email_correspondence_added',
                submission_id=submission_id,
                details=json.dumps({'email_id': email_id, 'subject': email.subject})
            )
            
            return jsonify({
                'success': True,
                'document_id': doc.id,
                'submission_id': submission_id
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/email/save_correspondence', methods=['POST'])
@login_required
def save_email_correspondence_stateless():
    """
    Save email as correspondence document to a submission.
    Uses AI to split threads and clean signatures/boilerplate.
    Stores as JSON so the viewer can render each message separately.
    Marks the email as read in the provider after saving.
    
    Request body:
    {
        "submission_id": int,
        "message_id": str,
        "account_id": int,
        "provider": str,
        "from_name": str,
        "from_email": str,
        "subject": str,
        "body_text": str,
        "received_date": str (ISO format)
    }
    """
    try:
        data = request.get_json()
        submission_id = data.get('submission_id')
        message_id = data.get('message_id')
        account_id = data.get('account_id')
        provider = data.get('provider')
        from_name = data.get('from_name', '')
        from_email = data.get('from_email', '')
        subject = data.get('subject', '(No subject)')
        body_text = data.get('body_text', '(No text content)')
        received_date = data.get('received_date', '')

        if not all([submission_id, message_id, account_id, provider]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        db_session = get_session()
        try:
            # Verify submission exists
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            insured_name = submission.insured_name or 'unknown_insured'
            sender_label = from_name or from_email or 'unknown'

            # Use AI to split thread and clean signatures/boilerplate
            ai_result = _clean_email_with_ai(
                subject=subject,
                from_label=sender_label,
                received_date=received_date,
                body_text=body_text
            )
            ai_messages = ai_result.get('messages') or []
            ai_title = ai_result.get('title') or f"Email with {sender_label}"

            # Drop messages with empty bodies
            ai_messages = [m for m in ai_messages if (m.get('body') or '').strip()]

            if not ai_messages:
                return jsonify({'success': False, 'error': 'Email has no meaningful body content to save.'}), 400

            # Store as JSON so the UI can render each message in its own frame
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            display_filename = f"{ai_title}.json"[:200]
            unique_filename = f"{timestamp}_correspondence_{submission_id}.json"
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)

            payload = {
                'title': ai_title,
                'outer': {
                    'from': sender_label,
                    'subject': subject,
                    'date': received_date
                },
                'messages': ai_messages
            }
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            # Upload to storage
            term_key = submission.effective_date or datetime.now().strftime('%Y-%m-%d')
            doc_key = _build_storage_key(submission_id, 'CORRESPONDENCE', unique_filename, session.get('user_id'), insured_name)
            storage_provider_val, storage_key = _storage_upload(filepath, doc_key, 'application/json')

            size_bytes = os.path.getsize(filepath) if os.path.exists(filepath) else None

            doc = Document(
                submission_id=submission_id,
                quote_id=None,
                document_type=DocumentType.CORRESPONDENCE,
                carrier=sender_label,
                term_key=term_key,
                version=1,
                is_active=True,
                storage_provider=storage_provider_val,
                storage_key=storage_key,
                original_filename=display_filename,
                content_type='application/json',
                size_bytes=size_bytes,
                uploaded_by=session.get('username')
            )
            db_session.add(doc)
            db_session.flush()
            doc_id = doc.id

            db_session.add(AuditLog(
                entity_type='submission',
                entity_id=submission_id,
                action='email_correspondence_added',
                submission_id=submission_id,
                user=session.get('username'),
                details=json.dumps({
                    'message_id': message_id,
                    'subject': subject,
                    'from': sender_label,
                    'title': ai_title,
                    'message_count': len(ai_messages)
                })
            ))

            db_session.commit()

            # Clean up temp file
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception as e:
                logger.warning(f"Could not delete temp file {filepath}: {e}")

            # Mark email as read in provider
            try:
                account = db_session.query(ConnectedAccount).filter_by(id=account_id).first()
                if account:
                    from app.oauth_services import get_oauth_service
                    oauth_service = get_oauth_service(provider, current_app.config)
                    tokens = account.get_decrypted_tokens()
                    access_token = tokens.get('access_token')

                    if not access_token or (account.expires_at and account.expires_at < datetime.utcnow()):
                        from datetime import timedelta
                        refresh_token = tokens.get('refresh_token')
                        if refresh_token:
                            new_tokens = oauth_service.refresh_access_token(refresh_token)
                            account.set_encrypted_tokens(new_tokens)
                            account.expires_at = datetime.utcnow() + timedelta(seconds=new_tokens.get('expires_in', 3600))
                            db_session.commit()
                            access_token = new_tokens.get('access_token')

                    if access_token:
                        oauth_service.mark_as_read(access_token, message_id)
            except Exception as e:
                logger.warning(f"Failed to mark email as read in provider: {e}")

            return jsonify({
                'success': True,
                'document_id': doc_id,
                'submission_id': submission_id,
                'message': f'Email saved as correspondence ({len(ai_messages)} message{"s" if len(ai_messages) != 1 else ""}).'
            })
        finally:
            db_session.close()
    except Exception as e:
        logger.error(f"Save correspondence error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# FILTERED EMAIL SEARCH & SMART TRIAGE
# ============================================================================

@bp.route('/api/email/search', methods=['POST'])
@login_required
def filtered_email_search():
    """
    Search emails with user-configurable filters via MS Graph API (or Gmail).

    Default filters are the user's brokers and insured names.
    User can adjust: brokers, extra addresses, insured names, subjects,
    has_attachments, and how far back to look.

    Request body:
    {
        "broker_emails": [str] | null,       # null = use all user brokers
        "extra_addresses": [str],            # additional from-addresses to include
        "insured_names": [str] | null,       # null = use all user insured names
        "subjects": [str],                   # additional subject keywords
        "has_attachments": bool,             # default true
        "days_back": int,                    # how far back to look (default 24)
        "remove_all_filters": bool           # if true, fetch all unread emails
    }
    """
    try:
        if not current_app.config.get('EMAIL_SCRAPING_ENABLED', False):
            return jsonify({'success': False, 'error': 'Email scraping is disabled'}), 400

        user_id = session.get('user_id')
        data = request.get_json() or {}

        db_session = get_session()
        try:
            # Get connected OAuth account
            account = db_session.query(ConnectedAccount).filter(
                ConnectedAccount.user_id == user_id,
                ConnectedAccount.status == ConnectedAccountStatus.ACTIVE
            ).first()

            if not account:
                return jsonify({'success': False, 'needs_connect': True,
                                'error': 'No email account connected.'}), 400

            # Get tokens, auto-refresh if needed
            from app.oauth_services import get_oauth_service
            provider_str = account.provider.value.lower()
            oauth_service = get_oauth_service(provider_str, current_app.config)

            tokens = account.get_decrypted_tokens()
            access_token = tokens.get('access_token')

            if not access_token or (account.expires_at and account.expires_at < datetime.utcnow()):
                refresh_token = tokens.get('refresh_token')
                if not refresh_token:
                    return jsonify({'success': False, 'needs_reauth': True,
                                    'provider': provider_str}), 401
                new_tokens = oauth_service.refresh_access_token(refresh_token)
                account.set_encrypted_tokens(new_tokens)
                account.expires_at = datetime.utcnow() + timedelta(seconds=new_tokens.get('expires_in', 3600))
                db_session.commit()
                access_token = new_tokens.get('access_token')

            # Build filter lists
            remove_all = data.get('remove_all_filters', False)

            if remove_all:
                broker_emails = []
                quote_subjects = []
            else:
                # Broker emails: use provided list or default to user's brokers
                if data.get('broker_emails') is not None:
                    broker_emails = [e.strip().lower() for e in data['broker_emails'] if e.strip()]
                else:
                    broker_emails = _get_user_broker_emails(db_session, user_id)

                # Extra addresses to include in from-filter
                extra_addresses = [e.strip().lower() for e in data.get('extra_addresses', []) if e.strip()]
                broker_emails.extend(extra_addresses)

                # Insured names for subject search
                if data.get('insured_names') is not None:
                    # User provided specific names — expand variants
                    quote_subjects = []
                    for name in data['insured_names']:
                        if name.strip():
                            quote_subjects.extend(_expand_insured_name_variants(name.strip()))
                else:
                    quote_subjects = _get_user_quote_subjects(db_session, user_id)

                # Extra subject keywords
                extra_subjects = [s.strip() for s in data.get('subjects', []) if s.strip()]
                quote_subjects.extend(extra_subjects)

            # Date range
            days_back = data.get('days_back', 24)
            since_date = datetime.now() - timedelta(days=days_back)

            # Has attachments filter
            has_attachments = data.get('has_attachments', True)

            # Fetch emails via OAuth service
            # Pass require_attachments explicitly so the API knows whether to filter
            unified_emails = oauth_service.fetch_emails(
                access_token=access_token,
                max_results=100,
                since_date=since_date,
                broker_emails=broker_emails if broker_emails else None,
                quote_subjects=quote_subjects if quote_subjects else None,
                require_attachments=has_attachments if has_attachments else False
            )

            if not unified_emails:
                unified_emails = []

            # Filter out self-sent emails
            own_email = (account.email_address or '').strip().lower()
            if own_email:
                unified_emails = [e for e in unified_emails if (e.from_email or '').strip().lower() != own_email]

            # Build response
            email_list = []
            for unified_email in unified_emails:
                email_data = {
                    'message_id': unified_email.message_id,
                    'from_email': unified_email.from_email,
                    'from_name': unified_email.from_name,
                    'subject': unified_email.subject or '(No subject)',
                    'body_text': unified_email.body_text or '',
                    'received_date': unified_email.date.isoformat() if unified_email.date else None,
                    'has_attachments': len(unified_email.attachments) > 0,
                    'attachment_count': len(unified_email.attachments),
                    'provider': provider_str,
                    'account_id': account.id,
                    'attachments': [
                        {
                            'attachment_id': att.get('attachment_id', ''),
                            'filename': att.get('filename', ''),
                            'content_type': att.get('content_type', ''),
                            'size': att.get('size', 0)
                        }
                        for att in unified_email.attachments
                    ]
                }
                email_list.append(email_data)

            return jsonify({
                'success': True,
                'emails': email_list,
                'count': len(email_list),
                'filters_applied': {
                    'broker_emails': broker_emails,
                    'insured_names': quote_subjects[:10] if quote_subjects else [],
                    'has_attachments': has_attachments,
                    'days_back': days_back,
                    'remove_all_filters': remove_all
                }
            })

        finally:
            db_session.close()

    except Exception as e:
        logger.error(f"Filtered email search error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/email/triage_attachment', methods=['POST'])
@login_required
def triage_attachment():
    """
    Smart triage: Download an email attachment, use LLM to classify it
    (application, quote, or other), extract insured name, then route it
    to the appropriate parser and submission.

    Flow:
    1. Download attachment via OAuth
    2. LLM classifies document type + extracts insured name
    3. Based on classification:
       - Application → process_application_two_pass → create new card in Submission stage
       - Quote → process_quote_two_pass → match to existing card or create new in Quoting
       - Other → save document to matched card or create new in Submission stage
    4. If insured name matches multiple submissions → return choices for user to pick
    5. If no insured name found → return error

    Request body:
    {
        "message_id": str,
        "account_id": int,
        "provider": str,
        "attachment_id": str,
        "filename": str,
        "submission_id": int | null,   # If user already picked a submission (from picker)
        "force_type": str | null       # If user wants to override classification
    }
    """
    try:
        data = request.get_json()
        message_id = data.get('message_id')
        account_id = data.get('account_id')
        provider = data.get('provider')
        attachment_id = data.get('attachment_id')
        filename = data.get('filename', 'attachment.pdf')
        user_submission_id = data.get('submission_id')  # User pre-selected
        force_type = data.get('force_type')  # User override

        if not all([message_id, account_id, provider, attachment_id]):
            return jsonify({'success': False, 'error': 'Missing required fields'}), 400

        db_session = get_session()
        try:
            account = db_session.query(ConnectedAccount).filter_by(id=account_id).first()
            if not account:
                return jsonify({'success': False, 'error': 'Connected account not found'}), 404

            # Get access token (with auto-refresh)
            from app.oauth_services import get_oauth_service
            oauth_service = get_oauth_service(provider, current_app.config)
            tokens = account.get_decrypted_tokens()
            access_token = tokens.get('access_token')

            if not access_token or (account.expires_at and account.expires_at < datetime.utcnow()):
                refresh_token = tokens.get('refresh_token')
                if not refresh_token:
                    return jsonify({'success': False, 'error': 'Token expired, please reconnect email'}), 401
                new_tokens = oauth_service.refresh_access_token(refresh_token)
                account.set_encrypted_tokens(new_tokens)
                account.expires_at = datetime.utcnow() + timedelta(seconds=new_tokens.get('expires_in', 3600))
                db_session.commit()
                access_token = new_tokens.get('access_token')

            # Download the attachment
            attachment_data = oauth_service.fetch_attachments(
                access_token=access_token,
                message_id=message_id,
                attachment_id=attachment_id
            )

            if not attachment_data:
                return jsonify({'success': False, 'error': f'Failed to download attachment: {filename}'}), 500

            # Save to temp file
            safe_filename = secure_filename(filename)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            unique_filename = f"{timestamp}_{safe_filename}"
            filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)

            with open(filepath, 'wb') as f:
                f.write(attachment_data)

            try:
                # Step 1: LLM Triage — classify document and extract insured name
                doc_type = force_type
                insured_name = None
                triage_result = None

                if not doc_type:
                    triage_result = _triage_document(filepath)
                    doc_type = triage_result.get('document_type', 'other')
                    insured_name = triage_result.get('insured_name')
                else:
                    # If force_type provided, still need insured name
                    triage_result = _triage_document(filepath)
                    insured_name = triage_result.get('insured_name')

                if not insured_name:
                    # Triage LLM couldn't find insured name from first 2 pages.
                    # Fallback: run the full parser (which does deeper extraction
                    # including vision for scanned PDFs) to get insured name AND
                    # re-determine document type based on what the parser finds.
                    print(f"[TRIAGE] Triage couldn't find insured name (doc_type={doc_type}), running full parser fallback...")
                    try:
                        # Try quote parser first — it handles both digital and scanned PDFs.
                        # If it finds carrier/premium data, it's a quote.
                        fallback_result = process_quote_two_pass(filepath, [])
                        parsed = fallback_result.get('pass2_normalized', {})

                        # Try insured from quote data
                        insured_info = parsed.get('insured') or {}
                        insured_name = insured_info.get('name')

                        # Also check policies for insured_name field
                        if not insured_name and parsed.get('policies'):
                            for policy in parsed['policies']:
                                if policy.get('insured_name'):
                                    insured_name = policy['insured_name']
                                    break

                        # Re-classify: if we found carrier/premium info, it's a quote
                        if parsed.get('policies') and len(parsed['policies']) > 0:
                            first_policy = parsed['policies'][0]
                            has_carrier = bool(first_policy.get('carrier'))
                            has_premium = bool(first_policy.get('total_premium') or first_policy.get('annual_premium'))
                            if has_carrier or has_premium:
                                doc_type = 'quote'
                                print(f"[TRIAGE] Fallback reclassified as QUOTE (carrier={first_policy.get('carrier')})")

                        # If quote parser didn't find insured, try application parser
                        if not insured_name:
                            app_result = process_application_two_pass(filepath)
                            app_parsed = app_result.get('pass2_normalized', {})
                            app_insured = (app_parsed.get('insured') or {}).get('name')
                            if app_insured:
                                insured_name = app_insured
                                # If we haven't already classified as quote, check if
                                # the app parser found coverage_types (implies application)
                                if doc_type not in ('quote',):
                                    coverage_types = (app_parsed.get('submission') or {}).get('coverage_types_needed', [])
                                    if coverage_types:
                                        doc_type = 'application'
                                        print(f"[TRIAGE] Fallback reclassified as APPLICATION")

                        if insured_name:
                            insured_name = insured_name.strip()
                            print(f"[TRIAGE] Full parser found insured name: {insured_name}, final doc_type: {doc_type}")
                    except Exception as fallback_err:
                        print(f"[TRIAGE] Full parser fallback failed: {fallback_err}")

                if not insured_name:
                    return jsonify({
                        'success': False,
                        'error': 'no_insured',
                        'message': 'Could not determine insured name from this document.',
                        'triage': triage_result
                    }), 400

                # Step 2: Find or create submission
                submission_id = user_submission_id
                is_new_submission = False

                if not submission_id:
                    # Try to match to existing submission by insured name
                    matches = _find_matching_submissions(db_session, insured_name, session.get('user_id'))

                    if len(matches) == 0:
                        # No match — create new submission
                        submission_id = create_submission(
                            insured_name=insured_name,
                            effective_date=datetime.now().strftime('%Y-%m-%d'),
                            state=triage_result.get('state') if triage_result else None,
                            user=session.get('username'),
                            assigned_to=session.get('user_id')
                        )
                        is_new_submission = True

                    elif len(matches) == 1:
                        submission_id = matches[0]['id']

                    else:
                        # Multiple matches — return picker data
                        return jsonify({
                            'success': False,
                            'error': 'multiple_matches',
                            'message': f'Found {len(matches)} submissions for "{insured_name}". Please select one.',
                            'matches': matches,
                            'triage': {
                                'document_type': doc_type,
                                'insured_name': insured_name
                            },
                            # Pass back attachment info so frontend can re-call with submission_id
                            'attachment_info': {
                                'message_id': message_id,
                                'account_id': account_id,
                                'provider': provider,
                                'attachment_id': attachment_id,
                                'filename': filename
                            }
                        })

                # Step 3: Route based on document type
                submission = db_session.query(Submission).filter_by(id=submission_id).first()
                if not submission:
                    return jsonify({'success': False, 'error': 'Submission not found after creation'}), 500

                result_data = {
                    'success': True,
                    'submission_id': submission_id,
                    'insured_name': submission.insured_name,
                    'document_type': doc_type,
                    'is_new_submission': is_new_submission,
                    'is_renewal': False
                }

                if doc_type == 'application':
                    # Parse as application
                    application_result = process_application_two_pass(filepath)
                    parsed_data = application_result['pass2_normalized']

                    # Update submission with parsed data if new
                    if is_new_submission:
                        submission_fields = parsed_data.get('submission') or {}
                        eff_date = submission_fields.get('effective_date')
                        if eff_date:
                            submission.effective_date = eff_date
                        state_val = (parsed_data.get('insured') or {}).get('address', {}).get('state')
                        if state_val:
                            submission.state = state_val
                        db_session.commit()

                    # Save document
                    content_type = 'application/pdf'
                    doc_key = _build_storage_key(
                        submission_id, DocumentType.APPLICATION.name, safe_filename,
                        session.get('user_id'), submission.insured_name
                    )
                    storage_provider_val, storage_key = _storage_upload(filepath, doc_key, content_type)
                    doc = Document(
                        submission_id=submission_id,
                        quote_id=None,
                        document_type=DocumentType.APPLICATION,
                        carrier=None,
                        term_key=submission.effective_date,
                        version=1,
                        is_active=True,
                        storage_provider=storage_provider_val,
                        storage_key=storage_key,
                        original_filename=safe_filename,
                        content_type=content_type,
                        size_bytes=len(attachment_data),
                        uploaded_by=session.get('username')
                    )
                    db_session.add(doc)

                    # Log intake
                    intake_data = {
                        'source': 'email_triage',
                        'application_filename': filename,
                        'insured': parsed_data.get('insured'),
                        'coverage_types': (parsed_data.get('submission') or {}).get('coverage_types_needed', []),
                        'effective_date': submission.effective_date
                    }
                    log_action(
                        entity_type='submission',
                        entity_id=submission_id,
                        action='submission_intake_parsed',
                        user=session.get('username'),
                        submission_id=submission_id,
                        details=json.dumps(intake_data)
                    )
                    # Also persist on the submission record
                    submission.submission_intake = json.dumps(intake_data)
                    db_session.commit()

                    result_data['stage'] = 'submission'
                    result_data['message'] = f'Application parsed and {"new submission created" if is_new_submission else "added to existing submission"}.'

                elif doc_type == 'quote':
                    # Parse as quote
                    three_pass_result = process_quote_two_pass(filepath, [])
                    parsed_data = three_pass_result['pass2_normalized']
                    layout_data = three_pass_result['pass1_layout']

                    carrier_name = None
                    effective_date = None
                    expiration_date = None
                    if parsed_data.get('policies') and len(parsed_data['policies']) > 0:
                        first_policy = parsed_data['policies'][0]
                        carrier_name = first_policy.get('carrier')
                        effective_date = first_policy.get('effective_date')
                        expiration_date = first_policy.get('expiration_date')



                    # Move to quoting stage
                    if submission.status == SubmissionStatus.RECEIVED:
                        submission.status = SubmissionStatus.IN_PROGRESS

                    # Create quote record
                    quote = Quote(
                        submission_id=submission_id,
                        carrier_name=carrier_name,
                        raw_document_path=filepath,
                        extracted_json=json.dumps(parsed_data),
                        pass1_layout_json=json.dumps(layout_data),
                        status=QuoteStatus.RECEIVED
                    )
                    db_session.add(quote)
                    db_session.flush()
                    quote_id = quote.id

                    # Save document
                    content_type = 'application/pdf'
                    doc_key = _build_storage_key(
                        submission_id, DocumentType.QUOTE.name, safe_filename,
                        session.get('user_id'), submission.insured_name
                    )
                    storage_provider_val, storage_key = _storage_upload(filepath, doc_key, content_type)
                    doc = Document(
                        submission_id=submission_id,
                        quote_id=quote_id,
                        document_type=DocumentType.QUOTE,
                        carrier=carrier_name,
                        term_key=effective_date or submission.effective_date,
                        version=1,
                        is_active=True,
                        storage_provider=storage_provider_val,
                        storage_key=storage_key,
                        original_filename=safe_filename,
                        content_type=content_type,
                        size_bytes=len(attachment_data),
                        uploaded_by=session.get('username')
                    )
                    db_session.add(doc)
                    db_session.commit()

                    result_data['stage'] = 'quoting'
                    result_data['quote_id'] = quote_id
                    result_data['carrier_name'] = carrier_name
                    result_data['message'] = f'Quote from {carrier_name or "unknown carrier"} parsed and {"new submission created (moved to Quoting stage)" if is_new_submission else "added to submission (moved to Quoting stage)"}.'

                elif doc_type == 'binder':
                    # Binder confirmation — mark submission as bound and save as the binding doc.
                    content_type = 'application/pdf' if filename.lower().endswith('.pdf') else 'application/octet-stream'
                    term_key = submission.effective_date or datetime.now().strftime('%Y-%m-%d')

                    # Deactivate any prior active binder for this term (single active binder per term)
                    db_session.query(Document).filter(
                        Document.submission_id == submission_id,
                        Document.document_type == DocumentType.BINDER,
                        Document.term_key == term_key,
                        Document.is_active == True
                    ).update({'is_active': False}, synchronize_session=False)

                    doc_key = _build_storage_key(
                        submission_id, DocumentType.BINDER.name, safe_filename,
                        session.get('user_id'), submission.insured_name
                    )
                    storage_provider_val, storage_key = _storage_upload(filepath, doc_key, content_type)
                    doc = Document(
                        submission_id=submission_id,
                        quote_id=None,
                        document_type=DocumentType.BINDER,
                        carrier=None,
                        term_key=term_key,
                        version=1,
                        is_active=True,
                        storage_provider=storage_provider_val,
                        storage_key=storage_key,
                        original_filename=safe_filename,
                        content_type=content_type,
                        size_bytes=len(attachment_data),
                        uploaded_by=session.get('username')
                    )
                    db_session.add(doc)

                    # Mark submission as bound
                    submission.status = SubmissionStatus.SENT_TO_FINANCE

                    log_action(
                        entity_type='submission',
                        entity_id=submission_id,
                        action='submission_bound_via_email',
                        user=session.get('username'),
                        submission_id=submission_id,
                        details=f'Binder confirmation received via email ("{filename}"), submission marked as Bound.'
                    )

                    db_session.commit()

                    result_data['stage'] = 'bind'
                    result_data['message'] = f'Binder confirmation received — submission marked as Bound.'

                else:
                    # Other document — just save to submission
                    content_type = 'application/pdf' if filename.lower().endswith('.pdf') else 'application/octet-stream'
                    doc_key = _build_storage_key(
                        submission_id, DocumentType.APPLICATION.name, safe_filename,
                        session.get('user_id'), submission.insured_name
                    )
                    storage_provider_val, storage_key = _storage_upload(filepath, doc_key, content_type)
                    doc = Document(
                        submission_id=submission_id,
                        quote_id=None,
                        document_type=DocumentType.APPLICATION,
                        carrier=None,
                        term_key=submission.effective_date,
                        version=1,
                        is_active=True,
                        storage_provider=storage_provider_val,
                        storage_key=storage_key,
                        original_filename=safe_filename,
                        content_type=content_type,
                        size_bytes=len(attachment_data),
                        uploaded_by=session.get('username')
                    )
                    db_session.add(doc)
                    db_session.commit()

                    result_data['stage'] = 'submission'
                    result_data['message'] = f'Document saved to {"new" if is_new_submission else "existing"} submission.'

                return jsonify(result_data)

            finally:
                # Clean up temp file
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except Exception:
                    pass

        finally:
            db_session.close()

    except Exception as e:
        logger.error(f"Triage attachment error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def _triage_document(filepath: str) -> dict:
    """
    Use LLM to classify a document and extract the insured name.

    Returns:
    {
        "document_type": "application" | "quote" | "binder" | "other",
        "insured_name": str | None,
        "state": str | None,
        "confidence": "high" | "medium" | "low"
    }
    """
    import pdfplumber
    from textwrap import dedent

    # Extract first 2 pages of text for classification
    text_content = ""
    try:
        with pdfplumber.open(filepath) as pdf:
            for i, page in enumerate(pdf.pages[:2]):
                page_text = page.extract_text()
                if page_text:
                    text_content += page_text + "\n\n"
    except Exception as e:
        logger.warning(f"Failed to extract text from {filepath}: {e}")
        return {'document_type': 'other', 'insured_name': None, 'confidence': 'low'}

    if not text_content.strip():
        return {'document_type': 'other', 'insured_name': None, 'confidence': 'low'}

    # Truncate to avoid token limits
    text_content = text_content[:4000]

    prompt = dedent(f"""
    You are classifying an insurance document. Based on the text below, determine:
    1. Document type: Is this an APPLICATION (new business submission form, ACORD 125, etc.),
       a QUOTE (pricing proposal from a carrier with premiums/coverages), a BINDER (confirmation
       that coverage has been bound/issued — e.g. "binder of insurance", "evidence of coverage",
       "we are pleased to confirm binding", "coverage is bound effective...", often includes a
       binder or policy number and confirms the insured is covered), or OTHER (correspondence,
       loss run, SOV, etc.)?
    2. The insured name (the business or person being insured).
    3. The state (if visible).

    RULES:
    - An APPLICATION typically has fields like "Applicant", "Named Insured", coverage types requested,
      and is a form being filled out to request insurance.
    - A QUOTE typically has carrier name, premium amounts, coverage limits, effective/expiration dates,
      and is a pricing proposal (not yet bound).
    - A BINDER confirms coverage has already been bound/issued — look for words like "binder",
      "bound", "confirmation of coverage", "evidence of insurance", or a binder/policy number
      being issued as confirmation rather than as a quote.
    - If it's neither clearly an application, quote, nor binder, classify as "other".
    - For insured name: look for "Named Insured", "Applicant", "Insured", "Account Name".
    - Do NOT confuse agent/broker name with insured name.

    Return ONLY valid JSON:
    {{
        "document_type": "application" | "quote" | "binder" | "other",
        "insured_name": "string or null",
        "state": "two-letter state code or null",
        "confidence": "high" | "medium" | "low"
    }}

    DOCUMENT TEXT:
    {text_content}
    """)

    try:
        from app.parsers.application_parser import _get_llm_client
        client = _get_llm_client()
        result = client.generate_json(prompt)
        return {
            'document_type': (result.get('document_type') or 'other').lower(),
            'insured_name': result.get('insured_name'),
            'state': result.get('state'),
            'confidence': result.get('confidence', 'medium')
        }
    except Exception as e:
        logger.error(f"LLM triage failed: {e}")
        return {'document_type': 'other', 'insured_name': None, 'confidence': 'low'}


def _find_matching_submissions(db_session, insured_name: str, user_id: int = None) -> list:
    """
    Find existing submissions that match the given insured name.
    Uses fuzzy matching (case-insensitive contains).

    Returns list of dicts with submission details for the picker UI.
    """
    if not insured_name:
        return []

    # Normalize for comparison
    name_lower = insured_name.strip().lower()

    # Query submissions — filter by assigned user if provided
    query = db_session.query(Submission)
    if user_id:
        query = query.filter(Submission.assigned_to == user_id)

    submissions = query.all()

    matches = []
    for sub in submissions:
        sub_name = (sub.insured_name or '').strip().lower()
        # Match if names are similar (contains or contained-in)
        if name_lower in sub_name or sub_name in name_lower:
            matches.append({
                'id': sub.id,
                'insured_name': sub.insured_name,
                'effective_date': sub.effective_date,
                'state': sub.state,
                'status': sub.status.value if sub.status else None,
                'quote_count': len(sub.quotes) if sub.quotes else 0,
                'created_at': sub.created_at.isoformat() if sub.created_at else None
            })

    return matches


# ============================================================================
# OAUTH EMAIL CONNECTIONS
# ============================================================================

@bp.route('/api/oauth/connect/<provider>', methods=['GET'])
@login_required
def oauth_connect(provider):
    """
    Start OAuth flow to connect an email account (Gmail or Outlook).
    """
    try:
        if provider not in ['gmail', 'outlook']:
            return jsonify({'success': False, 'error': 'Invalid provider'}), 400
        
        # Store return URL if provided (so callback can redirect back)
        return_url = request.args.get('return_url')
        if return_url:
            session['oauth_return_url'] = return_url
        
        user_id = session.get('user_id')
        
        # Get OAuth config
        config = {
            'GMAIL_CLIENT_ID': current_app.config.get('GMAIL_CLIENT_ID'),
            'GMAIL_CLIENT_SECRET': current_app.config.get('GMAIL_CLIENT_SECRET'),
            'GMAIL_REDIRECT_URI': current_app.config.get('GMAIL_REDIRECT_URI'),
            'MICROSOFT_CLIENT_ID': current_app.config.get('MICROSOFT_CLIENT_ID'),
            'MICROSOFT_CLIENT_SECRET': current_app.config.get('MICROSOFT_CLIENT_SECRET'),
            'MICROSOFT_REDIRECT_URI': current_app.config.get('MICROSOFT_REDIRECT_URI'),
            'MICROSOFT_TENANT_ID': current_app.config.get('MICROSOFT_TENANT_ID', 'common')
        }
        
        # Check if credentials are configured
        if provider == 'gmail':
            if not config.get('GMAIL_CLIENT_ID') or not config.get('GMAIL_CLIENT_SECRET'):
                return jsonify({'success': False, 'error': 'Gmail OAuth not configured. Add GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET to environment.'}), 400
        else:
            if not config.get('MICROSOFT_CLIENT_ID') or not config.get('MICROSOFT_CLIENT_SECRET'):
                return jsonify({'success': False, 'error': 'Outlook OAuth not configured. Add MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET to environment.'}), 400
        
        # Get OAuth service
        oauth_service = get_oauth_service(provider, config)
        
        # Pass login_hint if provided (pre-populates email in consent screen)
        login_hint = request.args.get('login_hint')
        
        if provider == 'outlook':
            if login_hint:
                oauth_service._login_hint = login_hint
            auth_url, flow = oauth_service.get_authorization_url()
            flow_state = flow.get('state', '')
            _store_flow(flow_state, flow, user_id=user_id)  # Store user_id server-side with flow
            state = flow_state
        else:
            auth_url, state = oauth_service.get_authorization_url()
            # For Gmail, append login_hint to the URL
            if login_hint:
                separator = '&' if '?' in auth_url else '?'
                auth_url = f"{auth_url}{separator}login_hint={login_hint}"
            session[f'oauth_state_{provider}'] = state
            session[f'oauth_user_id_{provider}'] = user_id  # Store user_id in session too
            # Store code_verifier for PKCE (needed during token exchange)
            session[f'oauth_code_verifier_{provider}'] = oauth_service._code_verifier
        
        return jsonify({
            'success': True,
            'authorization_url': auth_url,
            'state': state
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/oauth/<provider>/callback', methods=['GET'])
def oauth_callback(provider):
    """
    OAuth callback handler - exchanges code for tokens.
    """
    try:
        if provider not in ['gmail', 'outlook']:
            return jsonify({'success': False, 'error': 'Invalid provider'}), 400
        
        # Get parameters
        code = request.args.get('code')
        state = request.args.get('state')
        error = request.args.get('error')
        
        if error:
            return redirect(url_for('main.kanban', oauth_error=error))
        
        if not code:
            return redirect(url_for('main.kanban', oauth_error='No authorization code received'))

        # Get OAuth config
        config = {
            'GMAIL_CLIENT_ID': current_app.config.get('GMAIL_CLIENT_ID'),
            'GMAIL_CLIENT_SECRET': current_app.config.get('GMAIL_CLIENT_SECRET'),
            'GMAIL_REDIRECT_URI': current_app.config.get('GMAIL_REDIRECT_URI'),
            'MICROSOFT_CLIENT_ID': current_app.config.get('MICROSOFT_CLIENT_ID'),
            'MICROSOFT_CLIENT_SECRET': current_app.config.get('MICROSOFT_CLIENT_SECRET'),
            'MICROSOFT_REDIRECT_URI': current_app.config.get('MICROSOFT_REDIRECT_URI'),
            'MICROSOFT_TENANT_ID': current_app.config.get('MICROSOFT_TENANT_ID', 'common')
        }

        # Get OAuth service
        oauth_service = get_oauth_service(provider, config)

        # Provider-specific token exchange (do this BEFORE checking user_id)
        if provider == 'outlook':
            # Retrieve flow from server-side cache using state from callback URL
            flow_state = request.args.get('state', '')
            flow, user_id = _get_flow(flow_state)
            if not flow:
                raise Exception('OAuth session expired — please try connecting again')
            auth_response = dict(request.args)
            tokens = oauth_service.exchange_code_for_tokens(auth_response, flow)

        else:
            # Gmail — extract user_id from session, then validate state
            user_id = session.get(f'oauth_user_id_{provider}')
            expected_state = session.get(f'oauth_state_{provider}')
            
            if state != expected_state:
                return redirect(url_for('main.kanban', oauth_error='Invalid state parameter'))
            
            # Retrieve code_verifier for PKCE
            code_verifier = session.get(f'oauth_code_verifier_{provider}')
            tokens = oauth_service.exchange_code_for_tokens(code, state, code_verifier=code_verifier)
            session.pop(f'oauth_state_{provider}', None)
            session.pop(f'oauth_user_id_{provider}', None)
            session.pop(f'oauth_code_verifier_{provider}', None)

        # Verify we have a valid user_id
        if not user_id:
            return redirect(url_for('main.kanban', oauth_error='Unable to determine user. Please log in and try again.'))

        # Get user email
        user_email = oauth_service.get_user_email(tokens['access_token'])

        # Save connected account (now do DB operations)
        db_session = get_session()
        try:
            # Check if account already connected
            existing = db_session.query(ConnectedAccount).filter(
                ConnectedAccount.user_id == user_id,
                ConnectedAccount.provider == EmailProvider[provider.upper()],
                ConnectedAccount.email_address == user_email,
                ConnectedAccount.status == ConnectedAccountStatus.ACTIVE
            ).first()

            if existing:
                # Update existing tokens
                existing.set_encrypted_tokens(tokens)
                existing.status = ConnectedAccountStatus.ACTIVE
            else:
                # Create new connected account
                account = ConnectedAccount(
                    user_id=user_id,
                    provider=EmailProvider[provider.upper()],
                    email_address=user_email,
                    encrypted_tokens='',
                    status=ConnectedAccountStatus.ACTIVE
                )
                account.set_encrypted_tokens(tokens)
                db_session.add(account)

            db_session.commit()
            db_session.close()

            # Redirect back to where the user came from (stored in session), or default to kanban
            return_url = session.pop('oauth_return_url', None)
            if return_url:
                # Append oauth_success param so the page knows to auto-trigger actions
                separator = '&' if '?' in return_url else '?'
                return redirect(f"{return_url}{separator}oauth_success=1")
            return redirect(url_for('main.kanban', oauth_success=f'{provider.capitalize()} account connected successfully!'))

        except Exception as db_error:
            db_session.close()
            raise db_error

    except Exception as e:
        logger.error(f"OAuth callback error for {provider}: {str(e)}", exc_info=True)
        return redirect(url_for('main.kanban', oauth_error=str(e)))

@bp.route('/api/oauth/accounts', methods=['GET'])
@login_required
def get_connected_accounts():
    """
    Get all connected email accounts for the current user.
    """
    try:
        user_id = session.get('user_id')
        
        db_session = get_session()
        try:
            accounts = db_session.query(ConnectedAccount).filter(
                ConnectedAccount.user_id == user_id
            ).all()
            
            return jsonify({
                'success': True,
                'accounts': [account.to_dict() for account in accounts]
            })
        finally:
            db_session.close()
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/oauth/accounts/<int:account_id>', methods=['DELETE'])
@login_required
def disconnect_account(account_id):
    """
    Disconnect a connected email account (revokes tokens).
    """
    try:
        user_id = session.get('user_id')
        
        db_session = get_session()
        try:
            account = db_session.query(ConnectedAccount).filter(
                ConnectedAccount.id == account_id,
                ConnectedAccount.user_id == user_id
            ).first()
            
            if not account:
                return jsonify({'success': False, 'error': 'Account not found'}), 404
            
            # Mark as revoked
            account.status = ConnectedAccountStatus.REVOKED
            from datetime import datetime
            account.disconnected_at = datetime.utcnow()
            
            # Clear tokens
            account.encrypted_tokens = ''
            
            db_session.commit()
            
            # Log action
            log_action(
                entity_type='connected_account',
                entity_id=account_id,
                action='disconnected',
                user=session.get('username'),
                details=f"Disconnected {account.provider.value} account: {account.email_address}"
            )
            
            return jsonify({'success': True})
        finally:
            db_session.close()
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# //is this needed
@bp.route('/api/oauth/sync/<int:account_id>', methods=['POST'])
@login_required
def sync_account_emails(account_id):
    """
    Sync emails from a connected account.
    """
    try:
        user_id = session.get('user_id')
        
        db_session = get_session()
        try:
            account = db_session.query(ConnectedAccount).filter(
                ConnectedAccount.id == account_id,
                ConnectedAccount.user_id == user_id
            ).first()
            
            if not account:
                return jsonify({'success': False, 'error': 'Account not found'}), 404
            
            if account.status != ConnectedAccountStatus.ACTIVE:
                return jsonify({'success': False, 'error': 'Account is not active'}), 400
        finally:
            db_session.close()
        
        # Create email client and sync
        config = {
            'GMAIL_CLIENT_ID': current_app.config.get('GMAIL_CLIENT_ID'),
            'GMAIL_CLIENT_SECRET': current_app.config.get('GMAIL_CLIENT_SECRET'),
            'GMAIL_REDIRECT_URI': current_app.config.get('GMAIL_REDIRECT_URI'),
            'MICROSOFT_CLIENT_ID': current_app.config.get('MICROSOFT_CLIENT_ID'),
            'MICROSOFT_CLIENT_SECRET': current_app.config.get('MICROSOFT_CLIENT_SECRET'),
            'MICROSOFT_REDIRECT_URI': current_app.config.get('MICROSOFT_REDIRECT_URI'),
            'MICROSOFT_TENANT_ID': current_app.config.get('MICROSOFT_TENANT_ID', 'common')
        }
        
        email_client = create_email_client(config)
        result = email_client.fetch_and_process_emails(account_id)
        
        # Log action
        log_action(
            entity_type='connected_account',
            entity_id=account_id,
            action='emails_synced',
            user=session.get('username'),
            details=json.dumps(result)
        )
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/oauth/config_status', methods=['GET'])
@login_required
def get_oauth_config_status():
    """
    Get OAuth configuration status.
    """
    try:
        gmail_configured = bool(
            current_app.config.get('GMAIL_CLIENT_ID') and 
            current_app.config.get('GMAIL_CLIENT_SECRET')
        )
        outlook_configured = bool(
            current_app.config.get('MICROSOFT_CLIENT_ID') and 
            current_app.config.get('MICROSOFT_CLIENT_SECRET')
        )
        
        return jsonify({
            'success': True,
            'config': {
                'gmail': {
                    'configured': gmail_configured,
                    'client_id_set': bool(current_app.config.get('GMAIL_CLIENT_ID'))
                },
                'outlook': {
                    'configured': outlook_configured,
                    'client_id_set': bool(current_app.config.get('MICROSOFT_CLIENT_ID'))
                }
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# QUOTE UPLOAD & PROCESSING
# ============================================================================

@bp.route('/api/upload_quote', methods=['POST'])
@login_required
def upload_quote():
    """
    Upload and process a quote PDF.
    Can either create a new submission or add to existing one.
    """
    try:
        print(f"Received quote upload request with form data: {request.form} and files: {request.files}")
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file part'}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No selected file'}), 400

        if not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Invalid file type'}), 400

        # Get submission_id if provided (adding to existing submission)
        submission_id = request.form.get('submission_id', type=int)

        # Save the file
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_filename = f"{timestamp}_{filename}"
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)
        file.save(filepath)

        # Parse the document with three-pass system
        try:
            # Get existing quotes for this submission (if adding to existing)
            existing_quotes = []
            if submission_id:
                submission = get_submission_by_id(submission_id)
                if submission and submission.get('quotes'):
                    existing_quotes = [
                        json.loads(q['extracted_json']) if q.get('extracted_json') else {}
                        for q in submission['quotes']
                    ]

            # Run three-pass processing
            three_pass_result = process_quote_two_pass(filepath, existing_quotes)

            # Extract data from passes
            layout_data = three_pass_result['pass1_layout']
            parsed_data = three_pass_result['pass2_normalized']
            # intent_data = three_pass_result['pass3_intent']

            # print(f"\n📊 Three-Pass Processing Results:")

            # print("parsed_data:")
            # print(json.dumps(parsed_data, indent=2))
            # print(f"  Pass 1: Extracted {layout_data.get('total_pages', 0)} pages")
            # print(f"  Pass 2: Found {len(parsed_data.get('policies', []))} policies")
            # print(f"  Pass 3: Intent = {intent_data.get('quote_intent')}, Confidence = {intent_data.get('confidence')}")
            # print(f"  Comparison Groups: {intent_data.get('comparison_groups', [])}")
            # print(f"  Notes: {intent_data.get('notes', 'N/A')}\n")

            # Extract key fields
            insured_name = parsed_data.get('insured', {}).get('name', 'Unknown')
            carrier_name = None
            effective_date = None
            state = parsed_data.get('insured', {}).get('address', {}).get('state')
            print(f"insured_name: {insured_name}, state: {state}")
            # Try to get carrier and effective date from first policy
            if parsed_data.get('policies') and len(parsed_data['policies']) > 0:
                first_policy = parsed_data['policies'][0]
                carrier_name = first_policy.get('carrier')
                effective_date = first_policy.get('effective_date')
                print(f"carrier_name: {carrier_name}, effective_date: {effective_date}")

            # Create or get submission
            if submission_id:
                # Adding to existing submission - verify it exists
                submission = get_submission_by_id(submission_id)
                if not submission:
                    return jsonify({'success': False, 'error': 'Submission not found'}), 404
            else:
                # Create new submission
                if not effective_date:
                    effective_date = datetime.now().strftime('%Y-%m-%d')

                submission_id = create_submission(
                    insured_name=insured_name,
                    effective_date=effective_date,
                    state=state,
                    user=session.get('username'),
                    assigned_to=session.get('user_id')
                )
                print(f"Created new submission {submission_id}")

            # Extract subjectivities from parsed data (binding requirements)
            subjectivities = parsed_data.get('subjectivities')
            subjectivities_json_str = json.dumps(subjectivities) if subjectivities else None

            # Create quote record with three-pass data
            quote_id = create_quote(
                submission_id=submission_id,
                carrier_name=carrier_name,
                raw_document_path=filepath,
                extracted_json=json.dumps(parsed_data),
                pass1_layout_json=json.dumps(layout_data),
                subjectivities_json=subjectivities_json_str,
                # pass3_intent_json=json.dumps(intent_data),
                # quote_intent=intent_data.get('quote_intent'),
                # comparison_group=','.join(intent_data.get('comparison_groups', [])),
                user=None  # TODO: Add user authentication
            )
            print(f"Created quote {quote_id} for submission {submission_id}")

            # Mirror uploaded quote into generic documents table for stage-based access.
            db_session = get_session()
            try:
                quote_doc_key = _build_storage_key(submission_id, DocumentType.QUOTE.name, filename, session.get('user_id'),insured_name)
                storage_provider, storage_key = _storage_upload(filepath, quote_doc_key, file.content_type)
                doc = Document(
                    submission_id=submission_id,
                    quote_id=quote_id,
                    document_type=DocumentType.QUOTE,
                    carrier=carrier_name,
                    term_key=effective_date,
                    version=1,
                    is_active=True,
                    storage_provider=storage_provider,
                    storage_key=storage_key,
                    original_filename=filename,
                    content_type=file.content_type,
                    size_bytes=os.path.getsize(filepath) if os.path.exists(filepath) else None,
                    uploaded_by=session.get('username')
                )
                db_session.add(doc)
                db_session.commit()
                try:
                    print(f"file to delete: {filepath}")
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except Exception as e:
                    print(f"Warning: Could not delete temp file {filepath}: {e}")
            finally:
                db_session.close()
            # Log parsing action
            log_action(
                entity_type='quote',
                entity_id=quote_id,
                action='parsed',
                submission_id=submission_id,
                quote_id=quote_id,
                details=f"Successfully parsed document with {len(parsed_data.get('policies', []))} policies"
            )

            return jsonify({
                'success': True,
                'submission_id': submission_id,
                'quote_id': quote_id,
                'parsed_data': parsed_data,
                # 'intent_data': intent_data,
                'processing_metadata': three_pass_result['processing_metadata']
            })

        except Exception as e:
            return jsonify({'success': False, 'error': f'Parsing error: {str(e)}'}), 500

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# DELETE ROUTES
# ============================================================================

@bp.route('/api/submission/<int:submission_id>', methods=['DELETE'])
@login_required
def delete_submission(submission_id):
    """
    Delete a submission and all its associated quotes.
    """
    try:
        db_session = get_session()

        # Get the submission
        submission = db_session.query(Submission).filter_by(id=submission_id).first()
        if not submission:
            db_session.close()
            return jsonify({'success': False, 'error': 'Submission not found'}), 404

        # Get all quotes for this submission to delete their files
        quotes = db_session.query(Quote).filter_by(submission_id=submission_id).all()

        # Delete associated quote files
        for quote in quotes:
            if quote.raw_document_path and os.path.exists(quote.raw_document_path):
                try:
                    os.remove(quote.raw_document_path)
                except Exception as e:
                    print(f"Warning: Could not delete file {quote.raw_document_path}: {e}")

        # Store submission info for logging
        insured_name = submission.insured_name

        # Delete related ams_export_jobs (non-nullable FK, not covered by cascade)
        db_session.query(AmsExportJob).filter_by(submission_id=submission_id).delete()

        # Delete related email messages (backref without cascade)
        db_session.query(EmailMessage).filter_by(submission_id=submission_id).delete()

        # Delete the submission (cascade will delete quotes, documents, audit_logs)
        db_session.delete(submission)

        # Keep a deletion audit entry without an FK to the row being removed.
        db_session.add(AuditLog(
            entity_type='submission',
            entity_id=submission_id,
            action='deleted',
            submission_id=None,
            details=f"Deleted submission for {insured_name}"
        ))

        db_session.commit()
        db_session.close()

        return jsonify({
            'success': True,
            'message': f'Submission {submission_id} deleted successfully'
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/quote/<int:quote_id>', methods=['DELETE'])
@login_required
def delete_quote(quote_id):
    """
    Delete a quote while preserving the parent submission.
    Also deletes associated documents (quotes, SOVs, etc. linked to this quote).
    """
    try:
        db_session = get_session()

        # Get the quote
        quote = db_session.query(Quote).filter_by(id=quote_id).first()
        if not quote:
            db_session.close()
            return jsonify({'success': False, 'error': 'Quote not found'}), 404

        submission_id = quote.submission_id

        # Get all documents linked to this quote and delete their files
        documents = db_session.query(Document).filter_by(quote_id=quote_id).all()
        for doc in documents:
            # Delete file from storage
            if doc.storage_provider == 'local':
                if doc.storage_key.startswith(current_app.config['UPLOAD_FOLDER']):
                    local_path = doc.storage_key
                else:
                    local_path = os.path.join(current_app.config.get('DOCUMENTS_LOCAL_FOLDER', current_app.config['UPLOAD_FOLDER']), doc.storage_key)
                if os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                    except Exception as e:
                        print(f"Warning: Could not delete document file {local_path}: {e}")
            # Document will be cascade deleted from DB

        # Delete the quote (cascade will delete documents due to relationship)
        db_session.delete(quote)
        db_session.add(AuditLog(
            entity_type='quote',
            entity_id=quote_id,
            action='deleted',
            submission_id=submission_id,
            quote_id=None,
            details="Deleted quote and associated documents"
        ))

        db_session.commit()
        db_session.close()

        return jsonify({
            'success': True,
            'submission_deleted': False
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# SUBMISSION ASSIGNMENT
# ============================================================================

@bp.route('/api/submission/<int:submission_id>/assign', methods=['PUT'])
@login_required
def assign_submission(submission_id):
    """Assign a submission to a user"""
    try:
        data = request.get_json()
        user_id = data.get('user_id')

        # user_id can be None to unassign
        if user_id is not None and not isinstance(user_id, int):
            return jsonify({'success': False, 'error': 'Invalid user_id'}), 400

        db_session = get_session()
        try:
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            # Verify user exists if assigning
            if user_id is not None:
                user = db_session.query(User).filter_by(id=user_id, is_active=True).first()
                if not user:
                    return jsonify({'success': False, 'error': 'User not found'}), 404

            old_user_id = submission.assigned_to
            submission.assigned_to = user_id
            db_session.commit()

            # Log the assignment change
            log_action(
                entity_type='submission',
                entity_id=submission_id,
                action='assigned',
                submission_id=submission_id,
                details=f"Assigned from user {old_user_id} to user {user_id}"
            )

            return jsonify({
                'success': True,
                'submission': submission.to_dict()
            })
        finally:
            db_session.close()

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# APPETITE SCORING
# ============================================================================

@bp.route('/api/submission/<int:submission_id>/appetite', methods=['GET'])
@login_required
def get_submission_appetite(submission_id):
    """Get detailed appetite score breakdown for a submission"""
    try:
        from app.appetite_scoring import calculate_appetite_score

        # Get submission data
        submission_data = get_submission_by_id(submission_id)
        if not submission_data:
            return jsonify({'success': False, 'error': 'Submission not found'}), 404

        # Calculate appetite score
        score_result = calculate_appetite_score(submission_data, submission_data.get('quotes', []))

        return jsonify({
            'success': True,
            'appetite': score_result
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/appetite/rules', methods=['GET'])
@login_required
def get_appetite_rules():
    """Get all appetite scoring rules"""
    try:
        from app.models import AppetiteRule
        import json

        session = get_session()
        try:
            rules = session.query(AppetiteRule).all()
            rules_data = []

            for rule in rules:
                rule_dict = rule.to_dict()
                rule_dict['rule_data'] = json.loads(rule_dict['rule_data'])

                # Convert Infinity to a large number for JSON compatibility
                if 'ranges' in rule_dict['rule_data']:
                    for range_item in rule_dict['rule_data']['ranges']:
                        if range_item.get('max') == float('inf'):
                            range_item['max'] = 999999999

                rules_data.append(rule_dict)

            return jsonify({
                'success': True,
                'rules': rules_data
            })
        finally:
            session.close()

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/appetite/rules/<int:rule_id>', methods=['PUT'])
@admin_required
def update_appetite_rule(rule_id):
    """Update an appetite scoring rule"""
    try:
        from app.models import AppetiteRule
        import json

        data = request.get_json()
        if not data or 'rule_data' not in data:
            return jsonify({'success': False, 'error': 'Missing rule_data'}), 400

        session = get_session()
        try:
            rule = session.query(AppetiteRule).filter_by(id=rule_id).first()
            if not rule:
                return jsonify({'success': False, 'error': 'Rule not found'}), 404

            # Convert large numbers back to Infinity for storage
            rule_data = data['rule_data']
            if 'ranges' in rule_data:
                for range_item in rule_data['ranges']:
                    if range_item.get('max', 0) >= 999999:
                        range_item['max'] = float('inf')

            # Update rule data
            rule.rule_data = json.dumps(rule_data)

            # Update max_score if provided
            if 'max_score' in data:
                rule.max_score = data['max_score']

            # Update enabled if provided
            if 'enabled' in data:
                rule.enabled = data['enabled']

            session.commit()

            # Get submission IDs before closing session
            from app.models import Submission
            submission_ids = [s.id for s in session.query(Submission).all()]

            # Close session before recalculating
            session.close()

            # Recalculate all submission scores (uses its own session)
            from app.database import update_submission_appetite_score
            for submission_id in submission_ids:
                update_submission_appetite_score(submission_id)

            return jsonify({
                'success': True,
                'message': 'Rule updated successfully'
            })

        except Exception as e:
            session.rollback()
            session.close()
            raise

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# STATUS UPDATES
# ============================================================================

@bp.route('/api/submission/<int:submission_id>/status', methods=['PUT'])
@login_required
def update_submission_status(submission_id):
    """Update submission status"""
    try:
        data = request.get_json()
        new_status = data.get('status')
        is_renewal = data.get('is_renewal')

        if not new_status:
            return jsonify({'success': False, 'error': 'Status is required'}), 400

        session = get_session()
        submission = session.query(Submission).filter_by(id=submission_id).first()

        if not submission:
            session.close()
            return jsonify({'success': False, 'error': 'Submission not found'}), 404

        # Update status
        new_status_enum = SubmissionStatus[new_status.upper().replace(' ', '_')]
        submission.status = new_status_enum

        # If moving back to quoting (IN_PROGRESS), clear quote outcomes
        if new_status_enum == SubmissionStatus.IN_PROGRESS:
            quotes = session.query(Quote).filter_by(submission_id=submission_id).all()
            for quote in quotes:
                quote.quote_outcome = None
                quote.status = QuoteStatus.RECEIVED

        # Update renewal flag if provided
        if is_renewal is not None:
            submission.is_renewal = bool(is_renewal)

        session.commit()
        session.close()

        # Log action
        log_action(
            entity_type='submission',
            entity_id=submission_id,
            action='status_changed',
            submission_id=submission_id,
            details=f"Status changed to {new_status}" + (" (renewal)" if is_renewal else "")
        )

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/submission/<int:submission_id>/status_label', methods=['PUT'])
@login_required
def update_submission_status_label(submission_id):
    """Update editable status label on a submission card."""
    try:
        data = request.get_json() or {}
        raw_label = (data.get('status_label') or '').strip()
        status_label = raw_label[:255] if raw_label else None

        db_session = get_session()
        try:
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            submission.status_label = status_label
            db_session.commit()
        finally:
            db_session.close()

        return jsonify({'success': True, 'status_label': status_label})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _sanitize_notes_html(html_str):
    """Sanitize notes HTML to only allow safe formatting tags."""
    if not html_str:
        return html_str
    # Allow only safe inline formatting tags
    allowed_tags = {'b', 'i', 'u', 'strong', 'em', 'br', 'p', 'div', 'span', 'ul', 'ol', 'li'}
    # Remove any tags not in the allowlist
    def replace_tag(match):
        tag_content = match.group(1)
        # Extract tag name (handle closing tags and attributes)
        tag_name = re.match(r'/?(\w+)', tag_content)
        if tag_name and tag_name.group(1).lower() in allowed_tags:
            return match.group(0)
        return ''
    return re.sub(r'<([^>]+)>', replace_tag, html_str)


@bp.route('/api/submission/<int:submission_id>/notes', methods=['PUT'])
@login_required
def update_submission_notes(submission_id):
    """Update submission notes for a specific stage."""
    try:
        data = request.get_json() or {}
        stage = data.get('stage', 'submission')
        note_text = _sanitize_notes_html((data.get('notes') or '').strip())

        db_session = get_session()
        try:
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            # Load existing notes JSON or start fresh
            existing = {}
            if submission.notes:
                try:
                    existing = json.loads(submission.notes)
                except Exception:
                    existing = {}

            # Update the specific stage note
            if note_text:
                existing[stage] = note_text
            else:
                existing.pop(stage, None)

            submission.notes = json.dumps(existing) if existing else None
            db_session.commit()
        finally:
            db_session.close()

        return jsonify({'success': True, 'notes': existing})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/submission/<int:submission_id>/move_to_bind', methods=['POST'])
@login_required
def move_submission_to_bind(submission_id):
    """Persist quote outcomes (WON/LOST) and move submission to Selection & Bind stage."""
    try:
        data = request.get_json() or {}
        quote_outcomes = data.get('quote_outcomes') or []

        db_session = get_session()
        try:
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            quotes = db_session.query(Quote).filter_by(submission_id=submission_id).all()
            quote_by_id = {q.id: q for q in quotes}

            for row in quote_outcomes:
                quote_id = row.get('quote_id')
                outcome = (row.get('outcome') or '').upper()
                if quote_id not in quote_by_id:
                    continue
                if outcome not in ('WON', 'LOST'):
                    continue

                quote = quote_by_id[quote_id]
                quote.quote_outcome = outcome
                quote.status = QuoteStatus.CHOSEN if outcome == 'WON' else QuoteStatus.REVIEWED

            submission.status = SubmissionStatus.CHOSEN
            db_session.commit()
        finally:
            db_session.close()

        log_action(
            entity_type='submission',
            entity_id=submission_id,
            action='moved_to_bind',
            user=session.get('username'),
            submission_id=submission_id,
            details=json.dumps({'quote_outcomes': quote_outcomes})
        )

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/submission/<int:submission_id>/drag_to_bind', methods=['POST'])
@login_required
def drag_submission_to_bind(submission_id):
    """Handle drag-to-bind from kanban board.
    
    Validates that there are no duplicate coverage types among quotes,
    then marks all quotes as WON and moves submission to CHOSEN status.
    """
    try:
        db_session = get_session()
        try:
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            quotes = db_session.query(Quote).filter_by(submission_id=submission_id).all()

            if not quotes:
                return jsonify({'success': False, 'error': 'No quotes found for this submission'}), 400

            # Check for duplicate coverage types among quotes
            coverage_types = []
            for quote in quotes:
                if quote.extracted_json:
                    try:
                        extracted = json.loads(quote.extracted_json)
                        # Handle policies array structure
                        if 'policies' in extracted and isinstance(extracted['policies'], list):
                            for policy in extracted['policies']:
                                coverage = (policy.get('coverage_type') or '').strip().lower()
                                if coverage and coverage not in ('null', 'n/a', ''):
                                    coverage_types.append(coverage)
                        else:
                            # Fallback: top-level coverage_type
                            coverage = (extracted.get('coverage_type') or '').strip().lower()
                            if coverage and coverage not in ('null', 'n/a', ''):
                                coverage_types.append(coverage)
                    except (json.JSONDecodeError, TypeError):
                        pass

            # Check for duplicates
            seen = set()
            duplicates = set()
            for ct in coverage_types:
                if ct in seen:
                    duplicates.add(ct)
                seen.add(ct)

            if duplicates:
                return jsonify({
                    'success': False,
                    'error': 'duplicate_coverages',
                    'message': "This submission can't be moved to binding because it has multiple quotes for the same coverage type. Please open the submission and select which quotes to bind.",
                    'duplicates': list(duplicates)
                }), 400

            # All good - mark all quotes as WON and move to CHOSEN
            for quote in quotes:
                quote.quote_outcome = 'WON'
                quote.status = QuoteStatus.CHOSEN

            submission.status = SubmissionStatus.CHOSEN
            db_session.commit()
        finally:
            db_session.close()

        log_action(
            entity_type='submission',
            entity_id=submission_id,
            action='moved_to_bind',
            user=session.get('username'),
            submission_id=submission_id,
            details=json.dumps({'source': 'kanban_drag', 'all_quotes_won': True})
        )

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/quote/<int:quote_id>/status', methods=['PUT'])
@login_required
def update_quote_status(quote_id):
    """Update quote status"""
    try:
        data = request.get_json()
        new_status = data.get('status')

        if not new_status:
            return jsonify({'success': False, 'error': 'Status is required'}), 400

        session = get_session()
        quote = session.query(Quote).filter_by(id=quote_id).first()

        if not quote:
            session.close()
            return jsonify({'success': False, 'error': 'Quote not found'}), 404

        # Update status
        quote.status = QuoteStatus[new_status.upper()]
        session.commit()

        submission_id = quote.submission_id
        session.close()

        # Log action
        log_action(
            entity_type='quote',
            entity_id=quote_id,
            action='status_changed',
            submission_id=submission_id,
            quote_id=quote_id,
            details=f"Status changed to {new_status}"
        )

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/quote/<int:quote_id>/data', methods=['PUT'])
@admin_required
def update_quote_data(quote_id):
    """Update parsed quote data for a quote."""
    try:
        data = request.get_json() or {}
        parsed_data = data.get('parsed_data')

        if not isinstance(parsed_data, dict):
            return jsonify({'success': False, 'error': 'parsed_data must be an object'}), 400

        db_session = get_session()
        try:
            quote = db_session.query(Quote).filter_by(id=quote_id).first()
            if not quote:
                return jsonify({'success': False, 'error': 'Quote not found'}), 404

            quote.extracted_json = json.dumps(parsed_data)

            # Sync subjectivities column from parsed data
            subjectivities = parsed_data.get('subjectivities')
            quote.subjectivities_json = json.dumps(subjectivities) if subjectivities else None

            policies = parsed_data.get('policies') if isinstance(parsed_data.get('policies'), list) else []
            first_policy = policies[0] if policies else {}
            if isinstance(first_policy, dict):
                quote.carrier_name = first_policy.get('carrier') or quote.carrier_name

            db_session.commit()
            submission_id = quote.submission_id
        finally:
            db_session.close()

        update_submission_appetite_score(submission_id)

        log_action(
            entity_type='quote',
            entity_id=quote_id,
            action='quote_data_updated',
            user=session.get('username'),
            submission_id=submission_id,
            quote_id=quote_id,
            details='Admin updated parsed quote data'
        )

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/quote/<int:quote_id>/subjectivities', methods=['PUT'])
@login_required
def update_quote_subjectivities(quote_id):
    """Update subjectivities list or checked state for a quote."""
    try:
        data = request.get_json() or {}
        db_session = get_session()
        try:
            quote = db_session.query(Quote).filter_by(id=quote_id).first()
            if not quote:
                return jsonify({'success': False, 'error': 'Quote not found'}), 404

            # Update subjectivities list if provided
            if 'subjectivities' in data:
                subjectivities = data['subjectivities']
                quote.subjectivities_json = json.dumps(subjectivities) if subjectivities else None

            # Update checked state if provided
            if 'checked' in data:
                quote.subjectivities_checked = json.dumps(data['checked'])

            db_session.commit()
        finally:
            db_session.close()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/tenant-settings', methods=['GET'])
@login_required
def get_tenant_settings():
    """Get tenant-level settings."""
    try:
        db_session = get_session()
        try:
            from app.models import TenantSettings
            row = db_session.query(TenantSettings).first()
            settings = row.get_settings() if row else {}
        finally:
            db_session.close()
        return jsonify({'success': True, 'settings': settings})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/tenant-settings', methods=['PUT'])
@admin_required
def update_tenant_settings():
    """Update tenant-level settings (admin only)."""
    try:
        data = request.get_json() or {}
        db_session = get_session()
        try:
            from app.models import TenantSettings
            row = db_session.query(TenantSettings).first()
            if not row:
                row = TenantSettings(settings_json='{}')
                db_session.add(row)
            current = row.get_settings()
            current.update(data)
            row.set_settings(current)
            db_session.commit()
            settings = row.get_settings()
        finally:
            db_session.close()
        return jsonify({'success': True, 'settings': settings})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# ADMIN PAGE
# ============================================================================

@bp.route('/api/admin/generate-short-names', methods=['POST'])
@admin_required
def generate_short_names():
    """Backfill short_name for all submissions that don't have one."""
    from app.short_name import generate_short_name
    db_session = get_session()
    try:
        subs = db_session.query(Submission).filter(
            (Submission.short_name == None) | (Submission.short_name == '')
        ).all()
        updated = 0
        for sub in subs:
            sub.short_name = generate_short_name(sub.insured_name)
            updated += 1
        db_session.commit()
        return jsonify({'success': True, 'updated': updated})
    except Exception as e:
        db_session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db_session.close()


@bp.route('/admin', methods=['GET'])
@admin_required
def admin():
    """Display the admin page with database tables"""
    return render_template('admin.html')

@bp.route('/api/admin/sql', methods=['POST'])
@admin_required
def execute_sql():
    """Execute raw SQL (admin only - dangerous!)"""
    try:
        data = request.get_json()
        logger.info(f"Received SQL execution request: {data}")
        sql = data.get('sql', '').strip()
        logger.info(f"Executing SQL: {sql}")
        
        if not sql:
            return jsonify({'success': False, 'error': 'No SQL provided'}), 400
        
        session = get_session()
        try:
            logger.info(f"Executing admin SQL: {sql}")
            from sqlalchemy import text
            result = session.execute(text(sql))
            
            sql_upper = sql.upper()
            
            # For SELECT queries, return results
            if sql_upper.startswith('SELECT'):
                rows = result.fetchall()
                columns = result.keys()
                data = [dict(zip(columns, row)) for row in rows]
                session.close()
                return jsonify({'success': True, 'columns': list(columns), 'data': data})
            
            # ✅ ADD THIS: For INSERT/UPDATE/DELETE, commit and return affected rows
            else:
                session.commit()
                affected = result.rowcount
                session.close()
                return jsonify({
                    'success': True, 
                    'affected_rows': affected,
                    'message': f'Query executed successfully. {affected} rows affected.'
                })
                
        except Exception as e:
            session.rollback()
            session.close()
            logger.error(f"SQL execution error: {str(e)}")
            return jsonify({'success': False, 'error': str(e)}), 500
            
    except Exception as e:
        logger.error(f"SQL execution error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@bp.route('/api/admin/data', methods=['GET'])
@admin_required
def get_admin_data():
    """API endpoint to get all database data for admin view"""
    try:
        from app.models import AuditLog

        # Get all submissions
        submissions = get_all_submissions()

        # Get all quotes
        session = get_session()
        quotes_query = session.query(Quote).order_by(Quote.created_at.desc()).all()
        quotes = [q.to_dict() for q in quotes_query]

        # Get all users
        users_query = session.query(User).order_by(User.created_at.desc()).all()
        users = [u.to_dict() for u in users_query]

        # Get all brokers
        brokers_query = session.query(Broker).order_by(Broker.name).all()
        brokers = [b.to_dict() for b in brokers_query]

 # Get last 50 audit logs
        audit_logs = session.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(50).all()
        audit_data = [{
            'id': log.id,
            'entity_type': log.entity_type,
            'entity_id': log.entity_id,
            'action': log.action,
            'user': log.user,
            'details': log.details,
            'timestamp': log.timestamp.isoformat(),
            'submission_id': log.submission_id,
            'quote_id': log.quote_id
        } for log in audit_logs]
        
        # Get all email messages (last 100 for performance)
        email_messages_query = session.query(EmailMessage).order_by(EmailMessage.received_date.desc()).limit(100).all()
        email_messages = [e.to_dict() for e in email_messages_query]
        # Get all email attachments (last 100 for performance)
        email_attachments_query = session.query(EmailAttachment).order_by(EmailAttachment.created_at.desc()).limit(100).all()
        email_attachments = [att.to_dict() for att in email_attachments_query]
        # Get all documents
        documents_query = session.query(Document).order_by(Document.created_at.desc()).all()
        documents = [d.to_dict() for d in documents_query]

        # Get all appetite rules
        appetite_rules_query = session.query(AppetiteRule).all()
        appetite_rules = [r.to_dict() for r in appetite_rules_query]

        # Get all AMS export jobs
        ams_export_jobs_query = session.query(AmsExportJob).order_by(AmsExportJob.created_at.desc()).limit(100).all()
        ams_export_jobs = [j.to_dict() for j in ams_export_jobs_query]

        # Get all SMS alerts
        sms_alerts_query = session.query(SmsAlert).order_by(SmsAlert.created_at.desc()).limit(100).all()
        sms_alerts = [a.to_dict() for a in sms_alerts_query]

        # Get all connected accounts
        connected_accounts_query = session.query(ConnectedAccount).all()
        connected_accounts = [c.to_dict() for c in connected_accounts_query]

        session.close()

        return jsonify({
            'success': True,
            'submissions': submissions,
            'quotes': quotes,
            'users': users,
            'brokers': brokers,
            'email_messages': email_messages,
            'email_attachments': email_attachments,
            'documents': documents,
            'appetite_rules': appetite_rules,
            'ams_export_jobs': ams_export_jobs,
            'sms_alerts': sms_alerts,
            'connected_accounts': connected_accounts,
            'audit_log': audit_data
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/admin/users', methods=['POST'])
@admin_required
def create_user():
    """API endpoint to create a new user"""
    try:
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        full_name = data.get('full_name')
        role = data.get('role')

        if not all([username, password, full_name, role]):
            return jsonify({'success': False, 'error': 'All fields are required'}), 400

        # Validate password
        is_valid, error_msg = User.validate_password(password)
        if not is_valid:
            return jsonify({'success': False, 'error': error_msg}), 400

        # Validate role
        try:
            user_role = UserRole[role]
        except KeyError:
            return jsonify({'success': False, 'error': 'Invalid role'}), 400

        db_session = get_session()
        try:
            # Check if username already exists
            existing_user = db_session.query(User).filter_by(username=username).first()
            if existing_user:
                return jsonify({'success': False, 'error': 'Username already exists'}), 400

            # Create new user
            new_user = User(
                username=username,
                full_name=full_name,
                email=data.get('email'),
                role=user_role,
                is_active=True
            )
            new_user.set_password(password)

            db_session.add(new_user)
            db_session.commit()

            log_action(
                entity_type='user',
                entity_id=new_user.id,
                action='created',
                details=f"Created user {username} with role {role}"
            )

            return jsonify({'success': True, 'user': new_user.to_dict()})
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/admin/users/<int:user_id>', methods=['PUT'])
@admin_required
def update_user(user_id):
    """API endpoint to update a user"""
    try:
        data = request.get_json()

        db_session = get_session()
        try:
            user = db_session.query(User).filter_by(id=user_id).first()
            if not user:
                return jsonify({'success': False, 'error': 'User not found'}), 404

            # Update fields if provided
            if 'full_name' in data:
                user.full_name = data['full_name']

            if 'role' in data:
                try:
                    user.role = UserRole[data['role']]
                except KeyError:
                    return jsonify({'success': False, 'error': 'Invalid role'}), 400

            if 'is_active' in data:
                user.is_active = data['is_active']

            if 'password' in data and data['password']:
                is_valid, error_msg = User.validate_password(data['password'])
                if not is_valid:
                    return jsonify({'success': False, 'error': error_msg}), 400
                user.set_password(data['password'])

            db_session.commit()

            log_action(
                entity_type='user',
                entity_id=user_id,
                action='updated',
                details=f"Updated user {user.username}"
            )

            return jsonify({'success': True, 'user': user.to_dict()})
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    """API endpoint to deactivate a user"""
    try:
        db_session = get_session()
        try:
            user = db_session.query(User).filter_by(id=user_id).first()
            if not user:
                return jsonify({'success': False, 'error': 'User not found'}), 404

            # Don't allow deleting yourself
            if user_id == session.get('user_id'):
                return jsonify({'success': False, 'error': 'Cannot deactivate your own account'}), 400

            # Deactivate instead of delete
            user.is_active = False
            db_session.commit()

            log_action(
                entity_type='user',
                entity_id=user_id,
                action='deactivated',
                details=f"Deactivated user {user.username}"
            )

            return jsonify({'success': True})
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# SUBMIT TO MARKET
# ============================================================================

@bp.route('/api/submission/<int:submission_id>/submit_to_market', methods=['POST'])
@login_required
def submit_to_market(submission_id):
    """
    Submit a submission to selected brokers.
    Accepts per-broker email bodies and optional document_ids for attachments.
    Sends individual file attachments via OAuth (Outlook Graph API).
    """
    try:
        user_id = session.get('user_id')
        data = request.get_json() or {}
        broker_entries = data.get('broker_entries', [])  # [{id, body, document_ids}, ...]

        if not broker_entries:
            return jsonify({'success': False, 'error': 'No brokers selected'}), 400

        db_session = get_session()
        try:
            # Get user's saved signature
            user = db_session.query(User).filter_by(id=user_id).first()
            signature = (user.signature or '').strip() if user else ''

            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            # Get all submission documents for reference
            all_documents = db_session.query(Document).filter(
                Document.submission_id == submission_id
            ).all()
            doc_map = {doc.id: doc for doc in all_documents}

            results = {'sent': [], 'failed': [], 'portal_downloads': []}

            for entry in broker_entries:
                try:
                    broker_id = entry.get('id')
                    body_text = (entry.get('body') or '').strip()
                    document_ids = entry.get('document_ids', [])

                    broker = db_session.query(Broker).filter_by(
                        id=broker_id, user_id=user_id, is_enabled=True
                    ).first()

                    if not broker:
                        results['failed'].append({'broker_id': broker_id, 'error': 'Broker not found'})
                        continue

                    # Portal brokers get zip download
                    if broker.is_portal:
                        documents = [doc_map[did] for did in document_ids if did in doc_map] if document_ids else [
                            d for d in all_documents if d.document_type in [DocumentType.APPLICATION, DocumentType.SOV, DocumentType.LOSS_RUN]
                        ]
                        zip_path = _generate_broker_zip(submission, broker, documents)
                        results['portal_downloads'].append({
                            'broker_name': broker.name,
                            'broker_id': broker.id,
                            'zip_path': zip_path
                        })
                        log_action(
                            entity_type='submission', entity_id=submission_id,
                            action='broker_submission_sent', submission_id=submission_id,
                            user=session.get('username'),
                            details=f"Generated zip for portal broker: {broker.name} ({broker.portal_name})"
                        )
                        continue

                    # Email brokers: build body with signature
                    full_body = body_text
                    if signature:
                        full_body += f"\n\n{signature}"

                    # Resolve documents to attach
                    documents = [doc_map[did] for did in document_ids if did in doc_map] if document_ids else [
                        d for d in all_documents if d.document_type in [DocumentType.APPLICATION, DocumentType.SOV, DocumentType.LOSS_RUN]
                    ]

                    print(f"[SUBMIT TO MARKET] broker={broker.name}, document_ids={document_ids}, doc_map_keys={list(doc_map.keys())}, resolved={len(documents)} docs")

                    subject = f"Insurance Submission - {submission.insured_name}"

                    _send_email_via_oauth(
                        to_email=broker.email,
                        subject=subject,
                        body=full_body,
                        documents=documents
                    )

                    results['sent'].append(broker.name)

                    log_action(
                        entity_type='submission', entity_id=submission_id,
                        action='broker_submission_sent', submission_id=submission_id,
                        user=session.get('username'),
                        details=f"Sent to broker: {broker.name} ({broker.email})"
                    )
                except Exception as e:
                    error_msg = str(e)
                    print(f"Error sending email to broker {entry.get('id')}: {error_msg}")
                    # Check if this is an auth/token issue
                    if 're-connect' in error_msg.lower() or 'token' in error_msg.lower() or 'no connected email' in error_msg.lower():
                        return jsonify({
                            'success': False,
                            'needs_reauth': True,
                            'provider': 'outlook',
                            'error': error_msg
                        })
                    results['failed'].append({'broker_id': entry.get('id'), 'error': error_msg})

            log_action(
                entity_type='submission', entity_id=submission_id,
                action='submitted_to_market', submission_id=submission_id,
                details=f"Submitted to {len(broker_entries)} brokers"
            )

            return jsonify({'success': True, 'results': results})
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _send_email_via_oauth(to_email, subject, body, documents=None, raw_attachments=None):
    """
    Send email via OAuth (Outlook Graph API) using the user's connected account.
    Automatically refreshes tokens if expired.
    Falls back to error if no connected account is available.

    Args:
        documents: list of Document ORM objects to attach (reads from storage).
        raw_attachments: list of dicts [{filename, content_base64, content_type}]
                         already encoded — used for reply attachments.
    """
    import base64
    import mimetypes
    from app.oauth_services import get_oauth_service

    try:
        user_id = session.get('user_id')
        db_session = get_session()

        try:
            # Get the user's active connected account
            account = db_session.query(ConnectedAccount).filter(
                ConnectedAccount.user_id == user_id,
                ConnectedAccount.status == ConnectedAccountStatus.ACTIVE
            ).first()

            if not account:
                raise ValueError("No connected email account. Please connect your email account first.")

            # Get tokens
            tokens = account.get_decrypted_tokens()
            if not tokens:
                # Token decryption failed - need re-auth
                account.status = ConnectedAccountStatus.ERROR
                account.last_error = "Token decryption failed - re-authentication required"
                db_session.commit()
                raise ValueError("Email account tokens could not be read. Please re-connect your email account via Check Email.")

            access_token = tokens.get('access_token')
            refresh_token = tokens.get('refresh_token')

            # Get OAuth service
            provider_str = account.provider.value.lower()
            config = {
                'GMAIL_CLIENT_ID': current_app.config.get('GMAIL_CLIENT_ID'),
                'GMAIL_CLIENT_SECRET': current_app.config.get('GMAIL_CLIENT_SECRET'),
                'GMAIL_REDIRECT_URI': current_app.config.get('GMAIL_REDIRECT_URI'),
                'MICROSOFT_CLIENT_ID': current_app.config.get('MICROSOFT_CLIENT_ID'),
                'MICROSOFT_CLIENT_SECRET': current_app.config.get('MICROSOFT_CLIENT_SECRET'),
                'MICROSOFT_REDIRECT_URI': current_app.config.get('MICROSOFT_REDIRECT_URI'),
                'MICROSOFT_TENANT_ID': current_app.config.get('MICROSOFT_TENANT_ID', 'common')
            }
            oauth_service = get_oauth_service(provider_str, config)

            # Auto-refresh token if expired
            if not access_token or (account.expires_at and account.expires_at < datetime.utcnow()):
                if not refresh_token:
                    raise ValueError("Email token expired and no refresh token available. Please re-connect your email account.")

                print(f"[EMAIL] Access token expired for {account.email_address}, refreshing...")
                from datetime import timedelta
                new_tokens = oauth_service.refresh_access_token(refresh_token)
                account.set_encrypted_tokens(new_tokens)
                expires_in = new_tokens.get('expires_in', 3600)
                account.expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
                account.status = ConnectedAccountStatus.ACTIVE
                account.last_error = None
                db_session.commit()
                access_token = new_tokens.get('access_token')
                print(f"[EMAIL] Token refreshed successfully for {account.email_address}")

            # Build attachments list
            attachment_list = None
            if documents:
                attachment_list = []
                for doc in documents:
                    file_data = None

                    if doc.storage_provider == 's3':
                        # Download from S3
                        try:
                            import boto3
                            s3_client = boto3.client(
                                's3',
                                region_name=current_app.config.get('S3_REGION') or None,
                                endpoint_url=current_app.config.get('S3_ENDPOINT_URL') or None
                            )
                            bucket = current_app.config.get('S3_BUCKET')
                            response = s3_client.get_object(Bucket=bucket, Key=doc.storage_key)
                            file_data = response['Body'].read()
                        except Exception as e:
                            print(f"[EMAIL] Failed to download from S3: {doc.storage_key} - {e}")
                            continue

                    elif doc.storage_provider == 'local':
                        file_path = None
                        if doc.storage_key.startswith(current_app.config['UPLOAD_FOLDER']):
                            file_path = doc.storage_key
                        else:
                            file_path = os.path.join(
                                current_app.config.get('DOCUMENTS_LOCAL_FOLDER', current_app.config['UPLOAD_FOLDER']),
                                doc.storage_key
                            )

                        if file_path and os.path.exists(file_path):
                            with open(file_path, 'rb') as f:
                                file_data = f.read()
                        else:
                            print(f"[EMAIL] File not found for document {doc.id}: {file_path}")
                            continue

                    if file_data:
                        mime_type = mimetypes.guess_type(doc.original_filename or '')[0] or 'application/octet-stream'
                        attachment_list.append({
                            'filename': doc.original_filename or os.path.basename(doc.storage_key),
                            'content_base64': base64.b64encode(file_data).decode(),
                            'content_type': mime_type
                        })

            # Append raw attachments (from reply flow — already base64-encoded)
            if raw_attachments:
                if not attachment_list:
                    attachment_list = []
                for att in raw_attachments:
                    attachment_list.append({
                        'filename': att.get('filename', 'attachment'),
                        'content_base64': att.get('content_base64', ''),
                        'content_type': att.get('content_type', 'application/octet-stream')
                    })

            # Send via OAuth
            if provider_str == 'outlook':
                print(f"[EMAIL] Sending via Outlook Graph API to {to_email} from {account.email_address}...")
                oauth_service.send_email(
                    access_token=access_token,
                    to_recipients=[to_email],
                    subject=subject,
                    body_text=body,
                    attachments=attachment_list
                )
                print(f"[EMAIL] Outlook send success to {to_email}")
            elif provider_str == 'gmail':
                # TODO: Implement Gmail send via API
                raise ValueError("Gmail sending not yet implemented. Please connect an Outlook account.")
            else:
                raise ValueError(f"Unsupported email provider: {provider_str}")

        finally:
            db_session.close()

    except Exception as e:
        print(f"[EMAIL] OAuth send FAILED to {to_email}: {type(e).__name__}: {str(e)}")
        raise


def _generate_broker_zip(submission, broker, documents):
    """Generate a zip file with submission documents"""
    import zipfile

    # Create temp directory for zip
    temp_dir = os.path.join(current_app.config['UPLOAD_FOLDER'], 'temp_zips')
    os.makedirs(temp_dir, exist_ok=True)

    # Create zip file
    zip_filename = f"{submission.insured_name.replace(' ', '_')}_{submission.id}_{broker.id}.zip"
    zip_path = os.path.join(temp_dir, zip_filename)

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for doc in documents:
            # Get file path
            if doc.storage_provider == 'local':
                if doc.storage_key.startswith(current_app.config['UPLOAD_FOLDER']):
                    file_path = doc.storage_key
                else:
                    file_path = os.path.join(current_app.config.get('DOCUMENTS_LOCAL_FOLDER', current_app.config['UPLOAD_FOLDER']), doc.storage_key)

                if os.path.exists(file_path):
                    # Add file to zip with document type prefix
                    arcname = f"{doc.document_type.value}/{doc.original_filename}"
                    zipf.write(file_path, arcname=arcname)

    return zip_path


@bp.route('/api/submission/<int:submission_id>/download_broker_zip/<int:broker_id>', methods=['GET'])
@login_required
def download_broker_zip(submission_id, broker_id):
    """Download zip file for a portal broker"""
    try:
        user_id = session.get('user_id')

        db_session = get_session()
        try:
            # Get submission
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            # Get broker
            broker = db_session.query(Broker).filter_by(id=broker_id, user_id=user_id).first()
            if not broker:
                return jsonify({'success': False, 'error': 'Broker not found'}), 404

            if not broker.is_portal:
                return jsonify({'success': False, 'error': 'This is not a portal broker'}), 400

            # Get documents
            documents = db_session.query(Document).filter(
                Document.submission_id == submission_id,
                Document.document_type.in_([DocumentType.APPLICATION, DocumentType.SOV, DocumentType.LOSS_RUN])
            ).all()

            # Generate zip
            zip_path = _generate_broker_zip(submission, broker, documents)

            # Send file
            return send_file(
                zip_path,
                as_attachment=True,
                download_name=os.path.basename(zip_path),
                mimetype='application/zip'
            )
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# BROKER MANAGEMENT
# ============================================================================

@bp.route('/api/brokers', methods=['GET'])
@login_required
def get_brokers():
    """Get all brokers for the current user"""
    try:
        user_id = session.get('user_id')
        db_session = get_session()
        try:
            brokers = db_session.query(Broker).filter_by(user_id=user_id).order_by(Broker.name).all()
            return jsonify({
                'success': True,
                'brokers': [broker.to_dict() for broker in brokers]
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/brokers', methods=['POST'])
@login_required
def create_broker():
    """Create a new broker for the current user"""
    try:
        user_id = session.get('user_id')
        data = request.get_json() or {}

        name = (data.get('name') or '').strip()
        email = (data.get('email') or '').strip()
        portal_name = (data.get('portal_name') or '').strip()
        is_portal = data.get('is_portal', False)
        letterhead = (data.get('letterhead') or '').strip()
        email_body = (data.get('email_body') or '').strip()

        # Validate input
        if not email and not portal_name:
            return jsonify({'success': False, 'error': 'Either email or portal_name is required'}), 400

        if is_portal and not portal_name:
            return jsonify({'success': False, 'error': 'Portal name is required for portal brokers'}), 400

        if not is_portal and not email:
            return jsonify({'success': False, 'error': 'Email is required for email brokers'}), 400

        # Generate name if not provided
        if not name:
            if is_portal:
                name = portal_name
            else:
                # Extract name from email (part before @)
                name = email.split('@')[0].title()

        db_session = get_session()
        try:
            broker = Broker(
                user_id=user_id,
                name=name,
                email=email if not is_portal else None,
                portal_name=portal_name if is_portal else None,
                is_portal=is_portal,
                is_enabled=True,
                letterhead=letterhead if letterhead else None,
                email_body=email_body if email_body else None
            )
            db_session.add(broker)
            db_session.commit()

            return jsonify({
                'success': True,
                'broker': broker.to_dict()
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/brokers/<int:broker_id>', methods=['PUT'])
@login_required
def update_broker(broker_id):
    """Update a broker"""
    try:
        user_id = session.get('user_id')
        data = request.get_json() or {}

        db_session = get_session()
        try:
            broker = db_session.query(Broker).filter_by(id=broker_id, user_id=user_id).first()
            if not broker:
                return jsonify({'success': False, 'error': 'Broker not found'}), 404

            # Update fields
            if 'name' in data:
                broker.name = (data.get('name') or '').strip()
            if 'email' in data:
                broker.email = (data.get('email') or '').strip() if not broker.is_portal else None
            if 'portal_name' in data:
                broker.portal_name = (data.get('portal_name') or '').strip() if broker.is_portal else None
            if 'is_enabled' in data:
                broker.is_enabled = data.get('is_enabled', True)
            if 'letterhead' in data:
                letterhead = (data.get('letterhead') or '').strip()
                broker.letterhead = letterhead if letterhead else None
            if 'email_body' in data:
                email_body = (data.get('email_body') or '').strip()
                broker.email_body = email_body if email_body else None

            db_session.commit()

            return jsonify({
                'success': True,
                'broker': broker.to_dict()
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/brokers/<int:broker_id>', methods=['DELETE'])
@login_required
def delete_broker(broker_id):
    """Delete a broker"""
    try:
        user_id = session.get('user_id')

        db_session = get_session()
        try:
            broker = db_session.query(Broker).filter_by(id=broker_id, user_id=user_id).first()
            if not broker:
                return jsonify({'success': False, 'error': 'Broker not found'}), 404

            db_session.delete(broker)
            db_session.commit()

            return jsonify({'success': True})
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# AMS AGENT SETUP - Download & Install Wizard
# ============================================================================

@bp.route('/api/ams-agent/setup-info', methods=['GET'])
@login_required
def get_ams_agent_setup_info():
    """
    Return download URLs for the AMS agent one-click installers.
    macOS: serves a .command script that auto-installs everything.
    Windows: serves a .bat script that auto-installs everything.
    """
    try:
        import boto3
        from botocore.exceptions import ClientError

        bucket = current_app.config.get('AMS_AGENT_S3_BUCKET')
        prefix = current_app.config.get('AMS_AGENT_S3_PREFIX', 'agent-setup/')
        region = current_app.config.get('S3_REGION', 'us-east-1')
        endpoint_url = current_app.config.get('S3_ENDPOINT_URL') or None

        macos_filename = current_app.config.get('AMS_AGENT_MACOS_FILENAME', 'RiskRunwayLauncher.app.zip')
        windows_filename = current_app.config.get('AMS_AGENT_WINDOWS_FILENAME', 'RiskRunway-Windows-Setup.zip')

        if not bucket:
            return jsonify({
                'success': False,
                'error': 'Agent setup files not configured. Contact your administrator.'
            }), 500

        # Verify the files exist on S3 before offering them
        client = boto3.client('s3', region_name=region, endpoint_url=endpoint_url)
        urls = {}

        for platform_key, filename in [('macos', macos_filename), ('windows', windows_filename)]:
            object_key = f"{prefix}{filename}"
            try:
                client.head_object(Bucket=bucket, Key=object_key)
                # File exists — offer the one-click installer endpoint
                if platform_key == 'macos':
                    urls['macos'] = {
                        'url': '/api/ams-agent/installer/macos',
                        'filename': 'Install-RiskRunway.zip'
                    }
                else:
                    urls['windows'] = {
                        'url': '/api/ams-agent/installer/windows',
                        'filename': 'Install-RiskRunway.bat'
                    }
            except ClientError:
                pass

        if not urls:
            return jsonify({
                'success': False,
                'error': 'No agent setup files found on server. Contact your administrator.'
            }), 404

        return jsonify({'success': True, 'platforms': urls})
    except Exception as e:
        logger.error(f"Error generating agent setup info: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/ams-agent/installer/macos', methods=['GET'])
@login_required
def get_macos_installer():
    """
    Serve a .command file that the user double-clicks in Finder.
    It downloads the .app.zip from S3, unzips, moves to /Applications,
    registers the protocol handler, and cleans up. Zero manual steps.
    """
    import boto3

    bucket = current_app.config.get('AMS_AGENT_S3_BUCKET')
    prefix = current_app.config.get('AMS_AGENT_S3_PREFIX', 'agent-setup/')
    region = current_app.config.get('S3_REGION', 'us-east-1')
    endpoint_url = current_app.config.get('S3_ENDPOINT_URL') or None
    filename = current_app.config.get('AMS_AGENT_MACOS_FILENAME', 'RiskRunwayLauncher.app.zip')

    client = boto3.client('s3', region_name=region, endpoint_url=endpoint_url)
    download_url = client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': f"{prefix}{filename}"},
        ExpiresIn=600
    )

    script = f'''#!/bin/bash
# RiskRunway AMS Agent — One-Click Installer for macOS
# Double-click to install. You can delete this file afterwards.

set -e

echo ""
echo "  ============================================"
echo "   Installing RiskRunway AMS Agent..."
echo "  ============================================"
echo ""

TMPDIR=$(mktemp -d)
ZIP_FILE="$TMPDIR/RiskRunwayLauncher.app.zip"
APP_NAME="RiskRunwayLauncher"

echo "  → Downloading..."
curl -sL -o "$ZIP_FILE" "{download_url}"

echo "  → Extracting..."
unzip -qo "$ZIP_FILE" -d "$TMPDIR"

echo "  → Installing to /Applications..."
rm -rf "/Applications/$APP_NAME.app"
mv "$TMPDIR/$APP_NAME.app" "/Applications/$APP_NAME.app"

# Remove quarantine so it opens without Gatekeeper warning
xattr -rd com.apple.quarantine "/Applications/$APP_NAME.app" 2>/dev/null || true

echo "  → Registering riskrunway:// handler..."
/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister -f "/Applications/$APP_NAME.app"

# Setup directory
mkdir -p "$HOME/.riskrunway"

# Cleanup
rm -rf "$TMPDIR"

echo ""
echo "  ✓ Installation complete!"
echo ""
echo "  Return to your browser to finish setup."
echo "  (You can close this window)"
echo ""
'''

    # Wrap the .command script in a zip so execute permissions are preserved
    # (macOS auto-unzips downloads, and the .command inside retains +x)
    import io
    import zipfile
    import stat

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo('Install-RiskRunway.command')
        # Set Unix permissions: rwxr-xr-x (0o755)
        info.external_attr = (stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH) << 16
        zf.writestr(info, script)

    zip_buffer.seek(0)

    from flask import Response
    response = Response(zip_buffer.getvalue(), mimetype='application/zip')
    response.headers['Content-Disposition'] = 'attachment; filename="Install-RiskRunway.zip"'
    return response


@bp.route('/api/ams-agent/installer/windows', methods=['GET'])
@login_required
def get_windows_installer():
    """
    Serve a .bat file that the user double-clicks.
    Downloads the setup zip from S3, extracts, installs Python deps,
    registers the protocol handler, and cleans up.
    """
    import boto3

    bucket = current_app.config.get('AMS_AGENT_S3_BUCKET')
    prefix = current_app.config.get('AMS_AGENT_S3_PREFIX', 'agent-setup/')
    region = current_app.config.get('S3_REGION', 'us-east-1')
    endpoint_url = current_app.config.get('S3_ENDPOINT_URL') or None
    filename = current_app.config.get('AMS_AGENT_WINDOWS_FILENAME', 'RiskRunway-Windows-Setup.zip')

    client = boto3.client('s3', region_name=region, endpoint_url=endpoint_url)
    download_url = client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': f"{prefix}{filename}"},
        ExpiresIn=600
    )

    # Batch files can't handle & in URLs (cmd.exe treats & as command separator).
    # Solution: encode the PowerShell download command as Base64 so cmd.exe
    # never sees the URL characters at all.
    import base64
    # PowerShell -EncodedCommand expects UTF-16LE Base64
    # Use Join-Path to avoid backslash escaping issues
    ps_download_cmd = f"Invoke-WebRequest -Uri '{download_url}' -OutFile (Join-Path $env:TEMP 'riskrunway_setup.zip')"
    ps_encoded = base64.b64encode(ps_download_cmd.encode('utf-16-le')).decode('ascii')

    script = f'''@echo off
REM RiskRunway AMS Agent - One-Click Installer for Windows
REM Double-click to install. You can delete this file afterwards.

setlocal EnableDelayedExpansion
title RiskRunway Installer

echo.
echo  ============================================
echo   Installing RiskRunway AMS Agent...
echo  ============================================
echo.

set "INSTALL_DIR=%LOCALAPPDATA%\\RiskRunway"
set "TMPDIR=%TEMP%\\riskrunway_install_%RANDOM%"
set "ZIP_FILE=%TEMP%\\riskrunway_setup.zip"

mkdir "%TMPDIR%" 2>nul
mkdir "%INSTALL_DIR%" 2>nul

echo  [1/4] Downloading...
powershell -EncodedCommand {ps_encoded}
if not exist "%ZIP_FILE%" (
    echo        ERROR: Download failed. Check your internet connection.
    pause
    exit /b 1
)

echo  [2/4] Extracting...
powershell -Command "Expand-Archive -Path '%ZIP_FILE%' -DestinationPath '%TMPDIR%\\extracted' -Force"

echo  [3/4] Installing files...
xcopy /s /y /q "%TMPDIR%\\extracted\\RiskRunway-Windows-Setup\\*" "%INSTALL_DIR%\\" >nul 2>&1
if not exist "%INSTALL_DIR%\\local_agent.py" (
    xcopy /s /y /q "%TMPDIR%\\extracted\\*" "%INSTALL_DIR%\\" >nul 2>&1
)

echo  [4/4] Setting up...

REM Check for Python — the Windows 11 stub fakes 'where python' success
REM so we actually try running it to see if it's real
set "PYTHON="
python --version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PYTHON=python"
) else (
    py --version >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        set "PYTHON=py"
    )
)

if "!PYTHON!"=="" (
    echo.
    echo        Python not found. Downloading Python installer...
    echo        (This is ~25MB, may take a minute)
    echo.
    set "PY_INST=%TMPDIR%\\python_installer.exe"
    powershell -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile '!PY_INST!'"
    if not exist "!PY_INST!" (
        echo        ERROR: Could not download Python installer.
        echo        Please install Python manually from https://python.org
        echo        Make sure to check "Add Python to PATH"
        pause
        exit /b 1
    )
    echo        Installing Python (you may see a progress bar)...
    "!PY_INST!" /passive InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_tcltk=1
    set "PATH=%LOCALAPPDATA%\\Programs\\Python\\Python311;%LOCALAPPDATA%\\Programs\\Python\\Python311\\Scripts;!PATH!"
    set "PYTHON=%LOCALAPPDATA%\\Programs\\Python\\Python311\\python.exe"
    if not exist "!PYTHON!" (
        echo        ERROR: Python installation failed.
        echo        Please install Python manually from https://python.org
        pause
        exit /b 1
    )
    echo        Python installed successfully.
    echo.
)

REM Install dependencies
echo        Installing dependencies...
"!PYTHON!" -m pip install --quiet --upgrade pip 2>nul
"!PYTHON!" -m pip install --quiet pyautogui pyperclip mss Pillow requests pywin32 2>nul
echo        Dependencies installed.

REM Register protocol handler using VBScript (no console window)
REM Find pythonw.exe path for windowless execution
set "PYTHONW="
where pythonw.exe >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    for /f "delims=" %%%%p in ('where pythonw.exe') do set "PYTHONW=%%%%p"
)
if "!PYTHONW!"=="" (
    REM Fall back: pythonw.exe is usually next to python.exe
    for /f "delims=" %%%%p in ('where !PYTHON!') do set "PYTHONW=%%%%~dpp\\pythonw.exe"
)
if not exist "!PYTHONW!" set "PYTHONW=pythonw.exe"

REM Create VBScript launcher (runs Python with NO console window)
(
echo ' RiskRunwayLauncher.vbs - Protocol handler for riskrunway:// URLs
echo Dim url, jobId, server, agentPath, cmd
echo Dim fso, shell
echo Set fso = CreateObject^("Scripting.FileSystemObject"^)
echo Set shell = CreateObject^("WScript.Shell"^)
echo If WScript.Arguments.Count = 0 Then WScript.Quit 1
echo url = WScript.Arguments^(0^)
echo Dim queryStr, params, i, pair
echo If InStr^(url, "?"^) ^> 0 Then
echo     queryStr = Mid^(url, InStr^(url, "?"^) + 1^)
echo Else
echo     WScript.Quit 1
echo End If
echo params = Split^(queryStr, "^&"^)
echo jobId = ""
echo server = ""
echo For i = 0 To UBound^(params^)
echo     pair = Split^(params^(i^), "=", 2^)
echo     If UBound^(pair^) ^>= 1 Then
echo         If LCase^(pair^(0^)^) = "job_id" Then jobId = pair^(1^)
echo         If LCase^(pair^(0^)^) = "server" Then server = Unescape^(pair^(1^)^)
echo     End If
echo Next
echo If jobId = "" Or server = "" Then WScript.Quit 1
echo Dim scriptDir
echo scriptDir = fso.GetParentFolderName^(WScript.ScriptFullName^)
echo agentPath = scriptDir ^& "\local_agent.py"
echo If Not fso.FileExists^(agentPath^) Then
echo     MsgBox "Could not find local_agent.py", vbCritical, "RiskRunway"
echo     WScript.Quit 1
echo End If
echo cmd = """!PYTHONW!""" ^& " """ ^& agentPath ^& """ --job-id " ^& jobId ^& " --server " ^& server
echo shell.Run cmd, 0, False
echo WScript.Quit 0
) > "%INSTALL_DIR%\\RiskRunwayLauncher.vbs"

reg add "HKCU\\Software\\Classes\\riskrunway" /f >nul 2>&1
reg add "HKCU\\Software\\Classes\\riskrunway" /ve /t REG_SZ /d "URL:RiskRunway Protocol" /f >nul 2>&1
reg add "HKCU\\Software\\Classes\\riskrunway" /v "URL Protocol" /t REG_SZ /d "" /f >nul 2>&1
reg add "HKCU\\Software\\Classes\\riskrunway\\shell\\open\\command" /f >nul 2>&1
reg add "HKCU\\Software\\Classes\\riskrunway\\shell\\open\\command" /ve /t REG_SZ /d "wscript.exe \\"%INSTALL_DIR%\\RiskRunwayLauncher.vbs\\" \\"%%1\\"" /f >nul 2>&1

REM Cleanup
rmdir /s /q "%TMPDIR%" 2>nul

echo.
echo  ============================================
echo   Installation Complete!
echo  ============================================
echo.
echo  Return to your browser to continue.
echo  (This window will close in 5 seconds)
echo.
timeout /t 5 >nul
exit
'''

    from flask import Response
    response = Response(script, mimetype='application/octet-stream')
    response.headers['Content-Disposition'] = 'attachment; filename="Install-RiskRunway.bat"'
    return response


@bp.route('/api/ams-agent/open-settings', methods=['GET'])
@login_required
def open_settings_instructions():
    """
    Returns a small HTML page that attempts to open macOS System Settings
    to the correct Privacy pane via a deep link URL scheme.
    """
    pane = request.args.get('pane', 'accessibility')

    if pane == 'accessibility':
        title = "Enable Accessibility for Terminal"
        deep_link = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
        step_text = "Accessibility"
    else:
        title = "Enable Screen Recording for Terminal"
        deep_link = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
        step_text = "Screen Recording"

    html = f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 480px; margin: 3rem auto; padding: 1.5rem; color: #1a1a1a; line-height: 1.6; }}
h2 {{ margin-bottom: 1rem; }}
ol {{ padding-left: 1.25rem; }}
li {{ margin-bottom: 0.5rem; }}
.note {{ margin-top: 1.5rem; padding: 1rem; background: #f0f9ff; border-radius: 0.5rem; font-size: 0.875rem; color: #1e40af; }}
</style>
</head><body>
<h2>Enable {step_text}</h2>
<p>System Settings should have opened automatically.</p>
<ol>
    <li>Find <strong>"Terminal"</strong> in the list</li>
    <li>Toggle it <strong>ON</strong></li>
    <li>Close this tab and return to RiskRunway</li>
</ol>
<div class="note">
    <strong>If Settings didn't open:</strong><br>
    Apple Menu → System Settings → Privacy & Security → {step_text}
</div>
<script>window.location.href = '{deep_link}';</script>
</body></html>"""

    return html


@bp.route('/api/ams-agent/mark-installed', methods=['POST'])
@login_required
def mark_ams_agent_installed():
    """
    Mark that the current user has completed agent installation.
    Persisted in the User table so it survives across sessions/devices.
    """
    db_session = get_session()
    try:
        user = db_session.query(User).filter_by(id=session['user_id']).first()
        if user:
            user.ams_agent_installed = True
            db_session.commit()
        return jsonify({'success': True})
    finally:
        db_session.close()


@bp.route('/api/ams-agent/install-status', methods=['GET'])
@login_required
def get_ams_agent_install_status():
    """
    Check if the current user has previously completed agent installation.
    """
    db_session = get_session()
    try:
        user = db_session.query(User).filter_by(id=session['user_id']).first()
        installed = user.ams_agent_installed if user else False
        return jsonify({'success': True, 'installed': installed})
    finally:
        db_session.close()


# ============================================================================
# AMS VISION - Server-side Bedrock/Claude call for the local agent
# ============================================================================

@bp.route('/api/ams/vision', methods=['POST'])
def ams_vision():
    """
    Receives a screenshot from the local agent + a job_id to look up the quote.
    Sends the original quote page images + the AMS screenshot to Claude Vision,
    which reads the source document directly and matches data to form fields.
    Always uses Bedrock — only Claude Vision returns precise pixel coordinates.
    No auth required — called by the local agent on the user's machine.
    """
    try:
        from PIL import Image
        from io import BytesIO
        import settings as settings_module
        from app.parsers.llm_parsers import BedrockClient

        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No JSON payload'}), 400

        screenshot_b64 = data.get('screenshot')
        job_id = data.get('job_id')
        already_filled = data.get('already_filled', [])

        if not screenshot_b64:
            return jsonify({'success': False, 'error': 'screenshot is required'}), 400

        # Decode the AMS screenshot
        screenshot_bytes = base64.b64decode(screenshot_b64)
        screenshot_image = Image.open(BytesIO(screenshot_bytes)).convert("RGB")

        # Load quote page images from the job's quote
        quote_images = []
        if job_id:
            db_session = get_session()
            try:
                job = db_session.query(AmsExportJob).filter_by(id=job_id).first()
                if job and job.quote_id:
                    quote = db_session.query(Quote).filter_by(id=job.quote_id).first()
                    if quote and quote.pass1_layout_json:
                        layout = json.loads(quote.pass1_layout_json)
                        for page in layout.get('pages', []):
                            img_path = page.get('image_path')
                            if img_path and os.path.exists(img_path):
                                quote_images.append(Image.open(img_path).convert("RGB"))
                                logger.info(f"[AMS Vision] Loaded quote page image: {img_path}")
                elif job:
                    # No specific quote_id — try to find quote via submission
                    quotes = db_session.query(Quote).filter_by(submission_id=job.submission_id).all()
                    for quote in quotes:
                        if quote.pass1_layout_json:
                            layout = json.loads(quote.pass1_layout_json)
                            for page in layout.get('pages', []):
                                img_path = page.get('image_path')
                                if img_path and os.path.exists(img_path):
                                    quote_images.append(Image.open(img_path).convert("RGB"))
                                    logger.info(f"[AMS Vision] Loaded quote page image: {img_path}")
                            break  # Use first quote with images
            finally:
                db_session.close()

        # Fallback: if no quote images found, use the old json_data approach
        json_data = data.get('json_data', {})
        if not quote_images and not json_data:
            return jsonify({'success': False, 'error': 'No quote images or data available'}), 400

        # Build the prompt
        skip_note = ""
        if already_filled:
            skip_note = (
                f"\nFields already filled in a previous pass (skip these): "
                f"{sorted(already_filled)}\n"
            )

        if quote_images:
            # New approach: quote images + AMS screenshot
            num_quote_pages = len(quote_images)
            prompt = (
                f"You are looking at {num_quote_pages + 1} images.\n\n"
                f"Images 1-{num_quote_pages}: Pages from an insurance quote document (the SOURCE data).\n"
                f"Image {num_quote_pages + 1}: A screenshot of an AMS (Agency Management System) form (the TARGET to fill).\n\n"

                "Your job:\n"
                "1. Read the quote document to extract all relevant insurance data "
                "(insured name, address, carrier, dates, premiums, broker, etc.).\n"
                "2. Look at the AMS form screenshot and identify every visible, editable field.\n"
                "3. Match data from the quote to the appropriate form fields.\n"
                "4. Return pixel coordinates (x, y) for each text field on the AMS form screenshot "
                f"(the LAST image, image {num_quote_pages + 1}).\n\n"

                "STRICT RULES:\n"
                "- Coordinates must reference the LAST image (the AMS form screenshot).\n"
                "- Only include a match if you are confident the data belongs in that field.\n"
                "- DO NOT guess values — only use data explicitly visible in the quote document.\n"
                "- Broker field on the form is likely referring to the wholesale broker from the quote.\n"
                "- Producer field on the form is referring to the retail agent from the quote.\n"
                "- For fields that are not textboxes (dropdowns, checkboxes), omit coordinates.\n"
                "- Skip fields already filled.\n\n"

                "Formatting rules:\n"
                "- Dates → MM/DD/YYYY\n"
                "- Currency → digits only (no $)\n"
                "- State → 2-letter abbreviation\n"
                "- Phone → (555) 000-0000 if possible\n\n"

                f"{skip_note}"

                "Return ONLY valid JSON. No explanation.\n"
                "Format:\n"
                '{\n'
                '  "Insured Name":     {"x": 630, "y": 354, "value": "Acme Corp LLC", "field_type": "text_field"},\n'
                '  "Effective Date":   {"x": 322, "y": 727, "value": "02/10/2026", "field_type": "text_field"},\n'
                '  "State":            {"value": "LA", "field_type": "dropdown_field"},\n'
                '  "Line of Business": {"value": "Commercial Property", "field_type": "dropdown_field"}\n'
                '}'
            )
            # Send quote pages first, then AMS screenshot last
            all_images = quote_images + [screenshot_image]
        else:
            # Fallback: old approach with pre-extracted JSON
            prompt = (
                "You are looking at a screenshot of an insurance AMS "
                "(Agency Management System) form.\n\n"

                "Here is data available to fill this form — use what matches, ignore what doesn't:\n"
                f"{json.dumps(json_data, indent=2)}\n\n"

                "Your job:\n"
                "1. Look at every visible, editable field on the form.\n"
                "2. Match available data to fields using common sense.\n"
                "3. Return a JSON object for ONLY fields you have a value for.\n"

                "STRICT RULES:\n"
                "- Only include a match if you can clearly explain (to yourself) why the label and key refer to the same concept.\n"
                "- DO NOT guess values.\n"
                "- DO NOT include fields unless you are confident.\n"
                "- Broker field on the form is likely referring to the wholesale broker listed in the data.\n"
                "- Producer field on the form is referring to the retail agent listed in the data.\n"
                "- For fields that are not textboxes, omit coordinates.\n"
                "- Skip fields already filled.\n\n"

                "Formatting rules:\n"
                "- Dates → MM/DD/YYYY\n"
                "- Currency → digits only (no $)\n"
                "- State → 2-letter abbreviation\n"
                "- Phone → (555) 000-0000 if possible\n\n"

                f"{skip_note}"

                "Return ONLY valid JSON. No explanation.\n"
                "Format:\n"
                '{\n'
                '  "Insured Name":     {"x": 630, "y": 354, "value": "Acme Corp LLC", "key_path": "insured name", "field_type": "text_field"},\n'
                '  "Effective Date":   {"x": 322, "y": 727, "value": "02/10/2026", "key_path": "policy start date", "field_type": "text_field"},\n'
                '  "State":            {"value": "LA", "key_path": "insured state", "field_type": "dropdown_field"},\n'
                '  "Line of Business": {"value": "Commercial Property", "key_path": "type of coverage", "field_type": "dropdown_field"}\n'
                '}'
            )
            all_images = [screenshot_image]

        # Always use Bedrock/Claude — only model that returns pixel coordinates
        client = BedrockClient(model=settings_module.BEDROCK_VISION_MODEL, region=settings_module.BEDROCK_REGION)

        field_map = client.generate_json_with_images(prompt, all_images)
        logger.info(f"[AMS Vision] Bedrock returned {len(field_map)} field matches (quote_images={len(quote_images)})")

        return jsonify({'success': True, 'field_map': field_map})

    except Exception as e:
        logger.error(f"[AMS Vision] Error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# AMS EXTENSION FILL — Chrome extension enumerates DOM fields, server matches
# ============================================================================

@bp.route('/api/ams/extension-fill', methods=['POST'])
def ams_extension_fill():
    """
    Called by the Chrome extension content script.
    Receives: job_id + list of form fields (with labels, types, options).
    Returns: fill instructions mapping selectors to values.
    Uses Claude to match quote data to the available form fields.
    """
    try:
        import settings as settings_module
        from app.parsers.llm_parsers import BedrockClient

        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No JSON payload'}), 400

        job_id = data.get('job_id')
        fields = data.get('fields', [])

        if not fields:
            return jsonify({'success': False, 'error': 'No fields provided'}), 400

        # Load quote images for this job
        quote_images = []
        if job_id:
            db_session = get_session()
            try:
                job = db_session.query(AmsExportJob).filter_by(id=job_id).first()
                if job and job.quote_id:
                    quote = db_session.query(Quote).filter_by(id=job.quote_id).first()
                    if quote and quote.pass1_layout_json:
                        from PIL import Image
                        layout = json.loads(quote.pass1_layout_json)
                        for page in layout.get('pages', []):
                            img_path = page.get('image_path')
                            if img_path and os.path.exists(img_path):
                                quote_images.append(Image.open(img_path).convert("RGB"))
                elif job:
                    quotes = db_session.query(Quote).filter_by(submission_id=job.submission_id).all()
                    for quote in quotes:
                        if quote.pass1_layout_json:
                            from PIL import Image
                            layout = json.loads(quote.pass1_layout_json)
                            for page in layout.get('pages', []):
                                img_path = page.get('image_path')
                                if img_path and os.path.exists(img_path):
                                    quote_images.append(Image.open(img_path).convert("RGB"))
                            break
            finally:
                db_session.close()

        if not quote_images:
            return jsonify({'success': False, 'error': 'No quote images found for this job'}), 400

        # Build prompt: field list + quote images → ask Claude to match
        fields_description = json.dumps(fields, indent=2)

        prompt = (
            "You are matching insurance quote data to form fields.\n\n"
            "Below is a list of empty form fields from an AMS (Agency Management System) web form. "
            "Each field has a label, type, selector, and for dropdowns, available options.\n\n"
            f"FORM FIELDS:\n{fields_description}\n\n"
            "The images show pages from an insurance quote document.\n\n"
            "YOUR TASK:\n"
            "- Read the quote document to extract all relevant data\n"
            "- Match extracted data to the appropriate form fields\n"
            "- For dropdown/select fields, pick the closest matching option from the available options list\n"
            "- Format dates as MM/DD/YYYY, currency as digits only (no $), states as 2-letter codes\n"
            "- Type all values in ALL CAPS\n"
            "- Only match data you can clearly read from the quote — do not guess\n"
            "- Skip fields where no matching data exists in the quote\n\n"
            "Return ONLY valid JSON mapping each field's selector to its value:\n"
            '{\n'
            '  "#insured_name": {"value": "ACME CORP LLC"},\n'
            '  "#state": {"value": "LA"},\n'
            '  "[name=\\"effective_date\\"]": {"value": "02/10/2026"}\n'
            '}\n'
            "Only include fields you have confident matches for."
        )

        # Call Claude with quote images
        client = BedrockClient(model=settings_module.BEDROCK_VISION_MODEL, region=settings_module.BEDROCK_REGION)
        fills = client.generate_json_with_images(prompt, quote_images)

        logger.info(f"[AMS Extension Fill] Matched {len(fills)} fields for job {job_id}")

        return jsonify({'success': True, 'fills': fills})

    except Exception as e:
        logger.error(f"[AMS Extension Fill] Error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# AMS COMPUTER USE — Agentic loop with Claude computer-use tool
# ============================================================================

@bp.route('/api/ams/computer-use-step', methods=['POST'])
def ams_computer_use_step():
    """
    One step of the computer-use agentic loop.
    Receives: screenshot (base64), job_id, messages (conversation history)
    Returns: action to execute (click, type, scroll, etc.) or "done"
    
    The local agent calls this in a loop:
      1. Take screenshot → send here
      2. Get action back → execute it
      3. Take new screenshot → send here again
      4. Repeat until "done" or timeout
    """
    try:
        import settings as settings_module
        from PIL import Image
        from io import BytesIO

        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No JSON payload'}), 400

        screenshot_b64 = data.get('screenshot')
        job_id = data.get('job_id')
        messages = data.get('messages', [])
        display_width = data.get('display_width', 1920)
        display_height = data.get('display_height', 1080)

        if not screenshot_b64:
            return jsonify({'success': False, 'error': 'screenshot is required'}), 400

        # Decode screenshot to get dimensions
        screenshot_bytes = base64.b64decode(screenshot_b64)
        screenshot_image = Image.open(BytesIO(screenshot_bytes))
        img_width, img_height = screenshot_image.size

        # Load quote images for the system prompt (first call only — when messages is empty)
        quote_images_b64 = []
        system_prompt = ""
        if not messages:
            # First turn — build the system context with quote images
            quote_context = ""
            if job_id:
                db_session = get_session()
                try:
                    job = db_session.query(AmsExportJob).filter_by(id=job_id).first()
                    if job and job.quote_id:
                        quote = db_session.query(Quote).filter_by(id=job.quote_id).first()
                        if quote and quote.pass1_layout_json:
                            layout = json.loads(quote.pass1_layout_json)
                            for page in layout.get('pages', []):
                                img_path = page.get('image_path')
                                if img_path and os.path.exists(img_path):
                                    with open(img_path, 'rb') as f:
                                        img_bytes = f.read()
                                    quote_images_b64.append(base64.b64encode(img_bytes).decode('ascii'))
                    elif job:
                        quotes = db_session.query(Quote).filter_by(submission_id=job.submission_id).all()
                        for quote in quotes:
                            if quote.pass1_layout_json:
                                layout = json.loads(quote.pass1_layout_json)
                                for page in layout.get('pages', []):
                                    img_path = page.get('image_path')
                                    if img_path and os.path.exists(img_path):
                                        with open(img_path, 'rb') as f:
                                            img_bytes = f.read()
                                        quote_images_b64.append(base64.b64encode(img_bytes).decode('ascii'))
                                break
                finally:
                    db_session.close()

            system_prompt = (
                "You are an insurance data entry assistant. Your job is to fill in an AMS "
                "(Agency Management System) form with data from a quote document.\n\n"
                "You can see the AMS form via screenshots and control it using mouse clicks, "
                "and scrolling.\n\n"
            )

            # Build first user message with quote images + screenshot
            content_parts = []
            for i, img_b64 in enumerate(quote_images_b64):
                content_parts.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}
                })
            content_parts.append({
                "type": "text",
                "text": (
                    f"Above are {len(quote_images_b64)} pages from an insurance quote document. "
                    "Below is a screenshot of an AMS form.\n\n"
                    "YOUR TASK: Only handle DROPDOWNS and SCROLLING. Text fields have already been filled.\n\n"
                    "- There is a small 'exporting...' spinner overlay on screen — IGNORE IT. The form is ready.\n"
                    "- Text fields are already filled — do NOT click on or type into any text input fields\n"
                    "- Look at each dropdown/select field on the form\n"
                    "- Match it to the correct value from the quote document\n"
                    "- Click the dropdown, then click the correct option\n"
                    "- After handling all visible dropdowns, scroll the page down using the scroll action with a large amount (10+) to reveal more fields\n"
                    "- Repeat until you've scrolled through the entire form\n"
                    "- Do NOT type into any text fields\n"
                    "- When there are no more dropdowns and no more scrolling to do, stop."
                )
            })
            content_parts.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": screenshot_b64}
            })

            messages = [{"role": "user", "content": content_parts}]
        else:
            # Subsequent turns — add the new screenshot as a user message
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": screenshot_b64}
                    },
                    {
                        "type": "text",
                        "text": "Here is the updated screenshot after your last action. Continue filling the form, or say done if complete."
                    }
                ]
            })

        # Call Bedrock with computer-use tool
        import boto3
        client = boto3.client("bedrock-runtime", region_name=settings_module.BEDROCK_REGION)

        model_id = settings_module.BEDROCK_MODEL  # Haiku 4.5

        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "anthropic_beta": ["computer-use-2025-01-24"],
            "max_tokens": 4096,
            "system": system_prompt if system_prompt else "",
            "messages": messages,
            "tools": [
                {
                    "type": "computer_20250124",
                    "name": "computer",
                    "display_width_px": display_width,
                    "display_height_px": display_height,
                }
            ],
        }

        # Remove system key if empty (subsequent turns)
        if not system_prompt:
            body.pop("system", None)

        response = client.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
        )

        result = json.loads(response["body"].read())
        stop_reason = result.get("stop_reason", "")
        content_blocks = result.get("content", [])

        logger.info(f"[AMS Computer Use] stop_reason={stop_reason}, blocks={len(content_blocks)}")

        # Parse the response — look for tool_use blocks
        actions = []
        assistant_text = ""
        for block in content_blocks:
            if block.get("type") == "tool_use" and block.get("name") == "computer":
                actions.append(block.get("input", {}))
            elif block.get("type") == "text":
                assistant_text += block.get("text", "")

        # Add assistant response to messages for next turn
        messages.append({"role": "assistant", "content": content_blocks})

        # If there are actions, add a tool_result for the next turn
        if actions:
            # We'll send the screenshot as the tool result in the next call
            tool_use_id = next(
                (b["id"] for b in content_blocks if b.get("type") == "tool_use"),
                None
            )
            if tool_use_id:
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": "Action executed. New screenshot will be provided."
                    }]
                })

        # Determine if we're done
        is_done = (
            stop_reason == "end_turn" and not actions
        ) or "done" in assistant_text.lower()

        return jsonify({
            'success': True,
            'actions': actions,
            'is_done': is_done,
            'messages': messages,
            'assistant_text': assistant_text,
            'stop_reason': stop_reason,
        })

    except Exception as e:
        logger.error(f"[AMS Computer Use] Error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# AMS EXPORT JOBS
# ============================================================================

@bp.route('/api/ams-export/jobs', methods=['POST'])
@login_required
def create_ams_export_job():
    """
    Create a new AMS export job when user clicks 'Export to AMS' button.
    """
    try:
        data = request.get_json() or {}
        submission_id = data.get('submission_id')
        quote_id = data.get('quote_id')  # Optional: specific quote
        
        if not submission_id:
            return jsonify({'success': False, 'error': 'submission_id is required'}), 400
        
        db_session = get_session()
        try:
            # Verify submission exists
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404
            
            # Get quote data if quote_id provided, otherwise get all quotes for submission
            json_data = {}
            if quote_id:
                quote = db_session.query(Quote).filter_by(id=quote_id, submission_id=submission_id).first()
                if not quote:
                    return jsonify({'success': False, 'error': 'Quote not found'}), 404
                if quote.extracted_json:
                    json_data = json.loads(quote.extracted_json)
            else:
                # Get all quotes for this submission — prefer WON quote for AMS export
                quotes = db_session.query(Quote).filter_by(submission_id=submission_id).all()
                won_quote = next((q for q in quotes if q.quote_outcome == 'WON'), None)
                if won_quote:
                    quote_id = won_quote.id
                    if won_quote.extracted_json:
                        json_data = json.loads(won_quote.extracted_json)
                elif quotes:
                    # Fallback to first quote if none marked WON
                    quote_id = quotes[0].id
                    json_data = {
                        'submission': submission.to_dict(),
                        'quotes': [json.loads(q.extracted_json) for q in quotes if q.extracted_json]
                    }
                else:
                    json_data = {
                        'submission': submission.to_dict(),
                        'quotes': []
                    }
            
            # Create the job
            job = AmsExportJob(
                submission_id=submission_id,
                quote_id=quote_id,
                json_data=json.dumps(json_data),
                instructions='Enter this policy data into the AMS form.',
                status='pending',
                attempt_count=0,
                max_attempts=3,
                user_id=session.get('user_id')
            )
            db_session.add(job)
            db_session.commit()
            db_session.refresh(job)
            job_id   = job.id
            job_dict = job.to_dict()             
            # Log the action
            log_action(
                entity_type='submission',
                entity_id=submission_id,
                action='ams_export_job_created',
                user=session.get('username'),
                submission_id=submission_id,
                details=json.dumps({'job_id': job.id})
            )
  # ← serialize while session is still open
            return jsonify({
                'success': True,
                'job': job_dict
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/ams-export/jobs/<int:job_id>', methods=['GET'])
@login_required
def get_ams_export_job(job_id):
    """
    Get the status of an AMS export job.
    """
    try:
        db_session = get_session()
        try:
            job = db_session.query(AmsExportJob).filter_by(id=job_id).first()
            if not job:
                return jsonify({'success': False, 'error': 'Job not found'}), 404
            
            return jsonify({
                'success': True,
                'job': job.to_dict()
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# AMS EXPORT JOBS - Agent Polling Endpoints (no login required)
# ============================================================================

@bp.route('/api/ams/jobs/next', methods=['GET'])
def get_next_ams_export_job():
    """
    Get the next pending job for the local agent to poll.
    Returns a single job or null if no pending jobs.
    No login required - this is called by the local agent.
    """
    try:
        db_session = get_session()
        try:
            # Get the oldest pending job (not picked up yet)
            job = db_session.query(AmsExportJob).filter(
                AmsExportJob.status == 'pending'
            ).order_by(AmsExportJob.created_at.asc()).first()
            
            if not job:
                return jsonify({'success': True, 'job': None})
            
            return jsonify({
                'success': True,
                'job': job.to_dict()
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/ams/jobs/<int:job_id>', methods=['GET'])
def get_ams_export_job_for_agent(job_id):
    """
    Get a specific AMS export job by ID for the local agent to execute.
    Marks the job as 'in_progress' when fetched.
    No login required - this is called by the local agent in single-shot mode.
    """
    try:
        db_session = get_session()
        try:
            job = db_session.query(AmsExportJob).filter_by(id=job_id).first()
            
            if not job:
                return jsonify({'success': False, 'error': 'Job not found'}), 404
            
            # Only allow fetching pending jobs (not already in progress or completed)
            if job.status != 'pending':
                return jsonify({
                    'success': False, 
                    'error': f'Job is not available (status: {job.status})'
                }), 409
            
            # Mark as in progress
            job.status = 'in_progress'
            job.started_at = datetime.utcnow()
            job.attempt_count += 1
            db_session.commit()
            
            return jsonify({
                'success': True,
                'job': job.to_dict()
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/ams/jobs/<int:job_id>/status', methods=['PATCH'])
def update_ams_export_job_status(job_id):
    """
    Update an AMS export job status (called by local agent).
    Expects payload: { "status": "in_progress|completed|failed", "message": "optional message" }
    """
    try:
        data = request.get_json() or {}
        new_status = data.get('status')
        message = data.get('message')  # This is the error_message or success message
        
        # Map 'complete' to 'completed' for consistency
        if new_status == 'complete':
            new_status = 'completed'
        
        valid_statuses = ['pending', 'in_progress', 'completed', 'failed']
        if new_status not in valid_statuses:
            return jsonify({'success': False, 'error': f'Invalid status. Must be one of: {valid_statuses}'}), 400
        
        db_session = get_session()
        try:
            job = db_session.query(AmsExportJob).filter_by(id=job_id).first()
            if not job:
                return jsonify({'success': False, 'error': 'Job not found'}), 404
            
            job.status = new_status
            
            if new_status == 'in_progress' and not job.started_at:
                job.started_at = datetime.utcnow()
                job.attempt_count += 1
            
            if new_status == 'completed':
                job.completed_at = datetime.utcnow()
            
            # Use message as error_message if status is failed
            if new_status == 'failed' and message:
                job.error_message = message
            
            # If failed and attempts remaining, reset to pending for retry
            if new_status == 'failed' and job.attempt_count < job.max_attempts:
                job.status = 'pending'
            
            db_session.commit()
            
            return jsonify({
                'success': True,
                'job': job.to_dict()
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# Keep the old endpoints for frontend compatibility
@bp.route('/api/ams-export/jobs/pending', methods=['GET'])
def get_pending_ams_export_jobs():
    """
    Get pending jobs for the local agent to poll.
    No login required - this is called by the local agent.
    """
    try:
        db_session = get_session()
        try:
            # Get jobs that are pending (not picked up yet)
            pending_jobs = db_session.query(AmsExportJob).filter(
                AmsExportJob.status == 'pending'
            ).order_by(AmsExportJob.created_at.asc()).all()
            
            return jsonify({
                'success': True,
                'jobs': [job.to_dict() for job in pending_jobs]
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/ams-export/jobs/<int:job_id>', methods=['PATCH'])
def update_ams_export_job(job_id):
    """
    Update an AMS export job status (called by local agent).
    """
    try:
        data = request.get_json() or {}
        new_status = data.get('status')
        agent_id = data.get('agent_id')
        error_message = data.get('error_message')
        
        valid_statuses = ['pending', 'in_progress', 'completed', 'failed']
        if new_status not in valid_statuses:
            return jsonify({'success': False, 'error': f'Invalid status. Must be one of: {valid_statuses}'}), 400
        
        db_session = get_session()
        try:
            job = db_session.query(AmsExportJob).filter_by(id=job_id).first()
            if not job:
                return jsonify({'success': False, 'error': 'Job not found'}), 404
            
            job.status = new_status
            
            if agent_id:
                job.agent_id = agent_id
            
            if new_status == 'in_progress' and not job.started_at:
                job.started_at = datetime.utcnow()
                job.attempt_count += 1
            
            if new_status == 'completed':
                job.completed_at = datetime.utcnow()
            
            if error_message:
                job.error_message = error_message
            
            # If failed and attempts remaining, reset to pending for retry
            if new_status == 'failed' and job.attempt_count < job.max_attempts:
                job.status = 'pending'
            
            db_session.commit()
            
            return jsonify({
                'success': True,
                'job': job.to_dict()
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@bp.route('/favicon.ico')
def favicon():
    return '', 204

# ============================================================================
# USER SIGNATURE (for follow-up emails)
# ============================================================================


@bp.route('/api/user/signature', methods=['GET'])
@login_required
def get_user_signature():
    """Get the current user's saved signature."""
    try:
        user_id = session.get('user_id')
        db_session = get_session()
        try:
            user = db_session.query(User).filter_by(id=user_id).first()
            if not user:
                return jsonify({'success': False, 'error': 'User not found'}), 404
            return jsonify({'success': True, 'signature': user.signature or ''})
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/user/signature', methods=['PUT'])
@login_required
def save_user_signature():
    """Save the current user's signature."""
    try:
        user_id = session.get('user_id')
        data = request.get_json() or {}
        signature = (data.get('signature') or '').strip()

        db_session = get_session()
        try:
            user = db_session.query(User).filter_by(id=user_id).first()
            if not user:
                return jsonify({'success': False, 'error': 'User not found'}), 404
            user.signature = signature if signature else None
            db_session.commit()
            return jsonify({'success': True, 'signature': user.signature or ''})
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/submission/<int:submission_id>/send_follow_up', methods=['POST'])
@login_required
def send_follow_up(submission_id):
    """
    Send a follow-up email to one or more brokers for a submission.
    Uses OAuth (Outlook Graph API) to send. Supports optional document attachments.
    """
    try:
        user_id = session.get('user_id')
        data = request.get_json() or {}
        broker_entries = data.get('broker_entries', [])  # [{id, body, document_ids}, ...]

        print(f"[SEND FOLLOW-UP] Received request for submission {submission_id}")
        print(f"[SEND FOLLOW-UP] Broker entries count: {len(broker_entries)}")

        if not broker_entries:
            return jsonify({'success': False, 'error': 'No broker entries provided'}), 400

        db_session = get_session()
        try:
            # Get user's saved signature
            user = db_session.query(User).filter_by(id=user_id).first()
            signature = (user.signature or '').strip() if user else ''

            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            # Get all submission documents for reference
            all_documents = db_session.query(Document).filter(
                Document.submission_id == submission_id
            ).all()
            doc_map = {doc.id: doc for doc in all_documents}

            results = {'sent': [], 'failed': []}

            for entry in broker_entries:
                try:
                    broker_id = entry.get('id')
                    body_text = (entry.get('body') or '').strip()
                    document_ids = entry.get('document_ids', [])

                    broker = db_session.query(Broker).filter_by(
                        id=broker_id, user_id=user_id, is_enabled=True
                    ).first()

                    if not broker or broker.is_portal:
                        results['failed'].append({
                            'broker_id': broker_id,
                            'error': 'Broker not found or is portal-based'
                        })
                        continue

                    # Build full email body with signature
                    full_body = body_text
                    if signature:
                        full_body += f"\n\n{signature}"

                    subject = f"Follow-up: {submission.insured_name}"

                    # Resolve documents to attach
                    documents = [doc_map[did] for did in document_ids if did in doc_map] if document_ids else None

                    _send_email_via_oauth(
                        to_email=broker.email,
                        subject=subject,
                        body=full_body,
                        documents=documents
                    )

                    results['sent'].append({
                        'broker_id': broker_id,
                        'broker_name': broker.name,
                        'email': broker.email
                    })

                    log_action(
                        entity_type='submission',
                        entity_id=submission_id,
                        action='follow_up_sent',
                        user=session.get('username'),
                        submission_id=submission_id,
                        details=f"Follow-up sent to {broker.name} ({broker.email})"
                    )

                except Exception as broker_error:
                    error_msg = str(broker_error)
                    # Check if this is an auth/token issue
                    if 're-connect' in error_msg.lower() or 'token' in error_msg.lower() or 'no connected email' in error_msg.lower():
                        return jsonify({
                            'success': False,
                            'needs_reauth': True,
                            'provider': 'outlook',
                            'error': error_msg
                        })
                    results['failed'].append({
                        'broker_id': broker_id,
                        'error': error_msg
                    })

            db_session.commit()

            return jsonify({
                'success': True,
                'results': results
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/submission/<int:submission_id>/request_to_bind', methods=['POST'])
@login_required
def request_to_bind(submission_id):
    """
    Send a request-to-bind email to the winning quote's broker.
    Sends all selected documents as attachments along with the bind request body.
    """
    try:
        data = request.get_json()
        broker_id = data.get('broker_id')
        body_text = (data.get('body') or '').strip()
        document_ids = data.get('document_ids', [])

        if not broker_id:
            return jsonify({'success': False, 'error': 'No broker specified'}), 400
        if not body_text:
            return jsonify({'success': False, 'error': 'Email body is required'}), 400

        user_id = session.get('user_id')
        db_session = get_session()
        try:
            user = db_session.query(User).filter_by(id=user_id).first()
            signature = (user.signature or '').strip() if user else ''

            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            broker = db_session.query(Broker).filter_by(
                id=broker_id, user_id=user_id, is_enabled=True
            ).first()
            if not broker:
                return jsonify({'success': False, 'error': 'Broker not found'}), 404
            if not broker.email:
                return jsonify({'success': False, 'error': 'Broker has no email address configured'}), 400

            # Resolve documents to attach
            all_documents = db_session.query(Document).filter(
                Document.submission_id == submission_id
            ).all()
            doc_map = {doc.id: doc for doc in all_documents}

            documents = [doc_map[did] for did in document_ids if did in doc_map] if document_ids else all_documents

            # Build full body with signature
            full_body = body_text
            if signature:
                full_body += f"\n\n{signature}"

            subject = f"Request to Bind - {submission.insured_name}"

            _send_email_via_oauth(
                to_email=broker.email,
                subject=subject,
                body=full_body,
                documents=documents
            )

            log_action(
                entity_type='submission',
                entity_id=submission_id,
                action='request_to_bind_sent',
                user=session.get('username'),
                submission_id=submission_id,
                details=f"Request to bind sent to {broker.name} ({broker.email})"
            )

            db_session.commit()

            return jsonify({
                'success': True,
                'broker_name': broker.name,
                'broker_email': broker.email
            })
        except Exception as e:
            error_msg = str(e)
            print(f"Error sending request to bind for submission {submission_id}: {error_msg}")
            if 're-connect' in error_msg.lower() or 'token' in error_msg.lower() or 'no connected email' in error_msg.lower():
                return jsonify({
                    'success': False,
                    'needs_reauth': True,
                    'provider': 'outlook',
                    'error': error_msg
                })
            return jsonify({'success': False, 'error': error_msg}), 500
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']
