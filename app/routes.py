# app/routes.py
from flask import Blueprint, render_template, request, jsonify, current_app, session, redirect, url_for, send_file
import os
import json
import logging
from datetime import datetime
from typing import Dict, Optional, List
import requests
import base64
import uuid
import shutil
from functools import wraps
from werkzeug.utils import secure_filename
from sqlalchemy.orm import Session
from app.parsers.two_pass_parser import process_quote_two_pass
from app.parsers.application_parser import process_application_two_pass
from app.database import (
    get_all_submissions,
    get_submission_by_id,
    create_submission,
    create_quote,
    log_action,
    get_session,
    get_current_db_name,
    set_current_db,
    get_available_databases
)
from app.models import Submission, Quote, SubmissionStatus, QuoteStatus, User, UserRole, AuditLog, Document, DocumentType, Broker, EmailMessage, EmailAttachment, ConnectedAccount, EmailProvider, ConnectedAccountStatus, AmsExportJob
from app.email_scraper import EmailScraper  # IMAP-based scraping (active)
from app.email_client import EmailClient, create_email_client  # OAuth (future)
from app.oauth_services import get_oauth_service

logger = logging.getLogger(__name__)

bp = Blueprint('main', __name__)

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

    local_root = current_app.config.get('DOCUMENTS_LOCAL_FOLDER')
    final_path = os.path.join(local_root, object_key)
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    shutil.copy2(local_path, final_path)
    return 'local', object_key


def _build_storage_key(submission_id, document_type, filename):
    safe_name = secure_filename(filename) or 'document.bin'
    return f"submission_{submission_id}/{document_type.lower()}/{uuid.uuid4().hex}_{safe_name}"


def _document_download_url(document_id):
    return url_for('main.download_document', document_id=document_id)


def _send_bug_report_email(subject, body_text, screenshot_bytes, screenshot_filename, screenshot_subtype='png'):
    """Send bug report email with screenshot attachment using SendGrid HTTP API."""
    import base64
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

    api_key = current_app.config.get('SENDGRID_API_KEY')
    sender = current_app.config.get('BUG_REPORT_SENDER', 'chrisbouy@gmail.com')
    recipient = current_app.config.get('BUG_REPORT_RECIPIENT', 'chrisbouy@gmail.com')

    # Debug logging
    print(f"[BUG REPORT EMAIL] Config:")
    print(f"  API Key: {'*' * 20 if api_key else 'NOT SET'}")
    print(f"  Sender: {sender}")
    print(f"  Recipient: {recipient}")

    if not api_key:
        error_msg = "SendGrid API key is not configured. Set SENDGRID_API_KEY environment variable."
        print(f"[BUG REPORT EMAIL] ERROR: {error_msg}")
        raise ValueError(error_msg)

    # Create the email message
    message = Mail(
        from_email=sender,
        to_emails=recipient,
        subject=subject,
        plain_text_content=body_text
    )

    # Add screenshot attachment
    encoded_file = base64.b64encode(screenshot_bytes).decode()
    attached_file = Attachment(
        FileContent(encoded_file),
        FileName(screenshot_filename),
        FileType(f'image/{screenshot_subtype}'),
        Disposition('attachment')
    )
    message.attachment = attached_file

    # Send via SendGrid HTTP API
    print(f"[BUG REPORT EMAIL] Sending via SendGrid HTTP API...")
    try:
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print(f"[BUG REPORT EMAIL] Success! Status code: {response.status_code}")
        return response
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
# KANBAN BOARD - Landing Page
# ============================================================================

@bp.route('/', methods=['GET'])
@login_required
def kanban():
    """Display the Kanban board with all submissions"""
    return render_template('kanban.html')


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
            'available_databases': get_available_databases()
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
    return render_template('submission.html', submission_id=submission_id, stage_key=stage_key)


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

            subject = f"[IPFS Mapper Bug] Submission {submission.id} / Quote {quote.id}"
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
        insured_name = (request.form.get('insured_name') or '').strip()
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
            intake_data = {
                'source': 'manual',
                'insured': {'name': insured_name, 'address': None},
                'retail_agent': None,
                'quote_number': None,
                'account_number': None,
                'coverage_types': [],
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
            object_key = _build_storage_key(submission_id, DocumentType.APPLICATION.name, filename)
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

        log_action(
            entity_type='submission',
            entity_id=submission_id,
            action='submission_intake_parsed' if has_file else 'submission_created_manual',
            user=session.get('username'),
            submission_id=submission_id,
            details=json.dumps(intake_data)
        )

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

            object_key = _build_storage_key(submission_id, document_type.name, filename)
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

            log_action(
                entity_type='submission',
                entity_id=submission_id,
                action='document_uploaded',
                user=session.get('username'),
                submission_id=submission_id,
                quote_id=quote_id,
                details=json.dumps({
                    'document_id': doc.id,
                    'document_type': document_type.name,
                    'carrier': carrier,
                    'term_key': term_key,
                    'version': next_version
                })
            )

            item = doc.to_dict()
            item['download_url'] = _document_download_url(doc.id)
            return jsonify({'success': True, 'document': item})
        finally:
            db_session.close()
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
                bucket = current_app.config.get('S3_BUCKET')
                client = boto3.client(
                    's3',
                    region_name=current_app.config.get('S3_REGION') or None,
                    endpoint_url=current_app.config.get('S3_ENDPOINT_URL') or None
                )
                signed_url = client.generate_presigned_url(
                    ClientMethod='get_object',
                    Params={'Bucket': bucket, 'Key': doc.storage_key},
                    ExpiresIn=300
                )
                return redirect(signed_url)
            except Exception as err:
                return f"S3 download failed: {err}", 500

        # local storage provider
        if doc.storage_key.startswith(current_app.config['UPLOAD_FOLDER']):
            local_path = doc.storage_key
        else:
            local_path = os.path.join(current_app.config['DOCUMENTS_LOCAL_FOLDER'], doc.storage_key)
        if not os.path.exists(local_path):
            return "Document file missing", 404

        return send_file(local_path, as_attachment=False, download_name=doc.original_filename, mimetype=doc.content_type)
    finally:
        db_session.close()


@bp.route('/api/quote/<int:quote_id>/file', methods=['GET'])
@login_required
def view_quote_file(quote_id):
    """Open the original uploaded quote file."""
    db_session = get_session()
    try:
        quote = db_session.query(Quote).filter_by(id=quote_id).first()
        if not quote:
            return "Quote not found", 404
        if not quote.raw_document_path or not os.path.exists(quote.raw_document_path):
            return "Quote file missing", 404
        return send_file(quote.raw_document_path, as_attachment=False, download_name=os.path.basename(quote.raw_document_path))
    finally:
        db_session.close()


# ============================================================================
# EMAIL SCRAPING
# ============================================================================

@bp.route('/api/email/scrape', methods=['POST'])
@login_required
def trigger_email_scrape():
    """uses OAuth if available, falls back to IMAP"""
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
                'accounts_checked': []
            }
            
            # Try OAuth accounts first
            if oauth_accounts:
                print(f"Processing {len(oauth_accounts)} OAuth accounts")
                for account in oauth_accounts:
                    try:
                        result = _scrape_emails_with_oauth(account, db_session, user_id)
                        print(f"OAuth result: {result}")
                        if result.get('success'):
                            results['processed'] += result.get('processed', 0)
                            results['matched'] += result.get('matched', 0)
                            results['new_emails'] += result.get('new_emails', 0)
                            results['accounts_checked'].append(f"{account.provider.value}: {account.email_address}")
                            results['source'] = 'OAuth'
                            # Collect email details for frontend display
                            if 'email_details' not in results:
                                results['email_details'] = []
                            results['email_details'].extend(result.get('email_details', []))
                    except Exception as oauth_error:
                        logger.error(f"OAuth email scraping failed for {account.email_address}: {oauth_error}")
                        results['accounts_checked'].append(f"{account.provider.value}: {account.email_address} (failed: {str(oauth_error)})")
                
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
            
            # Fall back to IMAP if no OAuth accounts or OAuth failed
            if current_app.config.get('IMAP_PASSWORD'):
                scraper = EmailScraper(
                    imap_server=current_app.config['IMAP_SERVER'],
                    email_address=current_app.config['IMAP_EMAIL'],
                    password=current_app.config['IMAP_PASSWORD'],
                    use_ssl=current_app.config['IMAP_USE_SSL']
                )
                
                # Scrape emails from last 24 hours
                from datetime import timedelta
                since_date = datetime.now() - timedelta(days=24)
                
                imap_result = scraper.scrape_emails(since_date)
                results.update(imap_result)
                results['source'] = 'IMAP'
            else:
                if not oauth_accounts:
                    results['success'] = False
                    results['error'] = 'No OAuth accounts connected and IMAP not configured'
            
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


def _get_user_quote_subjects(db_session: Session, user_id: int) -> List[str]:
    """Get all quote carrier names from submissions assigned to user"""
    quote_subjects = []
    try:
        # Get submissions assigned to this user
        submissions = db_session.query(Submission).filter(
            Submission.assigned_to == user_id
        ).all()
        
        # Collect insured names from quotes
        names = set()
        for sub in submissions:
            if sub.insured_name:
                names.add(sub.insured_name.strip().lower())
        
        quote_subjects = list(names)
    except Exception as e:
        logger.warning(f"Failed to get quote subjects for user {user_id}: {e}")
    return quote_subjects


def _scrape_emails_with_oauth(account: ConnectedAccount, db_session: Session, user_id: int) -> Dict:
    """
    Scrape emails using OAuth credentials from a connected account.
    Filters emails by broker senders and quote subjects.
    """
    from datetime import timedelta
    from app.oauth_services import get_oauth_service, get_unified_email_data
    
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
        # print(f"Decrypted tokens for {account.email_address}: {tokens}")
        access_token = tokens.get('access_token') if tokens else None
        
        if not access_token:
            logger.warning(f"No valid access token for OAuth account {account.email_address} (provider: {account.provider.value})")
            return {
                'success': False,
                'error': f'No valid access token for {account.email_address}',
                'provider': account.provider.value.lower()
            }
        
        # Get OAuth service
        provider_str = account.provider.value.lower()
        print(f"Getting OAuth service for {provider_str}")
        oauth_service = get_oauth_service(provider_str, config)
        
        
        
        # Get broker emails and quote subjects for filtering
        broker_emails = _get_user_broker_emails(db_session, user_id)
        print(f"Broker emails for user {user_id}: {broker_emails}")
        quote_subjects = _get_user_quote_subjects(db_session, user_id)
        print(f"Quote subjects for user {user_id}: {quote_subjects}")

        
        # Fetch emails from last 24 hours
        since_date = datetime.now() - timedelta(days=24)
        unified_emails = oauth_service.fetch_emails(
            access_token=access_token,
            max_results=50,
            since_date=since_date,
            broker_emails=broker_emails,
            quote_subjects=quote_subjects
        )
        
        if not unified_emails:
            return {
                'success': True,
                'processed': 0,
                'new_emails': 0,
                'email_details': []
            }
        
        # Get already-processed message IDs
        existing_message_ids = set(
            row[0] for row in db_session.query(EmailMessage.message_id).all()
        )
        
        processed = 0
        new_emails = 0
        email_details = []
        
        # Process each email
        for unified_email in unified_emails:
            processed += 1
            print(f"Processing email {unified_email.message_id}")
            print(f"Email details: From: {unified_email.from_email}, Subject: {unified_email.subject}, Date: {unified_email.date}")
            # Skip if already processed
            if unified_email.message_id in existing_message_ids:
                continue
            
            new_emails += 1
            email_details.append({
                'from': unified_email.from_email or unified_email.from_name or 'Unknown',
                'subject': unified_email.subject or '(No subject)',
                'date': unified_email.date.isoformat() if unified_email.date else 'Unknown'
            })
            
            # Save email to database (not matched to submission yet - manual matching needed)
            email_msg = EmailMessage(
                message_id=unified_email.message_id,
                subject=unified_email.subject,
                connected_account_id=account.id,
                from_email=unified_email.from_email,
                from_name=unified_email.from_name,
                to_email=unified_email.to_email,
                received_date=unified_email.date,
                body_text=unified_email.body_text,
                body_html=unified_email.body_html,
                has_attachments=len(unified_email.attachments) > 0,
                attachment_count=len(unified_email.attachments),
                is_read=False
            )
            
            db_session.add(email_msg)
            db_session.flush()  # Get email_msg.id before creating attachments
            
            
            # Save attachments
            for att in unified_email.attachments:
                attachment = EmailAttachment(
                    email_id=email_msg.id,
                    message_id=unified_email.message_id,
                    attachment_id=att.get('attachment_id'),
                    filename=att.get('filename', ''),
                    content_type=att.get('content_type', ''),
                    size_bytes=att.get('size', 0)
                )
                db_session.add(attachment)
        
        db_session.commit()
        
        return {
            'success': True,
            'processed': processed,
            'new_emails': new_emails,
            'email_details': email_details,
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
            'configured': bool(current_app.config.get('IMAP_PASSWORD')),
            'imap_server': current_app.config.get('IMAP_SERVER', ''),
            'imap_email': current_app.config.get('IMAP_EMAIL', ''),
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
    """Delete an email message (marks as deleted so it won't reappear on scrape)"""
    try:
        db_session = get_session()
        try:
            email = db_session.query(EmailMessage).filter_by(id=email_id).first()
            if not email:
                return jsonify({'success': False, 'error': 'Email not found'}), 404
            
            # Try to mark as deleted, but if column doesn't exist, just delete the record
            try:
                email.is_deleted = True
                db_session.commit()
            except Exception:
                # is_deleted column might not exist, delete the record instead
                # Delete attachments files
                attachments = db_session.query(EmailAttachment).filter_by(email_id=email_id).all()
                for att in attachments:
                    if att.file_path and os.path.exists(att.file_path):
                        try:
                            os.remove(att.file_path)
                        except Exception:
                            pass
                db_session.delete(email)
                db_session.commit()
            
            return jsonify({'success': True})
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _download_attachment_on_demand(attachment: EmailAttachment, email: EmailMessage, db_session: Session) -> Optional[str]:
    """
    Download email attachment on-demand if not already downloaded.

    This implements lazy attachment loading - attachments are stored as metadata during
    email scraping, then downloaded only when the user clicks "Ingest Quote".

    Handles both OAuth (Gmail/Outlook) and IMAP sources:
    - OAuth: Uses stored access_token with automatic refresh
    - IMAP: Reconnects using config credentials and fetches by message_id + part_index

    Returns the file path or None if download fails.
    """
    # If already downloaded and file exists, return it
    if attachment.file_path and os.path.exists(attachment.file_path):
        logger.info(f"Attachment {attachment.filename} already downloaded at {attachment.file_path}")
        return attachment.file_path

    logger.info(f"Downloading attachment {attachment.filename} on-demand...")

    # Create attachments directory
    attachments_dir = os.path.join('data', 'email_attachments', str(email.id))
    os.makedirs(attachments_dir, exist_ok=True)
    file_path = os.path.join(attachments_dir, attachment.filename)

    try:
        # Check if this is an OAuth email or IMAP email
        print(f"Email {email.id} connected_account_id: {email.connected_account_id}")
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
            attachment_data = service.fetch_attachments(
                access_token=access_token,
                message_id=attachment.message_id,
                attachment_id=attachment.attachment_id
            )

            if attachment_data:
                with open(file_path, 'wb') as f:
                    f.write(attachment_data)

                # Update database with file path
                attachment.file_path = file_path
                db_session.commit()

                logger.info(f"Downloaded OAuth attachment {attachment.filename} to {file_path}")
                return file_path
            else:
                logger.error(f"Failed to download OAuth attachment {attachment.filename}")
                return None

        else:
            # IMAP path - need to reconnect and fetch
            if not current_app.config.get('IMAP_PASSWORD'):
                logger.error("IMAP credentials not configured")
                return None

            scraper = EmailScraper(
                imap_server=current_app.config['IMAP_SERVER'],
                email_address=current_app.config['IMAP_EMAIL'],
                password=current_app.config['IMAP_PASSWORD'],
                use_ssl=current_app.config['IMAP_USE_SSL']
            )

            if not scraper.connect():
                logger.error("Failed to connect to IMAP server")
                return None

            try:
                import email as email_lib
                from email.utils import parsedate_to_datetime

                # Search for the email by Message-ID
                scraper.mail.select('INBOX')
                _, message_numbers = scraper.mail.search(None, f'HEADER Message-ID "{attachment.message_id}"')

                if not message_numbers[0]:
                    logger.error(f"Email with message_id {attachment.message_id} not found in IMAP")
                    return None

                # Fetch the email
                num = message_numbers[0].split()[0]
                _, msg_data = scraper.mail.fetch(num, '(RFC822)')
                email_body = msg_data[0][1]
                msg = email_lib.message_from_bytes(email_body)

                # Walk through parts to find the attachment
                part_index = int(attachment.attachment_id) if attachment.attachment_id else 0
                current_index = 0

                if msg.is_multipart():
                    for part in msg.walk():
                        content_disposition = str(part.get("Content-Disposition", ""))
                        if "attachment" in content_disposition:
                            if current_index == part_index:
                                # Found the attachment
                                payload = part.get_payload(decode=True)
                                if payload:
                                    with open(file_path, 'wb') as f:
                                        f.write(payload)

                                    # Update database with file path
                                    attachment.file_path = file_path
                                    db_session.commit()

                                    logger.info(f"Downloaded IMAP attachment {attachment.filename} to {file_path}")
                                    return file_path
                            current_index += 1

                logger.error(f"Attachment not found in IMAP email at index {part_index}")
                return None

            finally:
                scraper.disconnect()

    except Exception as e:
        logger.error(f"Error downloading attachment on-demand: {e}")
        import traceback
        traceback.print_exc()
        return None


@bp.route('/api/email/<int:email_id>/ingest_quote/<int:submission_id>', methods=['POST'])
@login_required
def ingest_quote_to_submission(email_id, submission_id):
    """Ingest a quote from an email attachment to a submission"""
    try:
        print(f"Starting quote ingestion for email_id {email_id}, submission_id {submission_id}")
        db_session = get_session()
        logger.info(f"Processing quote ingestion for email_id {email_id}, submission_id {submission_id}")
        try:
            # Get the email with attachments eagerly loaded
            email = db_session.query(EmailMessage).filter_by(id=email_id).first()
            if not email:
                return jsonify({'success': False, 'error': 'Email not found'}), 404
            
            # Get the submission
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404
            
            # Get PDF attachments - query separately to avoid session issues
            attachments = db_session.query(EmailAttachment).filter(
                EmailAttachment.email_id == email_id,
                EmailAttachment.filename.like('%.pdf')
            ).all()
            logger.info(f"Found {len(attachments)} PDF attachments for email_id {email_id}")
            if not attachments:
                return jsonify({'success': False, 'error': 'No PDF attachments found in this email'}), 400

            created_quotes = []

            # Process each attachment - download on-demand if needed
            for att in attachments:
                # Download attachment on-demand if not already downloaded
                file_path = _download_attachment_on_demand(att, email, db_session)
                print(f"Downloaded attachment {att.filename} to {file_path}")
                if not file_path:
                    logger.warning(f"Failed to download attachment {att.filename}, skipping")
                    continue
                
                # Copy file to uploads folder
                filename = secure_filename(att.filename)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                unique_filename = f"{timestamp}_{filename}"
                filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)
                shutil.copy2(file_path, filepath)
                logger.info(f"Copied attachment {file_path} to {filepath} for processing quote from email_id {email_id}")
                # Process the quote
                try:
                    three_pass_result = process_quote_two_pass(filepath, [])
                    layout_data = three_pass_result['pass1_layout']
                    parsed_data = three_pass_result['pass2_normalized']
                    
                    # Extract key fields
                    carrier_name = None
                    effective_date = None
                    if parsed_data.get('policies') and len(parsed_data['policies']) > 0:
                        first_policy = parsed_data['policies'][0]
                        carrier_name = first_policy.get('carrier')
                        effective_date = first_policy.get('effective_date')
                    
                    if not effective_date:
                        effective_date = submission.effective_date
                    logger.info(f"Parsed quote data for email_id {email_id}: carrier={carrier_name}, effective_date={effective_date}")
                    # Create quote record
                    quote_id = create_quote(
                        submission_id=submission_id,
                        carrier_name=carrier_name,
                        raw_document_path=filepath,
                        extracted_json=json.dumps(parsed_data),
                        pass1_layout_json=json.dumps(layout_data),
                        user=session.get('username')
                    )
                    logger.info(f"Created quote record with id {quote_id} for email_id {email_id}")
                    # Create document record - need to get a new session
                    quote_session = get_session()
                    try:
                        logger.info(f"Uploading quote document for quote_id {quote_id}, submission_id {submission_id}")
                        quote_doc_key = _build_storage_key(submission_id, DocumentType.QUOTE.name, filename)
                        storage_provider, storage_key = _storage_upload(filepath, quote_doc_key, att.content_type)
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
                            content_type=att.content_type,
                            size_bytes=att.size_bytes,
                            uploaded_by=session.get('username')
                        )
                        logger.info(f"Creating document record for quote_id {quote_id}, submission_id {submission_id}")
                        quote_session.add(doc)
                        quote_session.commit()
                    finally:
                        quote_session.close()
                    
                    created_quotes.append({
                        'quote_id': quote_id,
                        'filename': filename,
                        'carrier': carrier_name
                    })
                    
                    # Log action
                    log_action(
                        entity_type='quote',
                        entity_id=quote_id,
                        action='email_quote_ingested',
                        submission_id=submission_id,
                        quote_id=quote_id,
                        details=json.dumps({'email_id': email_id, 'filename': filename})
                    )
                    
                except Exception as e:
                    print(f"Error processing quote {att.filename}: {e}")
                    continue
            
            if not created_quotes:
                return jsonify({'success': False, 'error': 'Failed to process any quotes'}), 500
            
            # Mark email as read
            email.is_read = True
            db_session.commit()
            
            return jsonify({
                'success': True,
                'quotes': created_quotes,
                'submission_id': submission_id
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


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
            doc_key = _build_storage_key(submission_id, 'CORRESPONDENCE', filename)
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
        
        if provider == 'outlook':
            auth_url, flow = oauth_service.get_authorization_url()
            flow_state = flow.get('state', '')
            _store_flow(flow_state, flow, user_id=user_id)  # Store user_id server-side with flow
            state = flow_state
        else:
            auth_url, state = oauth_service.get_authorization_url()
            session[f'oauth_state_{provider}'] = state
            session[f'oauth_user_id_{provider}'] = user_id  # Store user_id in session too
        
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
            
            tokens = oauth_service.exchange_code_for_tokens(code, state)
            session.pop(f'oauth_state_{provider}', None)
            session.pop(f'oauth_user_id_{provider}', None)

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

            print(f"\n📊 Three-Pass Processing Results:")

            print("parsed_data:")
            print(json.dumps(parsed_data, indent=2))
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

            # Create quote record with three-pass data
            quote_id = create_quote(
                submission_id=submission_id,
                carrier_name=carrier_name,
                raw_document_path=filepath,
                extracted_json=json.dumps(parsed_data),
                pass1_layout_json=json.dumps(layout_data),
                # pass3_intent_json=json.dumps(intent_data),
                # quote_intent=intent_data.get('quote_intent'),
                # comparison_group=','.join(intent_data.get('comparison_groups', [])),
                user=None  # TODO: Add user authentication
            )
            print(f"Created quote {quote_id} for submission {submission_id}")

            # Mirror uploaded quote into generic documents table for stage-based access.
            db_session = get_session()
            try:
                quote_doc_key = _build_storage_key(submission_id, DocumentType.QUOTE.name, filename)
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

        # Delete the submission (cascade will delete quotes due to relationship)
        db_session.delete(submission)

        # Log the deletion
        log_action(
            entity_type='submission',
            entity_id=submission_id,
            action='deleted',
            submission_id=submission_id,
            details=f"Deleted submission for {insured_name}"
        )

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
        log_action(
            entity_type='quote',
            entity_id=quote_id,
            action='deleted',
            submission_id=submission_id,
            quote_id=quote_id,
            details="Deleted quote and associated documents"
        )

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

        if not new_status:
            return jsonify({'success': False, 'error': 'Status is required'}), 400

        session = get_session()
        submission = session.query(Submission).filter_by(id=submission_id).first()

        if not submission:
            session.close()
            return jsonify({'success': False, 'error': 'Submission not found'}), 404

        # Update status
        submission.status = SubmissionStatus[new_status.upper().replace(' ', '_')]
        session.commit()
        session.close()

        # Log action
        log_action(
            entity_type='submission',
            entity_id=submission_id,
            action='status_changed',
            submission_id=submission_id,
            details=f"Status changed to {new_status}"
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


# ============================================================================
# ADMIN PAGE
# ============================================================================

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

        session.close()

        return jsonify({
            'success': True,
            'submissions': submissions,
            'quotes': quotes,
            'users': users,
            'brokers': brokers,
            'email_messages': email_messages,
            'email_attachments': email_attachments,
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
    Sends emails to email brokers and generates zip files for portal brokers.
    """
    try:
        user_id = session.get('user_id')
        data = request.get_json() or {}
        broker_ids = data.get('broker_ids', [])

        if not broker_ids:
            return jsonify({'success': False, 'error': 'No brokers selected'}), 400

        db_session = get_session()
        try:
            # Get submission
            submission = db_session.query(Submission).filter_by(id=submission_id).first()
            if not submission:
                return jsonify({'success': False, 'error': 'Submission not found'}), 404

            # Get selected brokers
            brokers = db_session.query(Broker).filter(
                Broker.id.in_(broker_ids),
                Broker.user_id == user_id,
                Broker.is_enabled == True
            ).all()

            if not brokers:
                return jsonify({'success': False, 'error': 'No valid brokers selected'}), 400

            # Get submission documents (applications, SOVs, loss runs)
            documents = db_session.query(Document).filter(
                Document.submission_id == submission_id,
                Document.document_type.in_([DocumentType.APPLICATION, DocumentType.SOV, DocumentType.LOSS_RUN])
            ).all()

            # Separate email and portal brokers
            email_brokers = [b for b in brokers if not b.is_portal]
            portal_brokers = [b for b in brokers if b.is_portal]

            results = {
                'emails_sent': [],
                'portal_downloads': []
            }

            # Send emails to email brokers
            for broker in email_brokers:
                try:
                    _send_broker_email(submission, broker, documents)
                    results['emails_sent'].append(broker.name)
                except Exception as e:
                    print(f"Error sending email to {broker.name}: {e}")

            # Generate zip files for portal brokers
            for broker in portal_brokers:
                try:
                    zip_path = _generate_broker_zip(submission, broker, documents)
                    results['portal_downloads'].append({
                        'broker_name': broker.name,
                        'zip_path': zip_path
                    })
                except Exception as e:
                    print(f"Error generating zip for {broker.name}: {e}")

            # Log action
            log_action(
                entity_type='submission',
                entity_id=submission_id,
                action='submitted_to_market',
                submission_id=submission_id,
                details=f"Submitted to {len(brokers)} brokers"
            )

            return jsonify({
                'success': True,
                'results': results
            })
        finally:
            db_session.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _send_broker_email(submission, broker, documents):
    """
    Send email to broker with zip file attachment.
    Uses connected OAuth account (Outlook/Gmail) if available, falls back to SendGrid.
    """
    import base64
    
    # Create zip file
    zip_path = _generate_broker_zip(submission, broker, documents)
    
    try:
        # Try to use connected OAuth account first
        user_id = session.get('user_id')
        db_session = get_session()
        
        try:
            # Look for a connected Outlook account
            outlook_account = db_session.query(ConnectedAccount).filter(
                ConnectedAccount.user_id == user_id,
                ConnectedAccount.provider == EmailProvider.OUTLOOK,
                ConnectedAccount.status == ConnectedAccountStatus.ACTIVE
            ).first()
            
            if outlook_account:
                tokens = outlook_account.get_decrypted_tokens()
                access_token = tokens.get('access_token')
                
                if access_token:
                    print(f"[BROKER EMAIL] Using OAuth account: {outlook_account.email_address}")
                    
                    # Prepare email body
                    if broker.email_body:
                        body = broker.email_body
                    else:
                        body = f"""Hello {broker.name},

Please find attached the insurance submission documents for {submission.insured_name}.

Effective Date: {submission.effective_date}
State: {submission.state}

Best regards,
Insurance Placement System"""
                    
                    # Add letterhead if configured
                    if broker.letterhead:
                        body = f"{body}\n\n{broker.letterhead}"
                    
                    # Read and encode zip file for attachment
                    with open(zip_path, 'rb') as f:
                        zip_data = f.read()
                    
                    zip_base64 = base64.b64encode(zip_data).decode()
                    
                    # Send via Graph API
                    config = {
                        'MICROSOFT_CLIENT_ID': current_app.config.get('MICROSOFT_CLIENT_ID'),
                        'MICROSOFT_CLIENT_SECRET': current_app.config.get('MICROSOFT_CLIENT_SECRET'),
                        'MICROSOFT_REDIRECT_URI': current_app.config.get('MICROSOFT_REDIRECT_URI'),
                        'MICROSOFT_TENANT_ID': current_app.config.get('MICROSOFT_TENANT_ID', 'common')
                    }
                    
                    oauth_service = get_oauth_service('outlook', config)
                    
                    # Send email with attachment
                    message_id = oauth_service.send_email(
                        access_token=access_token,
                        to_recipients=[broker.email],
                        subject=f"Insurance Submission - {submission.insured_name}",
                        body_text=body,
                        attachments=[{
                            'filename': os.path.basename(zip_path),
                            'content_base64': zip_base64,
                            'content_type': 'application/zip'
                        }]
                    )
                    
                    print(f"[BROKER EMAIL] Successfully sent via OAuth from {outlook_account.email_address}")
                    return message_id
        
        except Exception as oauth_error:
            print(f"[BROKER EMAIL] OAuth send failed: {oauth_error}")
            logger.error(f"OAuth email send error: {oauth_error}")
            # Fall through to SendGrid fallback
        
        finally:
            db_session.close()
        
        # Fall back to SendGrid if no OAuth account available
        print(f"[BROKER EMAIL] Falling back to SendGrid")
        return _send_broker_email_with_sendgrid(submission, broker, documents, zip_path)
        
    except Exception as e:
        print(f"[BROKER EMAIL] FAILED to send to {broker.name}: {type(e).__name__}: {str(e)}")
        raise
    
    finally:
        # Clean up zip file
        if os.path.exists(zip_path):
            os.remove(zip_path)


def _send_broker_email_with_sendgrid(submission, broker, documents, zip_path):
    """
    Fallback: Send email to broker using SendGrid HTTP API.
    Only used if no OAuth account is connected.
    """
    import base64
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

    try:
        # Email configuration
        api_key = current_app.config.get('SENDGRID_API_KEY')
        # Use broker-specific sender email, with fallback to bug report sender
        sender_email = current_app.config.get('BROKER_EMAIL_SENDER') or current_app.config.get('BUG_REPORT_SENDER', 'chrisbouy@gmail.com')

        if not api_key:
            raise ValueError("SendGrid API key is not configured. Set SENDGRID_API_KEY environment variable.")

        # Build email body
        if broker.email_body:
            body = broker.email_body
        else:
            body = f"""Hello {broker.name},

Please find attached the insurance submission documents for {submission.insured_name}.

Effective Date: {submission.effective_date}
State: {submission.state}

Best regards,
Insurance Placement System"""

        # Add letterhead if configured
        if broker.letterhead:
            body = f"{body}\n\n{broker.letterhead}"

        # Create the email message
        message = Mail(
            from_email=sender_email,
            to_emails=broker.email,
            subject=f"Insurance Submission - {submission.insured_name}",
            plain_text_content=body
        )

        # Read and attach zip file
        with open(zip_path, 'rb') as f:
            zip_data = f.read()

        encoded_file = base64.b64encode(zip_data).decode()
        attached_file = Attachment(
            FileContent(encoded_file),
            FileName(os.path.basename(zip_path)),
            FileType('application/zip'),
            Disposition('attachment')
        )
        message.attachment = attached_file

        # Send via SendGrid HTTP API
        print(f"[BROKER EMAIL] Sending via SendGrid from {sender_email}...")
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print(f"[BROKER EMAIL] SendGrid success! Status code: {response.status_code}")
        return response

    except Exception as e:
        print(f"[BROKER EMAIL] SendGrid FAILED to send to {broker.name}: {type(e).__name__}: {str(e)}")
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
                # Get all quotes for this submission
                quotes = db_session.query(Quote).filter_by(submission_id=submission_id).all()
                json_data = {
                    'submission': submission.to_dict(),
                    'quotes': [json.loads(q.extracted_json) for q in quotes if q.extracted_json]
                }
            
            # Create the job
            job = AmsExportJob(
                submission_id=submission_id,
                quote_id=quote_id,
                json_data=json.dumps(json_data),
                instructions='Enter this policy data into the highlighted form fields.',
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


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']