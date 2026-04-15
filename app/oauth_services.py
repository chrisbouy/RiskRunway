# app/oauth_services.py
"""
OAuth services for Gmail and Outlook email integration.
Provides token encryption, OAuth flows, and email fetching.
"""
import os
import json
import base64
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from functools import wraps

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import requests

logger = logging.getLogger(__name__)


# ============================================================================
# Token Encryption
# ============================================================================

def get_encryption_key() -> bytes:
    """
    Get or create encryption key for token storage.
    In production, this should be stored securely (e.g., environment variable).
    """
    from flask import current_app
    
    key = current_app.config.get('TOKEN_ENCRYPTION_KEY')
    if not key:
        # Generate a new key if not configured
        import secrets
        key = secrets.token_hex(32)
    
    # Convert hex to bytes and ensure it's 32 bytes for Fernet
    key_bytes = bytes.fromhex(key)
    if len(key_bytes) < 32:
        key_bytes = key_bytes.ljust(32, b'0')
    
    # Generate a Fernet key from the master key
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b'ipfs_mapper_email_tokens',  # Fixed salt for consistency
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(key_bytes[:32]))
    return key


def encrypt_token(tokens: Dict) -> str:
    """
    Encrypt token dictionary for storage.
    """
    try:
        key = get_encryption_key()
        fernet = Fernet(key)
        token_json = json.dumps(tokens)
        encrypted = fernet.encrypt(token_json.encode())
        return base64.b64encode(encrypted).decode()
    except Exception as e:
        logger.error(f"Failed to encrypt token: {e}")
        raise


def decrypt_token(encrypted_tokens: str) -> Dict:
    """
    Decrypt stored tokens. Returns empty dict if decryption fails.
    """
    try:
        if not encrypted_tokens:
            return {}
        
        key = get_encryption_key()
        fernet = Fernet(key)
        encrypted = base64.b64decode(encrypted_tokens.encode())
        decrypted = fernet.decrypt(encrypted)
        return json.loads(decrypted.decode())
    except Exception as e:
        logger.warning(f"Failed to decrypt token: {type(e).__name__}: {str(e)[:100]}")
        # Return empty dict so scraper can gracefully skip this account
        return {}


# ============================================================================
# Unified Email Data Model
# ============================================================================

class UnifiedEmail:
    """
    Normalized email structure regardless of provider.
    """
    def __init__(
        self,
        provider: str,
        message_id: str,
        subject: str,
        from_email: str,
        from_name: Optional[str],
        to_email: str,
        date: datetime,
        body_text: Optional[str],
        body_html: Optional[str],
        attachments: List[Dict]
    ):
        self.provider = provider
        self.message_id = message_id
        self.subject = subject
        self.from_email = from_email
        self.from_name = from_name
        self.to_email = to_email
        self.date = date
        self.body_text = body_text
        self.body_html = body_html
        self.attachments = attachments
    
    def to_dict(self) -> Dict:
        return {
            'provider': self.provider,
            'message_id': self.message_id,
            'subject': self.subject,
            'from': self.from_email,
            'from_name': self.from_name,
            'to': self.to_email,
            'date': self.date.isoformat() if self.date else None,
            'body_text': self.body_text,
            'body_html': self.body_html,
            'attachments': self.attachments
        }


# ============================================================================
# Gmail OAuth Service
# ============================================================================

class GmailOAuthService:
    """
    Gmail API OAuth service using Google API Python Client.
    """
    
    SCOPES = [
        'https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/gmail.labels'
    ]
    
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._service = None
    
    def get_authorization_url(self, state: str = None) -> Tuple[str, str]:
        """
        Generate OAuth authorization URL.
        Returns (url, state) tuple.
        """
        # Use google-auth library for OAuth flow
        from google_auth_oauthlib.flow import Flow
        
        flow = Flow.from_client_config(
            {
                'web': {
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'redirect_uri': self.redirect_uri,
                    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                    'token_uri': 'https://oauth2.googleapis.com/token',
                }
            },
            scopes=self.SCOPES
        )
        
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )
        
        return authorization_url, state
    
    def exchange_code_for_tokens(self, code: str, state: str) -> Dict:
        """
        Exchange authorization code for access and refresh tokens.
        """
        from google_auth_oauthlib.flow import Flow
        
        flow = Flow.from_client_config(
            {
                'web': {
                    'client_id': self.client_id,
                    'client_secret': self.client_secret,
                    'redirect_uri': self.redirect_uri,
                    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                    'token_uri': 'https://oauth2.googleapis.com/token',
                }
            },
            scopes=self.SCOPES
        )
        
        flow.fetch_token(code=code)
        return {
            'access_token': flow.credentials.token,
            'refresh_token': flow.credentials.refresh_token,
            'token_type': 'Bearer',
            'expires_in': 3600,
            'scope': ' '.join(self.SCOPES)
        }
    
    def refresh_access_token(self, refresh_token: str) -> Dict:
        """
        Refresh expired access token.
        """
        import google.oauth2.credentials
        
        credentials = google.oauth2.credentials.Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=self.client_id,
            client_secret=self.client_secret,
            token_uri='https://oauth2.googleapis.com/token',
            scopes=self.SCOPES
        )
        
        credentials.refresh(None)
        
        return {
            'access_token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_type': 'Bearer',
            'expires_in': 3600
        }
    
    def get_user_email(self, access_token: str) -> str:
        """
        Get the user's email address from Gmail API.
        """
        from googleapiclient.discovery import build
        
        credentials = google.oauth2.credentials.Credentials(token=access_token)
        service = build('gmail', 'v1', credentials=credentials)
        
        profile = service.users().getProfile(userId='me').execute()
        return profile.get('emailAddress', '')
    
    def fetch_emails(
        self,
        access_token: str,
        max_results: int = 50,
        query: str = None,
        since_date: datetime = None,
        broker_emails: list = None,
        quote_subjects: list = None
    ) -> List[UnifiedEmail]:
        """
        Fetch recent emails from Gmail.
        Filters by broker senders and/or quote subjects if provided.
        """
        from googleapiclient.discovery import build

        credentials = google.oauth2.credentials.Credentials(token=access_token)
        service = build('gmail', 'v1', credentials=credentials)

        # Build query
        query_parts = []

        # Filter by broker emails (if provided)
        if broker_emails:
            broker_queries = [f"from:{email}" for email in broker_emails]
            query_parts.append(f"({' OR '.join(broker_queries)})")

        # Filter by quote subjects (if provided)
        if quote_subjects:
            subject_queries = [f"subject:{subject}" for subject in quote_subjects]
            query_parts.append(f"({' OR '.join(subject_queries)})")

        # Default to attachments if no other filters
        if not query_parts:
            query_parts.append('has:attachment')

        # Add custom query if provided
        if query:
            query_parts.append(query)

        search_query = ' '.join(query_parts)

        if since_date:
            search_query += f' after:{since_date.strftime("%Y/%m/%d")}'

        # Get message list
        results = service.users().messages().list(
            userId='me',
            q=search_query,
            maxResults=max_results
        ).execute()

        messages = results.get('messages', [])
        emails = []

        for msg in messages:
            # Get full message
            message = service.users().messages().get(
                userId='me',
                id=msg['id'],
                format='full'
            ).execute()

            unified_email = self._parse_gmail_message(message)
            if unified_email:
                emails.append(unified_email)

        return emails
    
    def fetch_attachments(
        self,
        access_token: str,
        message_id: str,
        attachment_id: str
    ) -> bytes:
        """
        Download attachment from Gmail.
        """
        from googleapiclient.discovery import build
        
        credentials = google.oauth2.credentials.Credentials(token=access_token)
        service = build('gmail', 'v1', credentials=credentials)
        
        attachment = service.users().messages().attachments().get(
            userId='me',
            messageId=message_id,
            id=attachment_id
        ).execute()
        
        return base64.urlsafe_b64decode(attachment.get('data', ''))
    
    def _parse_gmail_message(self, message: Dict) -> Optional[UnifiedEmail]:
        """
        Parse Gmail message into unified format.
        """
        try:
            payload = message.get('payload', {})
            headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
            
            # Extract basic fields
            message_id = message.get('id', '')
            subject = headers.get('subject', '')
            from_header = headers.get('from', '')
            to_header = headers.get('to', '')
            date_str = headers.get('date', '')
            
            # Parse from header
            from_email, from_name = self._parse_email_header(from_header)
            
            # Parse date
            try:
                from email.utils import parsedate_to_datetime
                date = parsedate_to_datetime(date_str)
            except:
                date = datetime.utcnow()
            
            # Extract body
            body_text, body_html = self._extract_body(payload)
            
            # Extract attachments
            attachments = self._extract_attachments(payload, message_id)
            
            return UnifiedEmail(
                provider='gmail',
                message_id=message_id,
                subject=subject,
                from_email=from_email,
                from_name=from_name,
                to_email=to_header,
                date=date,
                body_text=body_text,
                body_html=body_html,
                attachments=attachments
            )
        except Exception as e:
            logger.error(f"Failed to parse Gmail message: {e}")
            return None
    
    def _parse_email_header(self, header: str) -> Tuple[str, Optional[str]]:
        """
        Parse email header like 'John Doe <john@example.com>'.
        """
        import re
        
        match = re.search(r'<(.*?)>', header)
        if match:
            email = match.group(1).strip()
            name = re.sub(r'<.*?>', '', header).strip()
            return email, name if name else None
        
        return header.strip(), None
    
    def _extract_body(self, payload: Dict) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract text and HTML body from message payload.
        """
        text_body = None
        html_body = None
        
        # Handle multipart messages
        if payload.get('parts'):
            for part in payload['parts']:
                content_type = part.get('mimeType', '')
                data = part.get('data')
                
                if not data:
                    continue
                
                try:
                    decoded_data = base64.urlsafe_b64decode(data.encode('ASCII'))
                    
                    if content_type == 'text/plain' and not text_body:
                        text_body = decoded_data.decode('utf-8', errors='ignore')
                    elif content_type == 'text/html' and not html_body:
                        html_body = decoded_data.decode('utf-8', errors='ignore')
                except:
                    pass
        
        # Handle simple messages
        elif payload.get('data'):
            try:
                content_type = payload.get('mimeType', '')
                decoded_data = base64.urlsafe_b64decode(payload['data'].encode('ASCII'))
                
                if content_type == 'text/plain':
                    text_body = decoded_data.decode('utf-8', errors='ignore')
                elif content_type == 'text/html':
                    html_body = decoded_data.decode('utf-8', errors='ignore')
            except:
                pass
        
        return text_body, html_body
    
    def _extract_attachments(self, payload: Dict, message_id: str) -> List[Dict]:
        """
        Extract attachment metadata from message payload.
        """
        attachments = []
        
        def process_parts(parts):
            for part in parts:
                content_disposition = part.get('headers', [])
                is_attachment = False
                filename = ''
                
                for header in content_disposition:
                    if header.get('name', '').lower() == 'content-disposition':
                        is_attachment = 'attachment' in header.get('value', '').lower()
                    if header.get('name', '').lower() == 'filename':
                        filename = header.get('value', '')
                
                if is_attachment and filename:
                    attachments.append({
                        'message_id': message_id,
                        'attachment_id': part.get('body', {}).get('attachmentId', ''),
                        'filename': filename,
                        'content_type': part.get('mimeType', ''),
                        'size': part.get('body', {}).get('size', 0)
                    })
                
                # Recurse into nested parts
                if part.get('parts'):
                    process_parts(part['parts'])
        
        if payload.get('parts'):
            process_parts(payload['parts'])
        
        return attachments


# ============================================================================
# Microsoft Outlook (Graph API) OAuth Service
# ============================================================================

class OutlookOAuthService:
    """
    Microsoft Graph API OAuth service using MSAL.
    """
    
    SCOPES = [
        'Mail.Read',
        'Mail.Send',
        'User.Read'
    ]
    
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, tenant_id: str = 'common'):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.tenant_id = tenant_id
        self._app = None
    
    def _get_app(self):
        """
        Get MSAL application instance.
        """
        if self._app is None:
            from msal import ConfidentialClientApplication
            self._app = ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=self.client_secret,
                authority=f'https://login.microsoftonline.com/{self.tenant_id}'
            )
        return self._app
    
    def get_authorization_url(self, state: str = None) -> Tuple[str, str]:
        # """
        # Generate OAuth authorization URL.
        # """
        # app = self._get_app()
        
        # # Generate state if not provided
        # import secrets
        # if not state:
        #     state = secrets.token_urlsafe(32)
        
        # # Use initiate_auth_code_flow to get authorization URL
        # flow = app.initiate_auth_code_flow(
        #     scopes=self.SCOPES,
        #     redirect_uri=self.redirect_uri
        # )
        
        # authorization_url = flow.get('auth_uri', '')
        
        # # Append state to the authorization URL if not already included
        # if state and 'state=' not in authorization_url:
        #     separator = '&' if '?' in authorization_url else '?'
        #     authorization_url = f"{authorization_url}{separator}state={state}"
        
        # return authorization_url, state
        """
        Generate OAuth authorization URL.
        Returns (url, flow) — caller must store the entire flow dict in session.
        """
        app = self._get_app()
        
        flow = app.initiate_auth_code_flow(
            scopes=self.SCOPES,
            redirect_uri=self.redirect_uri
        )
        
        authorization_url = flow.get('auth_uri', '')
        return authorization_url, flow  # return full flow, not just state

    # def exchange_code_for_tokens(self, code: str, state: str = None) -> Dict:
        # """
        # Exchange authorization code for access and refresh tokens.
        # """
        # app = self._get_app()
        
        # # Use acquire_token_by_authorization_code to exchange code
        # result = app.acquire_token_by_authorization_code(
        #     code=code,
        #     scopes=self.SCOPES,
        #     redirect_uri=self.redirect_uri
        # )
        
        # if 'access_token' in result:
        #     return {
        #         'access_token': result['access_token'],
        #         'refresh_token': result.get('refresh_token'),
        #         'token_type': result.get('token_type', 'Bearer'),
        #         'expires_in': result.get('expires_in', 3600),
        #         'scope': ' '.join(self.SCOPES)
        #     }
        # else:
        #     raise Exception(f"Failed to get tokens: {result.get('error_description', result.get('error'))}")
    def exchange_code_for_tokens(self, auth_response: dict, flow: dict) -> Dict:
        """
        Exchange authorization code for tokens using the saved MSAL flow.
        auth_response = dict of all query params from the callback URL
        flow = the flow dict saved in session during get_authorization_url
        """
        app = self._get_app()
        
        result = app.acquire_token_by_auth_code_flow(
            auth_code_flow=flow,
            auth_response=auth_response
        )
        
        if 'access_token' in result:
            return {
                'access_token': result['access_token'],
                'refresh_token': result.get('refresh_token'),
                'token_type': result.get('token_type', 'Bearer'),
                'expires_in': result.get('expires_in', 3600),
                'scope': ' '.join(self.SCOPES)
            }
        else:
            raise Exception(f"Failed to get tokens: {result.get('error_description', result.get('error'))}")
        
    
    def refresh_access_token(self, refresh_token: str) -> Dict:
        """
        Refresh expired access token.
        """
        app = self._get_app()
        
        result = app.acquire_token_by_refresh_token(
            refresh_token=refresh_token,
            scopes=self.SCOPES
        )
        
        if 'access_token' in result:
            return {
                'access_token': result['access_token'],
                'refresh_token': result.get('refresh_token', refresh_token),
                'token_type': result.get('token_type', 'Bearer'),
                'expires_in': result.get('expires_in', 3600)
            }
        else:
            raise Exception(f"Failed to refresh token: {result.get('error_description', result.get('error'))}")
    
    def get_user_email(self, access_token: str) -> str:
        """
        Get the user's email address from Graph API.
        """
        response = requests.get(
            'https://graph.microsoft.com/v1.0/me',
            headers={'Authorization': f'Bearer {access_token}'}
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get('mail', data.get('userPrincipalName', ''))
        return ''
    
    def fetch_emails(
        self,
        access_token: str,
        max_results: int = 50,
        query: str = None,
        since_date: datetime = None,
        broker_emails: list = None,
        quote_subjects: list = None
    ) -> List[UnifiedEmail]:
        """
        Fetch recent emails from Outlook/Graph API.
        Filters by broker senders and/or quote subjects if provided.
        """
        # Build query filters
        filters = []
        
        # Filter by broker emails (if provided)
        if broker_emails:
            broker_filter_parts = [f"from/emailAddress/address eq '{email}'" for email in broker_emails]
            filters.append(f"({' or '.join(broker_filter_parts)})")
        
        # Filter by quote subjects (if provided)
        if quote_subjects:
            subject_filter_parts = [f"contains(subject,'{subject}')" for subject in quote_subjects]
            filters.append(f"({' or '.join(subject_filter_parts)})")
        
        # If no filters provided, default to checking for attachments
        if not filters:
            filters.append('hasAttachments eq true')
        
        if query:
            filters.append(f"contains(subject,'{query}')")
        
        if since_date:
            # Format datetime for Graph API: '2026-03-24T00:32:00Z' (no microseconds, UTC timezone)
            formatted_date = since_date.replace(microsecond=0).isoformat() + 'Z'
            filters.append(f"receivedDateTime ge {formatted_date}")
        
        filter_query = ' and '.join(filters)
        
        # Get messages
        response = requests.get(
            'https://graph.microsoft.com/v1.0/me/messages',
            headers={'Authorization': f'Bearer {access_token}'},
            params={
                '$top': max_results,
                '$filter': filter_query,
                '$select': 'id,subject,from,toRecipients,receivedDateTime,body,attachments'
            }
        )
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch emails: {response.text}")
            return []
        
        data = response.json()
        messages = data.get('value', [])
        emails = []
        
        for msg in messages:
            unified_email = self._parse_outlook_message(msg, access_token)
            if unified_email:
                emails.append(unified_email)
        
        return emails
    
    def fetch_attachments(
        self,
        access_token: str,
        message_id: str,
        attachment_id: str
    ) -> bytes:
        """
        Download attachment from Outlook.
        """
        response = requests.get(
            f'https://graph.microsoft.com/v1.0/me/messages/{message_id}/attachments/{attachment_id}',
            headers={'Authorization': f'Bearer {access_token}'}
        )
        
        if response.status_code == 200:
            data = response.json()
            content_bytes = data.get('contentBytes', '')
            return base64.b64decode(content_bytes)
        
        return b''
    
    def _parse_outlook_message(self, message: Dict, access_token: str = None) -> Optional[UnifiedEmail]:
        """
        Parse Outlook message into unified format.
        """
        try:
            # Extract basic fields
            message_id = message.get('id', '')
            subject = message.get('subject', '')
            
            # From header
            from_info = message.get('from', {})
            from_email = from_info.get('emailAddress', {}).get('address', '')
            from_name = from_info.get('emailAddress', {}).get('name', '')
            
            # To header
            to_info = message.get('toRecipients', [])
            to_email = ', '.join([r.get('emailAddress', {}).get('address', '') for r in to_info])
            
            # Date
            date_str = message.get('receivedDateTime', '')
            try:
                date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            except:
                date = datetime.utcnow()
            
            # Body
            body_info = message.get('body', {})
            body_text = None
            body_html = None
            
            if body_info.get('contentType') == 'text':
                body_text = body_info.get('content', '')
            elif body_info.get('contentType') == 'html':
                body_html = body_info.get('content', '')
            
            # Attachments - fetch separately if we have access_token
            attachments = []
            
            # First check if attachments came in the message (inline)
            inline_attachments = message.get('attachments', [])
            print(f"Message {message_id} has {len(inline_attachments)} inline attachments")
            if inline_attachments:
                print(f"Message {message_id} has {len(inline_attachments)} inline attachments")
                for att in inline_attachments:
                    print(f"Inline attachment: {att}")
                    attachments.append({
                        'message_id': message_id,
                        'attachment_id': att.get('id', ''),
                        'filename': att.get('name', ''),
                        'content_type': att.get('odataType', ''),
                        'size': att.get('size', 0)
                    })
            
            # If no attachments and we have access_token, fetch them separately
            print(f"Attachments: {attachments}")
            if not attachments and access_token:
                try:
                    print(f"Fetching attachments for {message_id} (separate call)")
                    att_response = requests.get(
                        f'https://graph.microsoft.com/v1.0/me/messages/{message_id}/attachments',
                        headers={'Authorization': f'Bearer {access_token}'}
                    )
                    if att_response.status_code == 200:
                        att_data = att_response.json().get('value', [])
                        print(f"Message {message_id} fetched {len(att_data)} attachments (separate call)")
                        for att in att_data:
                            # print(f"Fetched attachment: {att}")
                            attachments.append({
                                'message_id': message_id,
                                'attachment_id': att.get('id', ''),
                                'filename': att.get('name', ''),
                                'content_type': att.get('odataType', ''),
                                'size': att.get('size', 0)
                            })
                except Exception as e:
                    print(f"Error fetching attachments for {message_id}: {e}")
            
            return UnifiedEmail(
                provider='outlook',
                message_id=message_id,
                subject=subject,
                from_email=from_email,
                from_name=from_name,
                to_email=to_email,
                date=date,
                body_text=body_text,
                body_html=body_html,
                attachments=attachments
            )
        except Exception as e:
            logger.error(f"Failed to parse Outlook message: {e}")
            return None

    
    def send_email(
        self,
        access_token: str,
        to_recipients: List[str],
        subject: str,
        body_text: str = None,
        body_html: str = None,
        attachments: List[Dict] = None,
        cc_recipients: List[str] = None,
        bcc_recipients: List[str] = None
    ) -> str:
        """
        Send an email using Graph API from the connected Outlook account.
        
        Args:
            access_token: OAuth access token
            to_recipients: List of recipient email addresses
            subject: Email subject
            body_text: Plain text body (optional)
            body_html: HTML body (optional, takes precedence over body_text)
            attachments: List of attachment dicts with {filename, content_base64, content_type}
            cc_recipients: List of CC addresses (optional)
            bcc_recipients: List of BCC addresses (optional)
        
        Returns:
            The message ID of the sent email
        """
        try:
            # Build email body
            if body_html:
                body_content = {'contentType': 'html', 'content': body_html}
            elif body_text:
                body_content = {'contentType': 'text', 'content': body_text}
            else:
                body_content = {'contentType': 'text', 'content': ''}
            
            # Build recipient lists
            def format_recipients(emails):
                return [{'emailAddress': {'address': email}} for email in emails] if emails else []
            
            # Build message
            message_body = {
                'subject': subject,
                'body': body_content,
                'toRecipients': format_recipients(to_recipients),
                'ccRecipients': format_recipients(cc_recipients),
                'bccRecipients': format_recipients(bcc_recipients),
            }
            
            # Add attachments if provided
            if attachments:
                message_body['attachments'] = []
                for att in attachments:
                    message_body['attachments'].append({
                        '@odata.type': '#microsoft.graph.fileAttachment',
                        'name': att.get('filename', 'attachment'),
                        'contentBytes': att.get('content_base64', ''),
                        'contentType': att.get('content_type', 'application/octet-stream')
                    })
            
            # Send email via Graph API
            response = requests.post(
                'https://graph.microsoft.com/v1.0/me/sendMail',
                headers={'Authorization': f'Bearer {access_token}'},
                json={'message': message_body}
            )
            
            if response.status_code != 202:
                error_text = response.text
                logger.error(f"Failed to send email: {response.status_code} - {error_text}")
                raise Exception(f"Failed to send email: HTTP {response.status_code}")
            
            # Graph API doesn't return message ID for sendMail, so we generate one
            import uuid
            message_id = str(uuid.uuid4())
            
            logger.info(f"Email sent successfully via Graph API to {to_recipients}")
            return message_id
            
        except Exception as e:
            logger.error(f"Error sending email via Graph API: {e}")
            raise


# ============================================================================
# Factory and Utility Functions
# ============================================================================

def get_oauth_service(provider: str, config: Dict):
    """
    Factory function to get OAuth service based on provider.
    """
    if provider == 'gmail':
        return GmailOAuthService(
            client_id=config.get('GMAIL_CLIENT_ID'),
            client_secret=config.get('GMAIL_CLIENT_SECRET'),
            redirect_uri=config.get('GMAIL_REDIRECT_URI')
        )
    elif provider == 'outlook':
        return OutlookOAuthService(
            client_id=config.get('MICROSOFT_CLIENT_ID'),
            client_secret=config.get('MICROSOFT_CLIENT_SECRET'),
            redirect_uri=config.get('MICROSOFT_REDIRECT_URI'),
            tenant_id=config.get('MICROSOFT_TENANT_ID', 'common')
        )
    else:
        raise ValueError(f"Unknown email provider: {provider}")


def get_unified_email_data(unified_email: UnifiedEmail) -> Dict:
    """
    Convert unified email to normalized dict for database storage.
    """
    return {
        'provider': unified_email.provider,
        'message_id': unified_email.message_id,
        'subject': unified_email.subject,
        'from_email': unified_email.from_email,
        'from_name': unified_email.from_name,
        'to_email': unified_email.to_email,
        'received_date': unified_email.date,
        'body_text': unified_email.body_text,
        'body_html': unified_email.body_html,
        'has_attachments': len(unified_email.attachments) > 0,
        'attachment_count': len(unified_email.attachments),
        'attachments': unified_email.attachments
    }
