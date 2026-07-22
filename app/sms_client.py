# app/sms_client.py
"""
Twilio SMS integration for Risk Runway.
Handles sending alerts to agents and processing inbound replies.
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict
from twilio.rest import Client
from twilio.request_validator import RequestValidator

from app.models import (
    SmsAlert, SmsAlertStatus, User, EmailMessage, Submission
)
from app.database import get_session

logger = logging.getLogger(__name__)


class SmsClient:
    """
    Sends SMS alerts to agents and manages the conversation state machine.
    """

    # How long an alert waits for a reply before expiring
    ALERT_EXPIRY_HOURS = 4

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.client = Client(account_sid, auth_token)
        self.from_number = from_number
        self.validator = RequestValidator(auth_token)

    def send_alert(
        self,
        user: User,
        message: str,
        email_id: Optional[int] = None,
        submission_id: Optional[int] = None,
        provider_message_id: Optional[str] = None,
        connected_account_id: Optional[int] = None
    ) -> Optional[SmsAlert]:
        """
        Send an SMS alert to an agent and create a tracking record.
        Returns the SmsAlert record, or None if the user has no phone number.
        """
        if not user.phone_number or not user.sms_alerts_enabled:
            logger.debug(f"User {user.id} has no phone or SMS disabled, skipping alert")
            return None

        db_session = get_session()
        try:
            # Send via Twilio
            twilio_message = self.client.messages.create(
                body=message,
                from_=self.from_number,
                to=user.phone_number
            )

            # Create tracking record
            alert = SmsAlert(
                user_id=user.id,
                email_id=email_id,
                submission_id=submission_id,
                alert_text=message,
                status=SmsAlertStatus.SENT,
                outbound_sid=twilio_message.sid,
                provider_message_id=provider_message_id,
                connected_account_id=connected_account_id,
                expires_at=datetime.utcnow() + timedelta(hours=self.ALERT_EXPIRY_HOURS)
            )
            db_session.add(alert)
            db_session.commit()

            logger.info(f"SMS alert sent to {user.phone_number} (SID: {twilio_message.sid})")
            return alert

        except Exception as e:
            logger.error(f"Failed to send SMS to {user.phone_number}: {e}")
            db_session.rollback()
            return None
        finally:
            db_session.close()

    def handle_inbound(self, from_number: str, body: str, message_sid: str) -> str:
        """
        Process an inbound SMS reply from an agent.
        Returns the response text to send back.

        Commands:
            1 / PROCESS     - Download attachment(s) and run through quote parser
            2 / SKIP        - Dismiss this alert
            MORE / SUMMARY  - Get AI summary of the email
            DRAFT           - Generate a draft reply
            SEND            - Approve and send the draft
            DONE            - Move submission card to next stage
        """
        db_session = get_session()
        try:
            # Find the user by phone number
            user = db_session.query(User).filter_by(phone_number=from_number).first()
            if not user:
                return "This number isn't registered with Risk Runway. Connect your phone in Settings."

            # Find their most recent pending alert
            alert = db_session.query(SmsAlert).filter(
                SmsAlert.user_id == user.id,
                SmsAlert.status.in_([SmsAlertStatus.SENT, SmsAlertStatus.DRAFT_SENT])
            ).order_by(SmsAlert.created_at.desc()).first()

            if not alert:
                return "No pending alerts. You're all caught up."

            command = body.strip().upper()
            alert.agent_reply = body
            alert.replied_at = datetime.utcnow()
            alert.inbound_sid = message_sid

            # === 1) PROCESS — ingest quote attachment(s) ===
            if command in ('1', 'PROCESS'):
                alert.status = SmsAlertStatus.EXECUTED
                alert.executed_at = datetime.utcnow()
                db_session.commit()
                return self._process_quote(alert, db_session)

            # === 2) SKIP ===
            elif command in ('2', 'SKIP', 'NO', 'IGNORE'):
                alert.status = SmsAlertStatus.SKIPPED
                db_session.commit()
                return "Got it, skipped."

            elif command in ('MORE', 'SUMMARY', 'DETAIL', 'DETAILS'):
                alert.status = SmsAlertStatus.REPLIED
                db_session.commit()
                return self._generate_email_summary(alert, db_session)

            elif command in ('DRAFT', 'REPLY'):
                alert.status = SmsAlertStatus.REPLIED
                db_session.commit()
                draft = self._generate_draft_reply(alert, user, db_session)
                if draft:
                    alert.draft_reply = draft
                    alert.status = SmsAlertStatus.DRAFT_SENT
                    db_session.commit()
                    truncated = draft[:1500] + "..." if len(draft) > 1500 else draft
                    return f"Draft reply:\n\n{truncated}\n\nReply SEND to send it, or 2 to discard."
                return "Couldn't generate a draft. The email may not have enough context."

            elif command in ('SEND', 'YES', 'APPROVE'):
                if alert.status == SmsAlertStatus.DRAFT_SENT and alert.draft_reply:
                    success = self._send_draft_email(alert, user, db_session)
                    if success:
                        alert.status = SmsAlertStatus.EXECUTED
                        alert.executed_at = datetime.utcnow()
                        db_session.commit()
                        return "Sent! The reply has been emailed."
                    return "Failed to send. Try again or handle it from Risk Runway."
                return "No draft to send. Reply DRAFT first to generate one."

            elif command in ('DONE', 'BIND', 'NEXT'):
                alert.status = SmsAlertStatus.EXECUTED
                alert.executed_at = datetime.utcnow()
                db_session.commit()
                return self._advance_submission(alert, db_session)

            else:
                # Unrecognized command
                alert.status = SmsAlertStatus.REPLIED
                db_session.commit()
                return "Reply 1 to Process, 2 to Skip. Other commands: MORE, DRAFT, DONE"

        except Exception as e:
            logger.error(f"Error handling inbound SMS from {from_number}: {e}")
            db_session.rollback()
            return "Something went wrong. Try again or check Risk Runway."
        finally:
            db_session.close()

    def validate_webhook(self, url: str, params: dict, signature: str) -> bool:
        """Validate that an inbound webhook actually came from Twilio."""
        return self.validator.validate(url, params, signature)

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _process_quote(self, alert: SmsAlert, db_session) -> str:
        """
        Re-fetch the email's attachment(s) from Outlook on demand and run through the quote parser.
        No email content is stored in the database — we fetch live from the provider.
        """
        if not alert.provider_message_id or not alert.connected_account_id:
            return "No email linked to this alert — can't process."

        submission_id = alert.submission_id
        if not submission_id:
            return "No submission linked — can't determine where to file this quote."

        try:
            from flask import current_app
            from app.models import ConnectedAccount, EmailProvider, Quote, QuoteStatus
            from app.oauth_services import get_oauth_service
            from datetime import timedelta
            import json as json_module
            import uuid

            # Get the connected account
            account = db_session.query(ConnectedAccount).filter_by(id=alert.connected_account_id).first()
            if not account:
                return "Email account not found. May need to reconnect."

            # Get OAuth service and refresh token
            config = {
                'GMAIL_CLIENT_ID': current_app.config.get('GMAIL_CLIENT_ID', ''),
                'GMAIL_CLIENT_SECRET': current_app.config.get('GMAIL_CLIENT_SECRET', ''),
                'GMAIL_REDIRECT_URI': current_app.config.get('GMAIL_REDIRECT_URI', ''),
                'MICROSOFT_CLIENT_ID': current_app.config.get('MICROSOFT_CLIENT_ID', ''),
                'MICROSOFT_CLIENT_SECRET': current_app.config.get('MICROSOFT_CLIENT_SECRET', ''),
                'MICROSOFT_REDIRECT_URI': current_app.config.get('MICROSOFT_REDIRECT_URI', ''),
                'MICROSOFT_TENANT_ID': current_app.config.get('MICROSOFT_TENANT_ID', 'common'),
            }

            provider_name = 'gmail' if account.provider == EmailProvider.GMAIL else 'outlook'
            service = get_oauth_service(provider_name, config)

            tokens = account.get_decrypted_tokens()
            access_token = tokens.get('access_token')
            refresh_token = tokens.get('refresh_token')

            # Refresh token
            if refresh_token:
                try:
                    new_tokens = service.refresh_access_token(refresh_token)
                    account.set_encrypted_tokens(new_tokens)
                    access_token = new_tokens.get('access_token')
                    db_session.commit()
                except Exception:
                    pass

            if not access_token:
                return "Email token expired. Reconnect your email in Risk Runway."

            # Fetch attachments for this specific message
            attachments = service.get_message_attachments(
                access_token=access_token,
                message_id=alert.provider_message_id
            )

            if not attachments:
                return "No attachments found on this email."

            # Filter to quote-like files
            quote_attachments = [
                att for att in attachments
                if att.get('filename', '').lower().endswith(('.pdf', '.xlsx', '.xls', '.docx', '.doc'))
            ]

            if not quote_attachments:
                return "No PDF/Excel attachments to parse."

            upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
            os.makedirs(upload_folder, exist_ok=True)

            processed = 0
            errors = []

            for att in quote_attachments:
                try:
                    # Download attachment content
                    content = service.download_attachment(
                        access_token=access_token,
                        message_id=alert.provider_message_id,
                        attachment_id=att.get('attachment_id') or att.get('id')
                    )

                    if not content:
                        errors.append(f"{att.get('filename')}: download failed")
                        continue

                    # Save to temp file
                    safe_filename = f"{uuid.uuid4()}_{att.get('filename', 'doc.pdf')}"
                    file_path = os.path.join(upload_folder, safe_filename)
                    with open(file_path, 'wb') as f:
                        f.write(content)

                    # Run through quote parser
                    from app.parsers.two_pass_parser import process_quote_two_pass

                    result = process_quote_two_pass(file_path, [])

                    if result and result.get('pass2_normalized'):
                        parsed_data = result['pass2_normalized']
                        carrier_name = None
                        if parsed_data.get('policies') and len(parsed_data['policies']) > 0:
                            first_policy = parsed_data['policies'][0]
                            carrier_name = first_policy.get('carrier')

                        quote = Quote(
                            submission_id=submission_id,
                            carrier_name=carrier_name,
                            raw_document_path=file_path,
                            extracted_json=json_module.dumps(parsed_data),
                            pass1_layout_json=json_module.dumps(result.get('pass1_layout')) if result.get('pass1_layout') else None,
                            status=QuoteStatus.RECEIVED
                        )
                        db_session.add(quote)
                        db_session.commit()
                        processed += 1
                    else:
                        # Clean up temp file if parsing failed
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        errors.append(f"{att.get('filename')}: parsing returned no data")

                except Exception as e:
                    logger.error(f"Error processing attachment {att.get('filename')}: {e}")
                    errors.append(f"{att.get('filename')}: {str(e)[:50]}")

            if processed > 0 and not errors:
                return f"✅ Processed {processed} quote{'s' if processed > 1 else ''}. Check Risk Runway for details."
            elif processed > 0 and errors:
                return f"✅ Processed {processed}, but {len(errors)} failed: {'; '.join(errors[:2])}"
            else:
                return f"❌ Processing failed: {'; '.join(errors[:2])}"

        except Exception as e:
            logger.error(f"Error in _process_quote: {e}")
            return f"❌ Error: {str(e)[:100]}"

    def _download_attachment(self, attachment, email_msg, db_session) -> Optional[str]:
        """
        Download an attachment from the email provider (OAuth).
        Returns the local file path, or None on failure.
        """
        import os

        # Find the connected account that fetched this email
        connected_account_id = email_msg.connected_account_id
        if not connected_account_id:
            logger.warning(f"No connected account for email {email_msg.id}, can't download attachment")
            return None

        from app.models import ConnectedAccount
        account = db_session.query(ConnectedAccount).filter_by(id=connected_account_id).first()
        if not account:
            return None

        try:
            from flask import current_app
            config = {
                'GMAIL_CLIENT_ID': current_app.config.get('GMAIL_CLIENT_ID', ''),
                'GMAIL_CLIENT_SECRET': current_app.config.get('GMAIL_CLIENT_SECRET', ''),
                'GMAIL_REDIRECT_URI': current_app.config.get('GMAIL_REDIRECT_URI', ''),
                'MICROSOFT_CLIENT_ID': current_app.config.get('MICROSOFT_CLIENT_ID', ''),
                'MICROSOFT_CLIENT_SECRET': current_app.config.get('MICROSOFT_CLIENT_SECRET', ''),
                'MICROSOFT_REDIRECT_URI': current_app.config.get('MICROSOFT_REDIRECT_URI', ''),
            }

            from app.oauth_services import get_oauth_service
            from app.models import EmailProvider

            provider_name = 'gmail' if account.provider == EmailProvider.GMAIL else 'outlook'
            service = get_oauth_service(provider_name, config)

            tokens = account.get_decrypted_tokens()
            access_token = tokens.get('access_token')

            # Refresh if needed
            refresh_token = tokens.get('refresh_token')
            if refresh_token:
                try:
                    new_tokens = service.refresh_access_token(refresh_token)
                    account.set_encrypted_tokens(new_tokens)
                    access_token = new_tokens.get('access_token')
                    db_session.commit()
                except Exception:
                    pass

            if not access_token:
                return None

            # Download the attachment content
            content = service.download_attachment(
                access_token=access_token,
                message_id=attachment.message_id or email_msg.message_id,
                attachment_id=attachment.attachment_id
            )

            if not content:
                return None

            # Save to uploads folder
            upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
            import uuid
            safe_filename = f"{uuid.uuid4()}_{attachment.filename}"
            file_path = os.path.join(upload_folder, safe_filename)
            os.makedirs(upload_folder, exist_ok=True)

            with open(file_path, 'wb') as f:
                f.write(content)

            return file_path

        except Exception as e:
            logger.error(f"Failed to download attachment {attachment.filename}: {e}")
            return None

    def _generate_email_summary(self, alert: SmsAlert, db_session) -> str:
        """
        Generate a short SMS-friendly summary of the email.
        Uses template for now — swap in Haiku call later for richer summaries.
        """
        if not alert.email_id:
            return "No email attached to this alert."

        email = db_session.query(EmailMessage).filter_by(id=alert.email_id).first()
        if not email:
            return "Email not found."

        # Simple template summary (no AI tokens spent)
        parts = []
        parts.append(f"From: {email.from_name or email.from_email}")
        parts.append(f"Subject: {email.subject}")
        if email.attachment_count > 0:
            parts.append(f"Attachments: {email.attachment_count}")
        if email.body_text:
            # First 300 chars of body
            preview = email.body_text[:300].replace('\n', ' ').strip()
            parts.append(f"\n{preview}...")

        return '\n'.join(parts)

    def _generate_draft_reply(self, alert: SmsAlert, user: User, db_session) -> Optional[str]:
        """
        Generate an AI draft reply to the email.
        TODO: Wire in Bedrock/Haiku call here.
        For now returns a placeholder.
        """
        if not alert.email_id:
            return None

        email = db_session.query(EmailMessage).filter_by(id=alert.email_id).first()
        if not email:
            return None

        # Placeholder — replace with actual LLM call
        # The LLM prompt would include: email body, agent's signature, submission context
        signature = user.signature or user.full_name
        return (
            f"Hi {email.from_name or 'there'},\n\n"
            f"Thank you for sending this over. I'll review and get back to you shortly.\n\n"
            f"Best,\n{signature}"
        )

    def _send_draft_email(self, alert: SmsAlert, user: User, db_session) -> bool:
        """
        Send the approved draft reply via Resend.
        TODO: Wire in Resend send here using existing infrastructure.
        """
        if not alert.email_id or not alert.draft_reply:
            return False

        email = db_session.query(EmailMessage).filter_by(id=alert.email_id).first()
        if not email:
            return False

        try:
            # TODO: Use Resend to send email as the agent
            # For now, log the intent
            logger.info(
                f"[SMS DRAFT SEND] Would send reply to {email.from_email} "
                f"on behalf of user {user.id}: {alert.draft_reply[:100]}..."
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send draft email: {e}")
            return False

    def _advance_submission(self, alert: SmsAlert, db_session) -> str:
        """Move the submission to the next kanban stage."""
        if not alert.submission_id:
            return "No submission linked to this alert."

        submission = db_session.query(Submission).filter_by(id=alert.submission_id).first()
        if not submission:
            return "Submission not found."

        # Advance to next status
        status_order = [
            SubmissionStatus.RECEIVED,
            SubmissionStatus.IN_PROGRESS,
            SubmissionStatus.CHOSEN,
            SubmissionStatus.SENT_TO_FINANCE
        ]

        from app.models import SubmissionStatus
        current_idx = next(
            (i for i, s in enumerate(status_order) if s == submission.status), -1
        )

        if current_idx < len(status_order) - 1:
            new_status = status_order[current_idx + 1]
            submission.status = new_status
            db_session.commit()
            return f"Moved '{submission.insured_name}' to {new_status.value}."
        else:
            return f"'{submission.insured_name}' is already at the final stage."


def create_sms_client(config: Dict) -> Optional[SmsClient]:
    """
    Factory function. Returns None if Twilio isn't configured.
    """
    account_sid = config.get('TWILIO_ACCOUNT_SID', '')
    auth_token = config.get('TWILIO_AUTH_TOKEN', '')
    from_number = config.get('TWILIO_PHONE_NUMBER', '')

    if not all([account_sid, auth_token, from_number]):
        logger.warning("[SMS] Twilio not configured — SMS alerts disabled")
        return None

    return SmsClient(account_sid, auth_token, from_number)


def build_email_alert_text(email_msg: EmailMessage, submission: Optional[Submission] = None) -> str:
    """
    Build the alert text for an incoming email match.
    Cheap, no AI — just a formatted template.
    """
    parts = []

    if submission:
        parts.append(f"📬 {submission.insured_name}")
    else:
        parts.append("📬 New email match")

    from_display = email_msg.from_name or email_msg.from_email
    parts.append(f"Quote from {from_display}")

    if email_msg.attachment_count > 0:
        parts.append(f"({email_msg.attachment_count} attachment{'s' if email_msg.attachment_count > 1 else ''})")

    if email_msg.subject:
        subj = email_msg.subject[:60] + "..." if len(email_msg.subject) > 60 else email_msg.subject
        parts.append(f"Re: {subj}")

    parts.append("")
    parts.append("Reply:")
    parts.append("1) Process")
    parts.append("2) Skip")

    return '\n'.join(parts)
