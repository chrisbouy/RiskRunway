"""
Applied Epic API Client

Handles authentication and API calls to Applied Epic's REST API and SDK Module.
Uses mock environment by default; swap EPIC_BASE_URL for production.
"""
import os
import time
import requests
from functools import wraps


class EpicAPIError(Exception):
    """Raised when an Epic API call fails."""
    def __init__(self, status_code, detail, endpoint=None):
        self.status_code = status_code
        self.detail = detail
        self.endpoint = endpoint
        super().__init__(f"Epic API Error ({status_code}) at {endpoint}: {detail}")


class EpicClient:
    """Client for Applied Epic REST API and SDK Module."""

    def __init__(self):
        self.client_id = os.environ.get('EPIC_CLIENT_ID', '')
        self.client_secret = os.environ.get('EPIC_CLIENT_SECRET', '')
        self.base_url = os.environ.get(
            'EPIC_BASE_URL',
            'https://api.mock.myappliedproducts.com'
        )
        self.token_url = f"{self.base_url}/v1/auth/connect/token"

        # API base paths
        self.client_api = f"{self.base_url}/epic/client/v1"
        self.policy_api = f"{self.base_url}/epic/policy/v2"
        self.attachment_api = f"{self.base_url}/epic/attachment/v2"
        self.sdk_api = f"{self.base_url}/sdk/v1"

        # Token cache
        self._access_token = None
        self._token_expires_at = 0

    @property
    def is_configured(self):
        """Check if Epic credentials are set."""
        return bool(self.client_id and self.client_secret)

    def _get_token(self):
        """Get a valid access token, refreshing if expired."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        resp = requests.post(self.token_url, data={
            'grant_type': 'client_credentials',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
        }, timeout=15)

        if resp.status_code != 200:
            raise EpicAPIError(
                resp.status_code,
                f"Token request failed: {resp.text}",
                self.token_url
            )

        token_data = resp.json()
        self._access_token = token_data['access_token']
        self._token_expires_at = time.time() + token_data.get('expires_in', 3600)
        return self._access_token

    def _headers(self):
        """Build request headers with bearer token."""
        return {
            'Authorization': f'Bearer {self._get_token()}',
            'Content-Type': 'application/json',
            'Accept': 'application/hal+json, application/json',
        }

    def _get(self, url, params=None):
        """Make an authenticated GET request."""
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        if resp.status_code >= 400:
            raise EpicAPIError(resp.status_code, resp.text, url)
        return resp.json()

    def _post(self, url, json_body=None):
        """Make an authenticated POST request."""
        resp = requests.post(url, headers=self._headers(), json=json_body, timeout=30)
        if resp.status_code >= 400:
            raise EpicAPIError(resp.status_code, resp.text, url)
        return resp.json()

    def _put(self, url, json_body=None):
        """Make an authenticated PUT request."""
        resp = requests.put(url, headers=self._headers(), json=json_body, timeout=30)
        if resp.status_code >= 400:
            raise EpicAPIError(resp.status_code, resp.text, url)
        return resp.json()

    # ─────────────────────────────────────────────
    # CLIENT / ACCOUNT LOOKUPS (REST API)
    # ─────────────────────────────────────────────

    def search_clients(self, name_contains, limit=20):
        """Search for clients by name (partial match)."""
        params = {
            'name_contains': name_contains,
            'active_status': 'active',
            'limit': limit,
        }
        data = self._get(f"{self.client_api}/clients", params=params)
        embedded = data.get('_embedded', {})
        clients = embedded.get('clients', [])
        return clients

    def get_client(self, client_id):
        """Get a single client by ID."""
        return self._get(f"{self.client_api}/clients/{client_id}")

    # ─────────────────────────────────────────────
    # POLICY LOOKUPS (REST API v2)
    # ─────────────────────────────────────────────

    def get_policies_for_client(self, client_id, status='PROSPECTIVE'):
        """Get policies for a client, filtered by status."""
        params = {
            'client': client_id,
            'embed': 'client,policyType',
        }
        if status:
            params['status'] = status
        data = self._get(f"{self.policy_api}/policies", params=params)
        embedded = data.get('_embedded', {})
        return embedded.get('policies', [])

    def get_policy(self, policy_id):
        """Get a single policy by ID with embedded resources."""
        params = {'embed': 'client,policyType,lines'}
        return self._get(f"{self.policy_api}/policies/{policy_id}", params=params)

    def get_lines_for_policy(self, policy_id):
        """Get lines for a specific policy."""
        params = {
            'policy': policy_id,
            'embed': 'client,lineType,issuingCompany',
        }
        data = self._get(f"{self.policy_api}/lines", params=params)
        embedded = data.get('_embedded', {})
        return embedded.get('lines', [])

    # ─────────────────────────────────────────────
    # LINE & POLICY UPDATES (SDK Module)
    # ─────────────────────────────────────────────

    def update_line(self, line_data):
        """
        Update an existing line via the SDK module.
        line_data must include LineID and Timestamp at minimum.
        """
        return self._put(f"{self.sdk_api}/lines", json_body=line_data)

    def update_policy(self, policy_data):
        """
        Update an existing policy via the SDK module.
        policy_data must include PolicyID and Timestamp at minimum.
        """
        return self._put(f"{self.sdk_api}/policies", json_body=policy_data)

    # ─────────────────────────────────────────────
    # ATTACHMENTS (REST API v2)
    # ─────────────────────────────────────────────

    def get_attachments_for_policy(self, policy_id, system_generated=None, description_contains=None):
        """
        Get attachments associated with a policy.

        Args:
            policy_id: UUID of the policy
            system_generated: If True/False, filter by system-generated flag
            description_contains: Filter by description substring

        Returns:
            List of attachment objects
        """
        params = {
            'policy': policy_id,
            'active_status': 'active',
            'fileStatus': 'OK',
        }
        if system_generated is not None:
            params['systemGenerated'] = str(system_generated).lower()
        if description_contains:
            params['description_contains'] = description_contains

        data = self._get(f"{self.attachment_api}/attachments", params=params)
        embedded = data.get('_embedded', {})
        return embedded.get('attachments', [])

    def download_attachment_file(self, file_url):
        """
        Download the actual file content from an attachment's file URL.

        Args:
            file_url: The URL from attachment.file.url

        Returns:
            bytes of the file content, or None if download fails
        """
        try:
            # The file URL may or may not need auth depending on the implementation
            # Try with auth first, fall back to without
            headers = {'Authorization': f'Bearer {self._get_token()}'}
            resp = requests.get(file_url, headers=headers, timeout=60)
            if resp.status_code == 200:
                return resp.content

            # Try without auth (some document services use pre-signed URLs)
            resp = requests.get(file_url, timeout=60)
            if resp.status_code == 200:
                return resp.content

            return None
        except Exception:
            return None

    def create_attachment(self, attach_to_id, attach_to_type, filename, description=None,
                          folder=None, received_on=None):
        """
        Create an attachment record in Epic and get an upload URL.

        Args:
            attach_to_id: UUID of the entity to attach to
            attach_to_type: One of ACCOUNT, POLICY, LINE, MARKETING_SUBMISSION, etc.
            filename: Name of the file being uploaded (with extension)
            description: Optional description (defaults to filename)
            folder: Optional folder UUID
            received_on: Optional ISO datetime string

        Returns:
            dict with attachment metadata including 'uploadUrl'
        """
        from datetime import datetime

        body = {
            'description': description or filename,
            'active': True,
            'receivedOn': received_on or datetime.utcnow().isoformat() + 'Z',
            'uploadFileName': filename,
            'attachTo': {
                'id': attach_to_id,
                'type': attach_to_type,
            }
        }
        if folder:
            body['folder'] = folder

        return self._post(f"{self.attachment_api}/attachments", json_body=body)

    def upload_attachment_file(self, upload_url, file_bytes, content_type='application/pdf'):
        """
        Upload actual file content to the URL returned by create_attachment.

        Args:
            upload_url: The uploadUrl from create_attachment response
            file_bytes: Raw file content
            content_type: MIME type of the file
        """
        headers = {
            'Content-Type': content_type,
        }
        resp = requests.put(upload_url, headers=headers, data=file_bytes, timeout=60)
        if resp.status_code >= 400:
            raise EpicAPIError(resp.status_code, resp.text, upload_url)
        return True

    # ─────────────────────────────────────────────
    # CONVENIENCE: FULL EXPORT FLOW
    # ─────────────────────────────────────────────

    def export_submission_to_epic(self, epic_policy_id, epic_line_id, line_update_data,
                                  policy_update_data=None, documents=None):
        """
        Full export flow: update line, optionally update policy, attach documents.

        Args:
            epic_policy_id: The Epic policy ID (integer for SDK)
            epic_line_id: The Epic line ID (integer for SDK)
            line_update_data: Dict of fields to update on the line
            policy_update_data: Optional dict of fields to update on the policy
            documents: Optional list of dicts with 'filepath', 'filename', 'content_type'

        Returns:
            dict with results of each step
        """
        results = {'line_updated': False, 'policy_updated': False, 'attachments': []}

        # Step 1: Update line
        if line_update_data:
            line_update_data['LineID'] = epic_line_id
            self.update_line(line_update_data)
            results['line_updated'] = True

        # Step 2: Update policy (optional)
        if policy_update_data:
            policy_update_data['PolicyID'] = epic_policy_id
            self.update_policy(policy_update_data)
            results['policy_updated'] = True

        # Step 3: Attach documents
        if documents:
            for doc in documents:
                try:
                    attachment_resp = self.create_attachment(
                        attach_to_id=str(epic_policy_id),
                        attach_to_type='POLICY',
                        filename=doc['filename'],
                        description=doc.get('description', doc['filename']),
                    )
                    upload_url = attachment_resp.get('uploadUrl')
                    if upload_url and doc.get('filepath'):
                        with open(doc['filepath'], 'rb') as f:
                            self.upload_attachment_file(
                                upload_url, f.read(),
                                content_type=doc.get('content_type', 'application/pdf')
                            )
                    results['attachments'].append({
                        'filename': doc['filename'],
                        'success': True,
                    })
                except EpicAPIError as e:
                    results['attachments'].append({
                        'filename': doc['filename'],
                        'success': False,
                        'error': str(e),
                    })

        return results


# Module-level singleton
_epic_client = None


def get_epic_client():
    """Get or create the Epic API client singleton."""
    global _epic_client
    if _epic_client is None:
        _epic_client = EpicClient()
    return _epic_client
