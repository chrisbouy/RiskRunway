# app/email_client.py
"""
Unified email client that handles fetching emails from OAuth providers
and processing attachments through the existing quote parsing pipeline.
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from sqlalchemy.orm import Session

from app.models import (
    ConnectedAccount, ConnectedAccountStatus, EmailProvider,
    EmailMessage, EmailAttachment, Submission
)
from app.oauth_services import (
    get_oauth_service, get_unified_email_data,
    decrypt_token, GmailOAuthService, OutlookOAuthService
)
from app.database import get_session

logger = logging.getLogger(__name__)


class EmailClient:
    """
    Unified email client that fetches emails from connected accounts
    and processes attachments through the quote parsing pipeline.
    """
    
    # Keywords that indicate a quote-related email
    QUOTE_KEYWORDS = ['quote', 'proposal', 'renewal', 'premium', 'indication', 'pricing', 'coverage']
    
    def __init__(self, config: Dict):
        self.config = config
    
    def fetch_and_process_emails(
        self,
        connected_account_id: int,
        max_results: int = 50,
        since_days: int = 30
    ) -> Dict:
        """
        Fetch emails from a connected account and process them.
        """
        db_session = get_session()
        
        try:
            # Get connected account
            account = db_session.query(ConnectedAccount).filter(
                ConnectedAccount.id == connected_account_id
            ).first()
            
            if not account:
                return {'success': False, 'error': 'Connected account not found'}
            
            if account.status != ConnectedAccountStatus.ACTIVE:
                return {'success': False, 'error': f'Account status is {account.status.value}'}
            
            # Get OAuth service
            service = get_oauth_service(account.provider.value, self.config)
            
            # Get decrypted tokens
            tokens = account.get_decrypted_tokens()
            access_token = tokens.get('access_token')
            
            if not access_token:
                return {'success': False, 'error': 'No access token available'}
            
            # Check if token needs refresh
            if account.expires_at and account.expires_at < datetime.utcnow():
                # Refresh token
                try:
                    new_tokens = service.refresh_access_token(tokens.get('refresh_token'))
                    account.set_encrypted_tokens(new_tokens)
                    db_session.commit()
                    access_token = new_tokens.get('access_token')
                except Exception as e:
                    logger.error(f"Failed to refresh token: {e}")
                    account.status = ConnectedAccountStatus.ERROR
                    account.last_error = str(e)
                    db_session.commit()
                    return {'success': False, 'error': f'Token refresh failed: {str(e)}'}
            
            # Calculate since date
            since_date = datetime.utcnow() - timedelta(days=since_days)
            
            # Fetch emails
            emails = service.fetch_emails(
                access_token=access_token,
                max_results=max_results,
                since_date=since_date
            )
            
            processed = 0
            matched = 0
            new_emails = 0
            
            # Get active submissions
            active_submissions = self._get_active_submissions(db_session)
            
            # Get existing message IDs
            existing_message_ids = set(
                row[0] for row in db_session.query(EmailMessage.message_id).all()
            )
            
            for unified_email in emails:
                # Skip if already processed
                if unified_email.message_id in existing_message_ids:
                    continue
                
                processed += 1
                
                # Try to match with submission
                submission, keywords = self._match_submission(
                    unified_email.subject,
                    unified_email.body_text or '',
                    unified_email.attachments,
                    active_submissions
                )
                
                # Save to database if matched or has attachments
                if submission or unified_email.attachments:
                    email_record = EmailMessage(
                        submission_id=submission.id if submission else None,
                        connected_account_id=account.id,
                        message_id=unified_email.message_id,
                        from_email=unified_email.from_email,
                        from_name=unified_email.from_name,
                        to_email=unified_email.to_email,
                        subject=unified_email.subject,
                        body_text=unified_email.body_text,
                        body_html=unified_email.body_html,
                        received_date=unified_email.date,
                        has_attachments=len(unified_email.attachments) > 0,
                        attachment_count=len(unified_email.attachments),
                        matched_insured_name='insured_name' in ','.join(keywords) if keywords else False,
                        matched_quote_attachment=self._has_quote_attachment(unified_email.attachments),
                        matched_keywords=','.join(keywords) if keywords else None
                    )
                    
                    db_session.add(email_record)
                    db_session.flush()
                    
                    # Process attachments
                    for att in unified_email.attachments:
                        self._process_attachment(
                            service=service,
                            account=account,
                            email_record=email_record,
                            attachment_info=att,
                            db_session=db_session
                        )
                    
                    new_emails += 1
                    if submission:
                        matched += 1
            
            # Update last sync time
            account.last_sync_at = datetime.utcnow()
            account.last_error = None
            db_session.commit()
            
            return {
                'success': True,
                'processed': processed,
                'matched': matched,
                'new_emails': new_emails
            }
            
        except Exception as e:
            logger.error(f"Error fetching emails: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            db_session.close()
    
    def _get_active_submissions(self, db_session: Session) -> List[Submission]:
        """
        Get active submissions (IN_PROGRESS or bound with renewal <= 120 days).
        """
        from app.models import SubmissionStatus
        
        active_submissions = []
        all_submissions = db_session.query(Submission).all()
        
        for sub in all_submissions:
            if sub.status == SubmissionStatus.IN_PROGRESS:
                active_submissions.append(sub)
            elif sub.status in (SubmissionStatus.CHOSEN, SubmissionStatus.SENT_TO_FINANCE):
                if sub.effective_date:
                    try:
                        effective_date = datetime.strptime(str(sub.effective_date)[:10], '%Y-%m-%d')
                        days_until = (effective_date - datetime.now()).days
                        if days_until <= 120:
                            active_submissions.append(sub)
                    except:
                        pass
        
        return active_submissions
    
    def _match_submission(
        self,
        subject: str,
        body_text: str,
        attachments: List[Dict],
        active_submissions: List[Submission]
    ) -> tuple:
        """
        Match email to a submission based on insured name, attachments, and keywords.
        """
        import re
        
        subject_lower = (subject or '').lower()
        body_lower = (body_text or '').lower()
        combined_text = f"{subject_lower} {body_lower}"
        
        matched_keywords = [kw for kw in self.QUOTE_KEYWORDS if kw in combined_text]
        
        # Check for PDF/Excel attachments
        has_quote_attachment = self._has_quote_attachment(attachments)
        
        # Try to match by insured name
        for submission in active_submissions:
            insured_name = (submission.insured_name or '').strip()
            if not insured_name:
                continue
            
            # Fuzzy match
            name_words = [w.lower() for w in re.findall(r'\w+', insured_name) if len(w) > 3]
            if not name_words:
                continue
            
            matches = sum(1 for word in name_words if word in combined_text)
            match_ratio = matches / len(name_words) if name_words else 0
            
            if match_ratio > 0.5:
                matched_keywords.append('insured_name_match')
                return submission, matched_keywords
            
            if len(insured_name) > 5 and insured_name.lower() in combined_text:
                matched_keywords.append('insured_name_exact')
                return submission, matched_keywords
        
        return None, matched_keywords
    
    def _has_quote_attachment(self, attachments: List[Dict]) -> bool:
        """Check if any attachment is a quote document (PDF/Excel)."""
        for att in attachments:
            filename = att.get('filename', '').lower()
            if filename.endswith(('.pdf', '.xlsx', '.xls', '.docx', '.doc')):
                return True
        return False
    
    def _process_attachment(
        self,
        service,
        account: ConnectedAccount,
        email_record: EmailMessage,
        attachment_info: Dict,
        db_session: Session
    ):
        """
        Process an email attachment - save to disk and trigger parsing.
        """
        import io
        
        filename = attachment_info.get('filename', '')
        content_type = attachment_info.get('content_type', '')
        
        # Skip non-PDF files
        if not filename.lower().endswith('.pdf'):
            logger.info(f"Skipping non-PDF attachment: {filename}")
            return
        
        # Create attachments directory
        attachments_dir = os.path.join('data', 'email_attachments', str(email_record.id))
        os.makedirs(attachments_dir, exist_ok=True)
        
        # Save file path
        file_path = os.path.join(attachments_dir, filename)
        
        # Download attachment data
        attachment_data = None
        
        if account.provider == EmailProvider.GMAIL:
            try:
                attachment_data = service.fetch_attachments(
                    access_token=account.get_decrypted_tokens().get('access_token'),
                    message_id=attachment_info.get('message_id'),
                    attachment_id=attachment_info.get('attachment_id')
                )
            except Exception as e:
                logger.error(f"Failed to fetch Gmail attachment: {e}")
        
        elif account.provider == EmailProvider.OUTLOOK:
            try:
                attachment_data = service.fetch_attachments(
                    access_token=account.get_decrypted_tokens().get('access_token'),
                    message_id=attachment_info.get('message_id'),
                    attachment_id=attachment_info.get('attachment_id')
                )
            except Exception as e:
                logger.error(f"Failed to fetch Outlook attachment: {e}")
        
        if attachment_data:
            with open(file_path, 'wb') as f:
                f.write(attachment_data)
            
            # Create database record
            attachment = EmailAttachment(
                email_id=email_record.id,
                filename=filename,
                content_type=content_type,
                size_bytes=len(attachment_data),
                file_path=file_path
            )
            db_session.add(attachment)
            
            logger.info(f"Saved attachment: {filename} to {file_path}")
    
    def ingest_attachment(self, email_attachment_id: int, submission_id: int) -> Dict:
        """
        Process an email attachment through the existing quote parsing pipeline.
        """
        from app.parsers.application_parser import process_document
        
        db_session = get_session()
        
        try:
            attachment = db_session.query(EmailAttachment).filter(
                EmailAttachment.id == email_attachment_id
            ).first()
            
            if not attachment:
                return {'success': False, 'error': 'Attachment not found'}
            
            if not attachment.file_path or not os.path.exists(attachment.file_path):
                return {'success': False, 'error': 'Attachment file not found'}
            
            # Process through existing parser
            result = process_document(
                file_path=attachment.file_path,
                submission_id=submission_id,
                document_type='quote',
                carrier_name=None
            )
            
            return {
                'success': True,
                'result': result
            }
            
        except Exception as e:
            logger.error(f"Error ingesting attachment: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            db_session.close()


def create_email_client(config: Dict) -> EmailClient:
    """Factory function to create email client."""
    return EmailClient(config)
