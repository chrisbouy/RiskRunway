// PDF Quote Extractor - Popup Script

// Configuration - set this to your backend domain
const BACKEND_DOMAIN = 'http://localhost:5001'; // Update for production

/**
 * Copy text to clipboard with visual feedback
 * @param {string} text - Text to copy
 * @param {HTMLElement} element - Element to show feedback on
 */
function copyToClipboard(text, element) {
  navigator.clipboard.writeText(text).then(() => {
    element.classList.add('copy-success');
    
    setTimeout(() => {
      element.classList.remove('copy-success');
    }, 300);
  }).catch(err => {
    console.error('Failed to copy:', err);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  const status = document.getElementById('status');
  const resultContainer = document.getElementById('resultContainer');
  const summaryCards = document.getElementById('summaryCards');
  const coverageBody = document.getElementById('coverageBody');

  // Auto-extract on popup open
  async function extractQuote() {
    resultContainer.classList.remove('visible');
    summaryCards.innerHTML = '';
    coverageBody.innerHTML = '';

    try {
      // Get current tab
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      
      if (!tab) {
        throw new Error('No active tab found');
      }

      const url = tab.url;

      // Check if URL is a PDF
      if (!isPdfUrl(url)) {
        throw new Error('Current tab is not a PDF. Please navigate to a PDF file.');
      }

      showStatus('Processing PDF...', 'loading');

      // Send request to backend
      const response = await fetch(`${BACKEND_DOMAIN}/api/parse`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          pdf_url: url,
          mode: 'finance'
        })
      });

      if (!response.ok) {
        // Try to get error message from response body
        let errorDetail = '';
        try {
          const errorData = await response.json();
          errorDetail = errorData.error || '';
        } catch (e) {
          // Couldn't parse JSON
        }
        throw new Error(`Server error: ${response.status} ${response.statusText}${errorDetail ? ' - ' + errorDetail : ''}`);
      }

      const data = await response.json();

      // Display formatted result
      if (data.success && data.parsed_data) {
        displayParsedData(data.parsed_data);
      }
      
      resultContainer.classList.add('visible');
      showStatus('Quote extracted successfully!', 'success');

    } catch (error) {
      // Handle errors
      let errorMessage = error.message;

      if (error.name === 'TypeError' && error.message === 'Failed to fetch') {
        errorMessage = 'Network error. Please check if the backend server is running.';
      }

      resultContainer.classList.add('visible');
      showStatus(errorMessage, 'error');
    }
  }

  // Trigger extraction immediately when popup opens
  extractQuote();

  /**
   * Display parsed quote data in cards and table
   * @param {object} parsedData - The parsed quote data
   */
  function displayParsedData(parsedData) {
    const insured = parsedData.insured || {};
    const broker = parsedData.general_agent_or_wholesale_broker || {};
    const policies = parsedData.policies || [];
    const totals = parsedData.totals || {};
    const financing = parsedData.financing || {};

    // Build summary cards HTML
    let cardsHtml = '';
    
    // Insured card
    const insuredName = insured.name || 'N/A';
    const insuredState = insured.address?.state || '';
    cardsHtml += createCard('Insured', `${insuredName}${insuredState ? ' (' + insuredState + ')' : ''}`);
    
    // Broker card
    const brokerName = broker.name || 'N/A';
    cardsHtml += createCard('Broker/GA', brokerName);
    
    // Quote # card
    const quoteNum = parsedData.quote_number || 'N/A';
    cardsHtml += createCard('Quote #', quoteNum);
    
    // Carrier card (from first policy)
    const carrier = policies[0]?.carrier || 'N/A';
    cardsHtml += createCard('Carrier', carrier);
    
    // Premium card
    const totalPremium = formatCurrency(totals.total_premium);
    cardsHtml += createCard('Total Premium', totalPremium, true);
    
    // Grand Total card
    const grandTotal = formatCurrency(totals.grand_total);
    cardsHtml += createCard('Grand Total', grandTotal, true);
    
    // Down Payment card (if financing)
    if (financing.down_payment) {
      cardsHtml += createCard('Down Payment', formatCurrency(financing.down_payment));
    }
    
    // Finance Amount card (if financing)
    if (financing.amount_financed) {
      cardsHtml += createCard('Financed', formatCurrency(financing.amount_financed));
    }

    summaryCards.innerHTML = cardsHtml;

    // Build coverage table rows
    let tableHtml = '';
    policies.forEach(policy => {
      const coverageType = policy.coverage_type || 'N/A';
      const policyCarrier = policy.carrier || 'N/A';
      const policyNumber = policy.policy_number || 'N/A';
      const premium = formatCurrency(policy.annual_premium);
      
      tableHtml += `
        <tr>
          <td class="clickable" data-copy="${escapeHtml(coverageType)}">${escapeHtml(coverageType)}</td>
          <td class="clickable" data-copy="${escapeHtml(policyCarrier)}">${escapeHtml(policyCarrier)}</td>
          <td class="clickable" data-copy="${escapeHtml(policyNumber)}">${escapeHtml(policyNumber)}</td>
          <td class="premium clickable" data-copy="${premium.replace(/[^0-9.-]/g, '')}">${premium}</td>
        </tr>
      `;
    });

    if (tableHtml === '') {
      tableHtml = '<tr><td colspan="4" style="text-align:center;color:#6b7280;">No coverages found</td></tr>';
    }

    coverageBody.innerHTML = tableHtml;

    // Attach click handlers for copy functionality
    attachCopyHandlers();
  }

  /**
   * Attach click handlers to copyable elements
   */
  function attachCopyHandlers() {
    // Card values
    document.querySelectorAll('.card-value.clickable').forEach(el => {
      el.addEventListener('click', function() {
        copyToClipboard(this.dataset.copy, this);
      });
    });

    // Table cells
    document.querySelectorAll('.coverage-table td.clickable').forEach(el => {
      el.addEventListener('click', function() {
        copyToClipboard(this.dataset.copy, this);
      });
    });
  }

  /**
   * Create a summary card HTML
   * @param {string} label - Card label
   * @param {string} value - Card value
   * @param {boolean} highlight - Whether to highlight the value
   * @returns {string} HTML string
   */
  function createCard(label, value, highlight = false) {
    return `
      <div class="card">
        <div class="card-label">${label}</div>
        <div class="card-value${highlight ? ' highlight' : ''} clickable" data-copy="${escapeHtml(value)}">${escapeHtml(value)}</div>
      </div>
    `;
  }

  /**
   * Format a number as currency
   * @param {number|null} amount - Amount to format
   * @returns {string} Formatted currency string
   */
  function formatCurrency(amount) {
    if (amount === null || amount === undefined || amount === '') {
      return 'N/A';
    }
    return '$' + Number(amount).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  /**
   * Escape HTML to prevent XSS
   * @param {string} text - Text to escape
   * @returns {string} Escaped text
   */
  function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
  }

  /**
   * Check if the URL points to a PDF file
   * @param {string} url - The URL to check
   * @returns {boolean}
   */
  function isPdfUrl(url) {
    if (!url) return false;
    
    // Check file extension
    const pdfExtensions = ['.pdf', '.PDF'];
    const hasPdfExtension = pdfExtensions.some(ext => url.toLowerCase().endsWith(ext));
    
    // Also check for Google Drive PDF viewer URLs
    const isGoogleDrivePdf = url.includes('drive.google.com/viewer') && url.includes('embedded=true');
    
    // Check for Chrome PDF viewer
    const isChromePdfViewer = url.startsWith('chrome-extension://') && url.includes('/pdf/viewer');
    
    return hasPdfExtension || isGoogleDrivePdf || isChromePdfViewer;
  }

  /**
   * Show a status message
   * @param {string} message - The message to display
   * @param {string} type - The type of message: 'error', 'success', 'loading'
   */
  function showStatus(message, type) {
    status.textContent = message;
    status.className = type;
    status.style.display = 'block';
  }
});
