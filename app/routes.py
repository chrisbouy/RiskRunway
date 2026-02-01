# app/routes.py
from flask import Blueprint, render_template, request, jsonify, current_app
import os
import json
from werkzeug.utils import secure_filename
from datetime import datetime
from app.parsers import analyze_with_gemini
from app.database import (
    get_all_submissions,
    get_submission_by_id,
    create_submission,
    create_quote,
    log_action,
    get_session
)
from app.models import Submission, Quote, SubmissionStatus, QuoteStatus

bp = Blueprint('main', __name__)


# ============================================================================
# KANBAN BOARD - Landing Page
# ============================================================================

@bp.route('/', methods=['GET'])
def kanban():
    """Display the Kanban board with all submissions"""
    return render_template('kanban.html')


@bp.route('/api/submissions', methods=['GET'])
def get_submissions():
    """API endpoint to get all submissions for the Kanban board"""
    try:
        submissions = get_all_submissions()
        return jsonify({'success': True, 'submissions': submissions})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# SUBMISSION DETAIL PAGE
# ============================================================================

@bp.route('/submission/<int:submission_id>', methods=['GET'])
def submission_detail(submission_id):
    """Display the submission detail page with all quotes"""
    submission = get_submission_by_id(submission_id)
    if not submission:
        return "Submission not found", 404
    return render_template('submission.html', submission_id=submission_id)


@bp.route('/api/submission/<int:submission_id>', methods=['GET'])
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

        return jsonify({
            'success': True,
            'submission': submission,
            'quotes': quotes
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# QUOTE UPLOAD & PROCESSING
# ============================================================================

@bp.route('/api/upload_quote', methods=['POST'])
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

        # Parse the document with Gemini
        try:
            result = analyze_with_gemini(filepath)
            raw_response = result.get("response", "").strip()

            # Clean markdown formatting if present
            if raw_response.startswith("```"):
                lines = raw_response.splitlines()
                if lines:
                    lines = lines[1:]  # remove ```json
                if lines and lines[-1].strip().startswith("```"):
                    lines = lines[:-1]  # remove ```
                raw_response = "\n".join(lines).strip()

            # Parse JSON
            try:
                parsed_data = json.loads(raw_response)
            except json.JSONDecodeError:
                return jsonify({
                    'success': False,
                    'error': 'Failed to parse AI response as JSON',
                    'raw_response': raw_response
                }), 500

            # Extract key fields
            insured_name = parsed_data.get('insured', {}).get('name', 'Unknown')
            carrier_name = None
            effective_date = None
            state = parsed_data.get('insured', {}).get('address', {}).get('state')

            # Try to get carrier and effective date from first policy
            if parsed_data.get('policies') and len(parsed_data['policies']) > 0:
                first_policy = parsed_data['policies'][0]
                carrier_name = first_policy.get('carrier')
                effective_date = first_policy.get('effective_date')

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

            # Create quote record
            quote_id = create_quote(
                submission_id=submission_id,
                carrier_name=carrier_name,
                raw_document_path=filepath,
                extracted_json=json.dumps(parsed_data),
                user=None  # TODO: Add user authentication
            )

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
                'parsed_data': parsed_data
            })

        except Exception as e:
            return jsonify({'success': False, 'error': f'Parsing error: {str(e)}'}), 500

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# DELETE ROUTES
# ============================================================================

@bp.route('/api/quote/<int:quote_id>', methods=['DELETE'])
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
# APPETITE SCORING
# ============================================================================

@bp.route('/api/submission/<int:submission_id>/appetite', methods=['GET'])
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


@bp.route('/api/quote/<int:quote_id>/status', methods=['PUT'])
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
def admin():
    """Display the admin page with database tables"""
    return render_template('admin.html')


@bp.route('/api/admin/data', methods=['GET'])
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
            'audit_log': audit_data
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']