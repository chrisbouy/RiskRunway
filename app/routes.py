# app/routes.py
from flask import Blueprint, render_template, request, jsonify, current_app, session, redirect, url_for
import os
import json
import requests
import base64
from functools import wraps
from werkzeug.utils import secure_filename
from datetime import datetime
from app.parsers.three_pass_parser import process_quote_three_pass
from app.parsers.application_parser import process_application_two_pass
from app.database import (
    get_all_submissions,
    get_submission_by_id,
    create_submission,
    create_quote,
    log_action,
    get_session
)
from app.models import Submission, Quote, SubmissionStatus, QuoteStatus, User, UserRole, AuditLog

bp = Blueprint('main', __name__)


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
            three_pass_result = process_quote_three_pass(temp_filepath, [])
            
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
    status = (submission.get('status') or '').lower()
    if status == 'received':
        stage_key = 'submission'
    elif status == 'in progress':
        stage_key = 'quoting'
    else:
        stage_key = 'bind'
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
            user=session.get('username')
        )

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
            three_pass_result = process_quote_three_pass(filepath, existing_quotes)

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
                    user=None  # TODO: Add user authentication
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
    Delete a quote. If it's the last quote in a submission, delete the submission too.
    """
    try:
        session = get_session()

        # Get the quote
        quote = session.query(Quote).filter_by(id=quote_id).first()
        if not quote:
            session.close()
            return jsonify({'success': False, 'error': 'Quote not found'}), 404

        submission_id = quote.submission_id

        # Count quotes in this submission
        quote_count = session.query(Quote).filter_by(submission_id=submission_id).count()

        submission_deleted = False

        if quote_count == 1:
            # This is the last quote, delete the submission too
            submission = session.query(Submission).filter_by(id=submission_id).first()
            if submission:
                session.delete(submission)
                submission_deleted = True
                log_action(
                    entity_type='submission',
                    entity_id=submission_id,
                    action='deleted',
                    submission_id=submission_id,
                    details=f"Deleted submission (last quote removed)"
                )
        else:
            # Just delete the quote
            session.delete(quote)
            log_action(
                entity_type='quote',
                entity_id=quote_id,
                action='deleted',
                submission_id=submission_id,
                quote_id=quote_id,
                details=f"Deleted quote"
            )

        session.commit()
        session.close()

        return jsonify({
            'success': True,
            'submission_deleted': submission_deleted
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
# HELPER FUNCTIONS
# ============================================================================

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']
