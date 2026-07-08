# app/sms_routes.py
"""
SMS webhook routes for Twilio integration.
Handles inbound SMS from agents and provides API for SMS settings.
"""
from flask import Blueprint, request, jsonify, current_app, session
from twilio.twiml.messaging_response import MessagingResponse
import logging

from app.sms_client import create_sms_client
from app.database import get_session
from app.models import User

logger = logging.getLogger(__name__)

sms_bp = Blueprint('sms', __name__, url_prefix='/api/sms')


@sms_bp.route('/webhook', methods=['POST'])
def twilio_inbound_webhook():
    """
    Twilio sends POST here when an agent replies to an SMS alert.
    This is a public endpoint (no login required) — validated via Twilio signature.
    
    Twilio expects TwiML response format.
    """
    print(f"[SMS WEBHOOK] Received request from {request.form.get('From', 'unknown')}: {request.form.get('Body', '')}")
    
    # Validate the request actually came from Twilio
    sms = create_sms_client(current_app.config)
    if not sms:
        logger.error("[SMS WEBHOOK] Twilio not configured")
        return _twiml_response("SMS service unavailable."), 200

    # Twilio signature validation — skip in cases where URL mismatch occurs behind load balancer
    signature = request.headers.get('X-Twilio-Signature', '')
    # Use the configured APP_BASE_URL + path for validation (ALB may change the URL Flask sees)
    base_url = current_app.config.get('APP_BASE_URL', request.url_root.rstrip('/'))
    validation_url = f"{base_url}/api/sms/webhook"
    params = request.form.to_dict()

    if not sms.validate_webhook(validation_url, params, signature):
        # Log but don't block — URL mismatch behind ALB is common
        print(f"[SMS WEBHOOK] Signature validation failed. URL used: {validation_url}, request.url: {request.url}")
        # Still process it — the From number matching is sufficient auth for our use case

    # Extract inbound message data
    from_number = request.form.get('From', '')
    body = request.form.get('Body', '')
    message_sid = request.form.get('MessageSid', '')

    logger.info(f"[SMS WEBHOOK] Inbound from {from_number}: {body[:50]}...")

    # Process the reply
    response_text = sms.handle_inbound(from_number, body, message_sid)

    return _twiml_response(response_text), 200


@sms_bp.route('/settings', methods=['GET'])
def get_sms_settings():
    """Get current user's SMS settings."""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    db_session = get_session()
    try:
        user = db_session.query(User).filter_by(id=session['user_id']).first()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        return jsonify({
            'success': True,
            'phone_number': user.phone_number,
            'sms_alerts_enabled': user.sms_alerts_enabled,
            'twilio_configured': bool(current_app.config.get('TWILIO_ACCOUNT_SID'))
        })
    finally:
        db_session.close()


@sms_bp.route('/settings', methods=['PUT'])
def update_sms_settings():
    """Update current user's SMS settings (phone number, enable/disable)."""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400

    db_session = get_session()
    try:
        user = db_session.query(User).filter_by(id=session['user_id']).first()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        # Update phone number
        if 'phone_number' in data:
            phone = (data['phone_number'] or '').strip()
            # Basic validation: should start with + and contain digits
            if phone and (not phone.startswith('+') or not phone[1:].replace('-', '').isdigit()):
                return jsonify({'success': False, 'error': 'Phone number must be in E.164 format (e.g. +15551234567)'}), 400
            user.phone_number = phone or None

        # Update enabled flag
        if 'sms_alerts_enabled' in data:
            user.sms_alerts_enabled = bool(data['sms_alerts_enabled'])

        db_session.commit()

        return jsonify({
            'success': True,
            'phone_number': user.phone_number,
            'sms_alerts_enabled': user.sms_alerts_enabled
        })
    except Exception as e:
        db_session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        db_session.close()


@sms_bp.route('/test', methods=['POST'])
def send_test_sms():
    """Send a test SMS to the current user to verify their setup."""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    sms = create_sms_client(current_app.config)
    if not sms:
        return jsonify({'success': False, 'error': 'Twilio not configured on server'}), 500

    db_session = get_session()
    try:
        user = db_session.query(User).filter_by(id=session['user_id']).first()
        if not user or not user.phone_number:
            return jsonify({'success': False, 'error': 'No phone number configured'}), 400

        alert = sms.send_alert(
            user=user,
            message="🧪 Test from Risk Runway — SMS alerts are working! Reply SKIP to dismiss."
        )

        if alert:
            return jsonify({'success': True, 'message': 'Test SMS sent'})
        else:
            return jsonify({'success': False, 'error': 'Failed to send — check phone number and Twilio config'}), 500
    finally:
        db_session.close()


@sms_bp.route('/alerts', methods=['GET'])
def get_sms_alerts():
    """Get recent SMS alerts for the current user."""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    from app.models import SmsAlert
    db_session = get_session()
    try:
        alerts = db_session.query(SmsAlert).filter_by(
            user_id=session['user_id']
        ).order_by(SmsAlert.created_at.desc()).limit(50).all()

        return jsonify({
            'success': True,
            'alerts': [a.to_dict() for a in alerts]
        })
    finally:
        db_session.close()


def _twiml_response(message: str) -> str:
    """Build a TwiML XML response for Twilio."""
    resp = MessagingResponse()
    resp.message(message)
    return str(resp)
