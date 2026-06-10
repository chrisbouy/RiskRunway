"""
Applied Epic integration routes.

Provides endpoints for:
- Searching Epic clients
- Importing client/policy data to create a RiskRunway submission
- Exporting bound submission data back to Epic
"""
from flask import Blueprint, request, jsonify, session
from datetime import datetime
from functools import wraps
import json
import os

from app.epic_client import get_epic_client, EpicAPIError
from app.database import get_session, create_submission, log_action
from app.models import Submission, SubmissionStatus, Document

epic_bp = Blueprint('epic', __name__)


def login_required(f):
    """Require authenticated session."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'success': False, 'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated


@epic_bp.route('/api/epic/status', methods=['GET'])
@login_required
def epic_status():
    """Check if Epic integration is configured and accessible."""
    client = get_epic_client()
    return jsonify({
        'success': True,
        'configured': client.is_configured,
        'base_url': client.base_url,
    })


@epic_bp.route('/api/epic/clients/search', methods=['GET'])
@login_required
def epic_search_clients():
    """
    Search Epic clients by name.
    Query params: q (search term), limit (default 20)
    """
    client = get_epic_client()
    if not client.is_configured:
        return jsonify({'success': False, 'error': 'Epic integration not configured'}), 400

    query = (request.args.get('q') or '').strip()
    if not query or len(query) < 2:
        return jsonify({'success': False, 'error': 'Search query must be at least 2 characters'}), 400

    limit = min(int(request.args.get('limit', 20)), 100)

    try:
        clients = client.search_clients(name_contains=query, limit=limit)
        # Normalize to a simple list for the frontend
        results = []
        for c in clients:
            results.append({
                'id': c.get('id'),
                'name': c.get('name'),
                'lookup_code': c.get('lookupCode'),
                'type': c.get('type'),  # PROSPECT or INSURED
                'active': c.get('active'),
                'address': c.get('address'),
            })
        return jsonify({'success': True, 'clients': results})
    except EpicAPIError as e:
        return jsonify({'success': False, 'error': f'Epic API error: {e.detail}'}), e.status_code


@epic_bp.route('/api/epic/clients/<client_id>/policies', methods=['GET'])
@login_required
def epic_client_policies(client_id):
    """
    Get prospective policies for a given Epic client.
    Returns policies that are ready to be marketed.
    """
    client = get_epic_client()
    if not client.is_configured:
        return jsonify({'success': False, 'error': 'Epic integration not configured'}), 400

    status_filter = request.args.get('status', 'PROSPECTIVE')

    try:
        policies = client.get_policies_for_client(client_id, status=status_filter)
        results = []
        for p in policies:
            # Extract embedded data if available
            embedded = p.get('_embedded', {})
            policy_type = embedded.get('policyType', {})

            results.append({
                'id': p.get('id'),
                'description': p.get('description'),
                'policy_number': p.get('policyNumber'),
                'effective_on': p.get('effectiveOn'),
                'expiration_on': p.get('expirationOn'),
                'status': p.get('status'),
                'policy_type_code': policy_type.get('code'),
                'policy_type_description': policy_type.get('description'),
                'business_type': policy_type.get('businessType'),
            })
        return jsonify({'success': True, 'policies': results})
    except EpicAPIError as e:
        return jsonify({'success': False, 'error': f'Epic API error: {e.detail}'}), e.status_code


@epic_bp.route('/api/epic/policies/<policy_id>/lines', methods=['GET'])
@login_required
def epic_policy_lines(policy_id):
    """Get lines for a specific Epic policy."""
    client = get_epic_client()
    if not client.is_configured:
        return jsonify({'success': False, 'error': 'Epic integration not configured'}), 400

    try:
        lines = client.get_lines_for_policy(policy_id)
        results = []
        for line in lines:
            embedded = line.get('_embedded', {})
            line_type = embedded.get('lineType', {})
            issuing_company = embedded.get('issuingCompany', {})

            results.append({
                'id': line.get('id'),
                'line_type_code': line_type.get('code'),
                'line_type_description': line_type.get('description'),
                'effective_on': line.get('effectiveOn'),
                'expiration_on': line.get('expirationOn'),
                'stage': line.get('stage'),
                'issuing_company': issuing_company.get('name'),
                'bill_mode': line.get('billMode'),
            })
        return jsonify({'success': True, 'lines': results})
    except EpicAPIError as e:
        return jsonify({'success': False, 'error': f'Epic API error: {e.detail}'}), e.status_code


@epic_bp.route('/api/epic/import', methods=['POST'])
@login_required
def epic_import_submission():
    """
    Import a client/policy from Epic to create a new RiskRunway submission.

    Expected JSON body:
    {
        "client_id": "uuid",
        "client_name": "Acme Corp",
        "policy_id": "uuid",
        "line_id": "uuid",
        "effective_date": "2024-01-01",
        "expiration_date": "2025-01-01",
        "state": "NY",
        "description": "General Liability Policy",
        "line_type": "GL"
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'Request body required'}), 400

    client_name = (data.get('client_name') or '').strip()
    if not client_name:
        return jsonify({'success': False, 'error': 'client_name is required'}), 400

    effective_date = data.get('effective_date') or datetime.now().strftime('%Y-%m-%d')
    state = data.get('state')

    # Determine appropriate RR status based on Epic policy/line stage
    epic_policy_status = (data.get('policy_status') or '').upper()
    epic_line_stage = (data.get('line_stage') or '').upper()

    if epic_policy_status == 'CONTRACTED' or epic_line_stage == 'ISSUED':
        rr_status = SubmissionStatus.SENT_TO_FINANCE  # Already bound
    elif epic_line_stage in ('SUBMITTED', 'IN_PROCESS'):
        rr_status = SubmissionStatus.IN_PROGRESS  # Quoting stage
    else:
        rr_status = SubmissionStatus.RECEIVED  # Submission stage (NOT_SUBMITTED or unknown)

    # Create the submission in RiskRunway
    submission_id = create_submission(
        insured_name=client_name,
        effective_date=effective_date,
        state=state,
        user=session.get('username'),
        assigned_to=session.get('user_id'),
    )

    # Update with Epic-specific fields and correct status
    db_session = get_session()
    try:
        submission = db_session.query(Submission).filter_by(id=submission_id).first()
        submission.ams_type = 'epic'
        submission.epic_client_id = data.get('client_id')
        submission.epic_policy_id = data.get('policy_id')
        submission.epic_line_id = data.get('line_id')
        submission.status = rr_status
        # Set status label to policy type for visibility on kanban card
        policy_desc = data.get('policy_type_description') or data.get('line_type') or data.get('description') or ''
        if policy_desc:
            submission.status_label = policy_desc
        db_session.commit()
    finally:
        db_session.close()

    log_action(
        entity_type='submission',
        entity_id=submission_id,
        action='imported_from_epic',
        user=session.get('username'),
        submission_id=submission_id,
        details=json.dumps({
            'epic_client_id': data.get('client_id'),
            'epic_policy_id': data.get('policy_id'),
            'epic_line_id': data.get('line_id'),
            'line_type': data.get('line_type'),
        })
    )

    return jsonify({
        'success': True,
        'submission_id': submission_id,
        'message': f'Submission created for {client_name} from Epic',
    }), 201


@epic_bp.route('/api/epic/export/<int:submission_id>', methods=['POST'])
@login_required
def epic_export_submission(submission_id):
    """
    Export a bound submission back to Epic.

    Updates the line with carrier/premium/policy number data and attaches documents.

    Expected JSON body (optional overrides):
    {
        "issuing_company_code": "ACEIN1",
        "policy_number": "GL-2024-001",
        "estimated_premium": 5000.00,
        "status_code": "BND"
    }
    """
    client = get_epic_client()
    if not client.is_configured:
        return jsonify({'success': False, 'error': 'Epic integration not configured'}), 400

    db_session = get_session()
    try:
        submission = db_session.query(Submission).filter_by(id=submission_id).first()
        if not submission:
            return jsonify({'success': False, 'error': 'Submission not found'}), 404

        if submission.ams_type != 'epic':
            return jsonify({'success': False, 'error': 'Submission is not linked to Epic'}), 400

        if not submission.epic_line_id or not submission.epic_policy_id:
            return jsonify({'success': False, 'error': 'Missing Epic line or policy ID'}), 400

        # Get override data from request
        data = request.get_json() or {}

        # Build line update payload
        line_update = {}
        if data.get('issuing_company_code'):
            line_update['IssuingCompanyLookupCode'] = data['issuing_company_code']
        if data.get('estimated_premium') is not None:
            line_update['EstimatedPremium'] = data['estimated_premium']
        if data.get('status_code'):
            line_update['StatusCode'] = data['status_code']

        # Build policy update payload
        policy_update = {}
        if data.get('policy_number'):
            policy_update['PolicyNumber'] = data['policy_number']
        if data.get('description'):
            policy_update['Description'] = data['description']

        # Collect documents to attach
        documents_to_attach = []
        docs = db_session.query(Document).filter_by(
            submission_id=submission_id,
            is_active=True
        ).all()

        for doc in docs:
            if doc.storage_provider == 'local' and os.path.exists(doc.storage_key):
                documents_to_attach.append({
                    'filepath': doc.storage_key,
                    'filename': doc.original_filename,
                    'content_type': doc.content_type or 'application/pdf',
                    'description': f"{doc.document_type.value}: {doc.original_filename}",
                })
            elif doc.storage_provider == 's3':
                # For S3 docs, we'd need to download first — skip for now in mock
                documents_to_attach.append({
                    'filepath': None,  # Signal that upload will be skipped
                    'filename': doc.original_filename,
                    'content_type': doc.content_type or 'application/pdf',
                    'description': f"{doc.document_type.value}: {doc.original_filename}",
                })

        # Execute the export
        try:
            results = client.export_submission_to_epic(
                epic_policy_id=submission.epic_policy_id,
                epic_line_id=submission.epic_line_id,
                line_update_data=line_update if line_update else None,
                policy_update_data=policy_update if policy_update else None,
                documents=documents_to_attach if documents_to_attach else None,
            )
        except EpicAPIError as e:
            return jsonify({
                'success': False,
                'error': f'Epic API error during export: {e.detail}',
                'status_code': e.status_code,
            }), 500

        # Mark submission as exported
        submission.epic_exported_at = datetime.utcnow()
        db_session.commit()

        log_action(
            entity_type='submission',
            entity_id=submission_id,
            action='exported_to_epic',
            user=session.get('username'),
            submission_id=submission_id,
            details=json.dumps(results)
        )

        return jsonify({
            'success': True,
            'message': 'Submission exported to Epic successfully',
            'results': results,
        })

    finally:
        db_session.close()
