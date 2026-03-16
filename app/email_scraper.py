# app/email_scraper.py
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import os
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from sqlalchemy.orm import Session
from app.models import Submission, EmailMessage, EmailAttachment, SubmissionStatus
from app.database import get_session
import logging

logger = logging.getLogger(__name__)


class EmailScraper:
    """
    Scrapes emails from IMAP server and matches them to submissions.
    """
    
    def __init__(self, imap_server: str, email_address: str, password: str, use_ssl: bool = True):
        self.imap_server = imap_server
        self.email_address = email_address
        self.password = password
        self.use_ssl = use_ssl
        self.mail = None
        
    def connect(self):
        """Connect to IMAP server."""
        try:
            if self.use_ssl:
                self.mail = imaplib.IMAP4_SSL(self.imap_server)
            else:
                self.mail = imaplib.IMAP4(self.imap_server)
            
            self.mail.login(self.email_address, self.password)
            logger.info(f"Connected to IMAP server: {self.imap_server}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to IMAP: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from IMAP server."""
        if self.mail:
            try:
                self.mail.logout()
            except:
                pass
    
    def decode_header_value(self, value: str) -> str:
        """Decode email header value."""
        if not value:
            return ""
        
        decoded_parts = decode_header(value)
        result = []
        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                result.append(part.decode(encoding or 'utf-8', errors='ignore'))
            else:
                result.append(part)
        return ' '.join(result)
    
    def extract_email_body(self, msg) -> Tuple[Optional[str], Optional[str]]:
        """Extract text and HTML body from email message."""
        text_body = None
        html_body = None
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                
                # Skip attachments
                if "attachment" in content_disposition:
                    continue
                
                try:
                    body = part.get_payload(decode=True)
                    if body:
                        body = body.decode('utf-8', errors='ignore')
                        if content_type == "text/plain" and not text_body:
                            text_body = body
                        elif content_type == "text/html" and not html_body:
                            html_body = body
                except:
                    pass
        else:
            try:
                body = msg.get_payload(decode=True)
                if body:
                    body = body.decode('utf-8', errors='ignore')
                    content_type = msg.get_content_type()
                    if content_type == "text/plain":
                        text_body = body
                    elif content_type == "text/html":
                        html_body = body
            except:
                pass
        
        return text_body, html_body
    
    def extract_attachments(self, msg) -> List[Dict]:
        """Extract attachment metadata from email message."""
        attachments = []
        
        if msg.is_multipart():
            for part in msg.walk():
                content_disposition = str(part.get("Content-Disposition", ""))
                
                if "attachment" in content_disposition:
                    filename = part.get_filename()
                    if filename:
                        filename = self.decode_header_value(filename)
                        content_type = part.get_content_type()
                        
                        # Get payload size
                        payload = part.get_payload(decode=True)
                        size_bytes = len(payload) if payload else 0
                        
                        attachments.append({
                            'filename': filename,
                            'content_type': content_type,
                            'size_bytes': size_bytes,
                            'payload': payload
                        })
        
        return attachments
    
    def match_submission(self, subject: str, body_text: str, attachments: List[Dict], 
                        db_session: Session, active_submissions: List[Submission]) -> Optional[Tuple[Submission, List[str]]]:
        """
        Match email to a submission based on insured name, attachments, and keywords.
        Returns (submission, matched_keywords) or None.
        """
        subject_lower = (subject or '').lower()
        body_lower = (body_text or '').lower()
        combined_text = f"{subject_lower} {body_lower}"
        
        # Keywords that indicate this might be a quote-related email
        quote_keywords = ['quote', 'proposal', 'renewal', 'premium', 'indication', 'pricing', 'coverage']
        
        matched_keywords = [kw for kw in quote_keywords if kw in combined_text]
        
        # Check for PDF/Excel attachments (likely quotes)
        has_quote_attachment = any(
            att['filename'].lower().endswith(('.pdf', '.xlsx', '.xls', '.docx', '.doc'))
            for att in attachments
        )
        
        # Try to match to a submission by insured name
        for submission in active_submissions:
            insured_name = (submission.insured_name or '').strip()
            if not insured_name:
                continue
            
            # Fuzzy match: check if significant parts of insured name appear in subject/body
            # Split insured name into words and check if most appear
            name_words = [w.lower() for w in re.findall(r'\w+', insured_name) if len(w) > 3]
            if not name_words:
                continue
            
            matches = sum(1 for word in name_words if word in combined_text)
            match_ratio = matches / len(name_words) if name_words else 0
            
            # If >50% of significant words match, consider it a match
            if match_ratio > 0.5:
                matched_keywords.append('insured_name_match')
                return submission, matched_keywords
            
            # Also check for exact partial matches (e.g., "Tree Frogs" in "Tree Frogs Adventure Park")
            if len(insured_name) > 5 and insured_name.lower() in combined_text:
                matched_keywords.append('insured_name_exact')
                return submission, matched_keywords
        
        # If we have quote keywords and attachments but no name match, still might be relevant
        # Return None for now (could be enhanced to show unmatched emails)
        return None, matched_keywords
    
    def save_attachment(self, attachment_data: Dict, email_id: int, db_session: Session) -> EmailAttachment:
        """Save attachment to disk and database."""
        # Create attachments directory if it doesn't exist
        attachments_dir = os.path.join('data', 'email_attachments', str(email_id))
        os.makedirs(attachments_dir, exist_ok=True)
        
        # Save file to disk
        filename = attachment_data['filename']
        file_path = os.path.join(attachments_dir, filename)
        
        with open(file_path, 'wb') as f:
            f.write(attachment_data['payload'])
        
        # Create database record
        attachment = EmailAttachment(
            email_id=email_id,
            filename=filename,
            content_type=attachment_data['content_type'],
            size_bytes=attachment_data['size_bytes'],
            file_path=file_path
        )
        db_session.add(attachment)
        return attachment
    
    def scrape_emails(self, since_date: datetime) -> Dict:
        """
        Scrape emails from IMAP server since the given date.
        Returns dict with counts of processed, matched, and new emails.
        """
        if not self.connect():
            return {'success': False, 'error': 'Failed to connect to IMAP server'}
        
        try:
            db_session = get_session()
            
            # Get active submissions (IN_PROGRESS or bound with renewal <= 120 days)
            active_submissions = []
            all_submissions = db_session.query(Submission).all()
            
            for sub in all_submissions:
                if sub.status == SubmissionStatus.IN_PROGRESS:
                    active_submissions.append(sub)
                elif sub.status in (SubmissionStatus.CHOSEN, SubmissionStatus.SENT_TO_FINANCE):
                    # Check if renewal within 120 days
                    if sub.effective_date:
                        try:
                            renewal_date = datetime.strptime(str(sub.effective_date)[:10], '%Y-%m-%d')
                            days_until = (renewal_date - datetime.now()).days
                            if days_until <= 120:
                                active_submissions.append(sub)
                        except:
                            pass
            
            # Get already-processed message IDs (including deleted ones - so they won't reappear)
            # Use a fresh query with expire_on_commit to ensure fresh data
            db_session.expire_on_commit = True
            existing_message_ids = set(
                row[0] for row in db_session.query(EmailMessage.message_id).all()
            )
            logger.info(f"Found {len(existing_message_ids)} existing email message IDs in database")
            
            # Select inbox
            self.mail.select('INBOX')
            
            # Search for emails since date
            date_str = since_date.strftime('%d-%b-%Y')
            _, message_numbers = self.mail.search(None, f'SINCE {date_str}')
            
            processed = 0
            matched = 0
            new_emails = 0
            
            for num in message_numbers[0].split():
                _, msg_data = self.mail.fetch(num, '(RFC822)')
                email_body = msg_data[0][1]
                msg = email.message_from_bytes(email_body)
                
                # Extract message ID
                message_id = msg.get('Message-ID', '')
                if not message_id or message_id in existing_message_ids:
                    continue
                
                # Extract headers
                from_header = self.decode_header_value(msg.get('From', ''))
                to_header = self.decode_header_value(msg.get('To', ''))
                subject = self.decode_header_value(msg.get('Subject', ''))
                date_header = msg.get('Date', '')
                
                # Parse date
                try:
                    received_date = parsedate_to_datetime(date_header)
                except:
                    received_date = datetime.utcnow()
                
                # Extract from email and name
                from_email = re.search(r'[\w\.-]+@[\w\.-]+', from_header)
                from_email = from_email.group(0) if from_email else from_header
                from_name = re.sub(r'<.*?>', '', from_header).strip()
                
                # Extract body
                text_body, html_body = self.extract_email_body(msg)
                
                # Extract attachments
                attachments = self.extract_attachments(msg)
                
                # Try to match to submission
                submission, keywords = self.match_submission(
                    subject, text_body, attachments, db_session, active_submissions
                )
                
                # Only save if matched to a submission
                if submission:
                    try:
                        email_record = EmailMessage(
                            submission_id=submission.id,
                            message_id=message_id,
                            from_email=from_email,
                            from_name=from_name,
                            to_email=to_header,
                            subject=subject,
                            body_text=text_body,
                            body_html=html_body,
                            received_date=received_date,
                            has_attachments=len(attachments) > 0,
                            attachment_count=len(attachments),
                            is_read=False,
                            matched_insured_name='insured_name' in ','.join(keywords),
                            matched_quote_attachment=any(
                                att['filename'].lower().endswith(('.pdf', '.xlsx', '.xls'))
                                for att in attachments
                            ),
                            matched_keywords=','.join(keywords)
                        )
                        db_session.add(email_record)
                        db_session.flush()  # Get email_record.id
                        
                        # Save attachments
                        for att_data in attachments:
                            self.save_attachment(att_data, email_record.id, db_session)
                        
                        matched += 1
                        new_emails += 1
                    except Exception as db_error:
                        # Handle duplicate message_id - email already exists
                        if 'UNIQUE constraint failed' in str(db_error) or 'message_id' in str(db_error).lower():
                            logger.warning(f"Email with message_id {message_id} already exists, skipping")
                        else:
                            logger.error(f"Error saving email record: {db_error}")
                        db_session.rollback()
                        continue
                
                processed += 1
            
            db_session.commit()
            db_session.close()
            
            return {
                'success': True,
                'processed': processed,
                'matched': matched,
                'new_emails': new_emails
            }
            
        except Exception as e:
            logger.error(f"Error scraping emails: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            self.disconnect()

