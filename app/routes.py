# app/routes.py
from flask import Blueprint, render_template, request, jsonify, current_app, session, redirect, url_for, send_file
import os
import json
from datetime import datetime
import requests
import base64
import uuid
import shutil
from functools import wraps
from werkzeug.utils import secure_filename
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
from app.models import Submission, Quote, SubmissionStatus, QuoteStatus, User, UserRole, AuditLog, Document, DocumentType, Broker

bp = Blueprint('main', __name__)


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
    days_until_renewal = _days_until_renewal(submission.get('effective_date'))

    if status == 'received':
        return 'submission'
    if status == 'in progress':
        return 'quoting'
    if status in ('chosen', 'sent to finance') and days_until_renewal is not None and days_until_renewal <= 120:
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

        # Attach document summaries for kanban dropdown and bound indicator.
        submission_ids = [s['id'] for s in submissions]
        docs_by_submission = {sid: [] for sid in submission_ids}
        active_binder_submission_ids = set()
        if submission_ids:
            db_session = get_session()
            try:
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
            finally:
                db_session.close()

        for sub in submissions:
            sub['documents'] = docs_by_submission.get(sub['id'], [])
            sub['is_bound'] = sub['id'] in active_binder_submission_ids

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

        session.close()

        return jsonify({
            'success': True,
            'submissions': submissions,
            'quotes': quotes,
            'users': users,
            'brokers': brokers,
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
    """Send email to broker with zip file attachment using SendGrid HTTP API."""
    import base64
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition

    # Create zip file
    zip_path = _generate_broker_zip(submission, broker, documents)

    try:
        # Email configuration
        api_key = current_app.config.get('SENDGRID_API_KEY')
        sender_email = current_app.config.get('BUG_REPORT_SENDER', 'chrisbouy@gmail.com')

        if not api_key:
            raise ValueError("SendGrid API key is not configured. Set SENDGRID_API_KEY environment variable.")

        # Build email body - use custom body if configured, otherwise use default
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
        print(f"[BROKER EMAIL] Sending submission to {broker.name} ({broker.email})...")
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        print(f"[BROKER EMAIL] Success! Status code: {response.status_code}")
        return response

    except Exception as e:
        print(f"[BROKER EMAIL] FAILED to send to {broker.name}: {type(e).__name__}: {str(e)}")
        raise
    finally:
        # Clean up zip file
        if os.path.exists(zip_path):
            os.remove(zip_path)


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
# HELPER FUNCTIONS
# ============================================================================

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']
