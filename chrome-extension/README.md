# PDF Quote Extractor - Chrome Extension

A minimal Chrome Extension (Manifest V3) that extracts quote data from PDF documents.

## Files

- `manifest.json` - Extension configuration (Manifest V3)
- `popup.html` - Extension popup UI
- `popup.js` - Extension logic
- `icon16.png`, `icon48.png`, `icon128.png` - Extension icons (you need to add these)

## Setup

### 1. Replace YOUR_BACKEND_DOMAIN

Edit `popup.js` and replace `YOUR_BACKEND_DOMAIN` with your actual backend domain:

```javascript
const BACKEND_DOMAIN = 'https://your-domain.com';
```

### 2. Add Icons

Create or add three PNG icon files:
- `icon16.png` - 16x16 pixels
- `icon48.png` - 48x48 pixels  
- `icon128.png` - 128x128 pixels

You can create simple icons using any image editor or online tool.

### 3. Load Extension in Chrome

1. Open Google Chrome
2. Navigate to `chrome://extensions/`
3. Enable **Developer mode** (toggle in top-right corner)
4. Click **Load unpacked** button
5. Select the `chrome-extension` folder
6. The extension icon should appear in your Chrome toolbar

### 4. Using the Extension

1. Navigate to a PDF in Chrome
2. Click the extension icon in the toolbar
3. Click "Extract Quote" button
4. View the extracted JSON result in the popup

## Backend API Requirements

Your backend should expose an endpoint at `/api/parse` that accepts:

```json
POST /api/parse
{
  "pdf_url": "https://example.com/quote.pdf",
  "mode": "finance"
}
```

And returns the parsed quote data as JSON.

## Security Notes

- No API keys are embedded in the extension
- All AI/OCR processing is done server-side
- Uses modern JavaScript (no deprecated APIs)
- Follows Manifest V3 CSP rules (no inline scripts)
