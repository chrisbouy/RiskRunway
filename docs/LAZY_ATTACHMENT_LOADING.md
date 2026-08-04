# Lazy Attachment Loading Implementation

## Overview

Implemented lazy (on-demand) attachment downloading for OAuth email sources. Attachments are no longer downloaded during email scraping - instead, only metadata is stored, and the actual file is downloaded when the user clicks "Ingest Quote".

## Benefits

1. **Faster Email Scraping** - No need to download large PDF attachments during background scraping
2. **Reduced Storage** - Only download attachments that are actually used
3. **No Re-authentication Required** - OAuth tokens are automatically refreshed when needed

## Implementation Details

### 1. Email Scraping Phase (Background)

#### OAuth (`app/email_client.py`)
- Stores metadata only (filename, size, message_id, attachment_id)
- Stores in `EmailAttachment` with `file_path=None`

### 2. EmailAttachment Model

Key fields:
- `message_id` - Original email Message-ID
- `attachment_id` - Provider's attachment ID
- `file_path` - NULL until downloaded on-demand

### 3. On-Demand Download (`app/routes.py`)

Helper function: `_download_attachment_on_demand(attachment, email, db_session)`

1. Get ConnectedAccount from `email.connected_account_id`
2. Get decrypted tokens
3. Auto-refresh access_token if expired (using refresh_token)
4. Call `service.fetch_attachments(access_token, message_id, attachment_id)`
5. Save file to disk
6. Update `attachment.file_path` in database

### 4. Quote Ingestion Flow

**Updated `ingest_quote_to_submission()` route:**

```python
for att in attachments:
    # Download on-demand if not already downloaded
    file_path = _download_attachment_on_demand(att, email, db_session)
    
    if not file_path:
        continue
    
    # Rest of processing (same as before):
    # - Copy to uploads folder
    # - Parse with process_quote_two_pass()
    # - Create quote record
    # - Upload to storage
    # - Create document record
```

## OAuth Token Refresh

**No user re-authentication required!**

OAuth provides:
- `access_token` (short-lived: ~1 hour)
- `refresh_token` (long-lived: 90 days for Outlook, indefinite for Gmail)

The system automatically:
1. Checks if `account.expires_at < now()`
2. Calls `service.refresh_access_token(refresh_token)`
3. Updates encrypted tokens in database
4. Uses new access_token for download

User only needs to re-authenticate if:
- They manually revoke access
- Refresh token expires (rare)

## Testing

1. **Test Email Scraping (no downloads)**:
   - Trigger email scraping
   - Verify: EmailAttachment records created with file_path=NULL

2. **Test On-Demand Download (OAuth)**:
   - Click "Ingest Quote" on an email
   - Verify: Attachment downloads successfully
   - Verify: file_path updated in database
   - Verify: Quote created and parsed

3. **Test Token Refresh**:
   - Wait for access_token to expire (1+ hour)
   - Click "Ingest Quote"
   - Verify: Token auto-refreshes
   - Verify: Download succeeds
   - Verify: No authentication prompt

## Files Modified

- `app/email_client.py` - Stores metadata only during scraping
- `app/models.py` - attachment_id field holds provider's attachment ID
- `app/routes.py` - `_download_attachment_on_demand()` helper + updated `ingest_quote_to_submission()`

## Backwards Compatibility

Existing attachments that were already downloaded (have `file_path` set):
- Will NOT be re-downloaded
- `_download_attachment_on_demand()` checks if file exists first
- Returns existing path if file is on disk
