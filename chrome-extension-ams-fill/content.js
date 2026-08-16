/**
 * RiskRunway AMS Fill — Content Script
 * 
 * Injected into the AMS tab. Enumerates form fields, sends to server for
 * AI matching, then fills the form directly via DOM with visual feedback.
 */

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'enumerate_and_fill') {
    handleEnumerateAndFill(message).then(sendResponse);
    return true; // async
  }
});

// ─── Blocking Overlay ────────────────────────────────────────────────────────

function showBlockingOverlay() {
  const overlay = document.createElement('div');
  overlay.id = 'riskrunway-blocking-overlay';
  overlay.style.cssText = `
    position: fixed;
    inset: 0;
    z-index: 2147483647;
    background: rgba(0, 0, 0, 0.4);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 60px;
    pointer-events: all;
    cursor: not-allowed;
  `;
  overlay.innerHTML = `
    <div id="riskrunway-overlay-content" style="
      background: #0f1219;
      border: 2px solid #4f8ef7;
      border-radius: 12px;
      padding: 28px 40px;
      text-align: center;
      box-shadow: 0 20px 60px rgba(0,0,0,0.6);
      pointer-events: none;
    ">
      <div style="font-size: 24px; font-weight: 700; color: #4f8ef7; margin-bottom: 8px;">
        ⟳ Exporting...
      </div>
      <div style="font-size: 14px; color: #8892b0;">
        Do not click or type while data is being transferred
      </div>
    </div>
  `;
  // Block all interaction
  const blockClick = e => e.stopPropagation();
  const blockKey = e => e.stopPropagation();
  const blockMouse = e => e.stopPropagation();
  overlay.addEventListener('click', blockClick, true);
  overlay.addEventListener('keydown', blockKey, true);
  overlay.addEventListener('mousedown', blockMouse, true);
  overlay._blockers = { blockClick, blockKey, blockMouse };
  document.body.appendChild(overlay);
}

function showReviewOverlay(filledCount) {
  const overlay = document.getElementById('riskrunway-blocking-overlay');
  if (!overlay) return;
  
  // Remove blocking listeners so clicks can dismiss
  if (overlay._blockers) {
    overlay.removeEventListener('click', overlay._blockers.blockClick, true);
    overlay.removeEventListener('keydown', overlay._blockers.blockKey, true);
    overlay.removeEventListener('mousedown', overlay._blockers.blockMouse, true);
  }

  overlay.style.cursor = 'pointer';
  const content = document.getElementById('riskrunway-overlay-content');
  if (content) {
    content.style.pointerEvents = 'auto';
    content.style.cursor = 'pointer';
    content.innerHTML = `
      <div style="font-size: 28px; font-weight: 700; color: #059669; margin-bottom: 10px;">
        ✓ Export Complete
      </div>
      <div style="font-size: 14px; color: #6ee7b7; margin-bottom: 12px;">
        ${filledCount} field${filledCount !== 1 ? 's' : ''} filled
      </div>
      <div style="font-size: 20px; font-weight: 600; color: #ffffff; margin-bottom: 8px;">
        Review Transferred Data Before Saving
      </div>
      <div style="font-size: 12px; color: #6b7394; margin-top: 16px;">
        Click anywhere to dismiss
      </div>
    `;
  }
  overlay.addEventListener('click', () => overlay.remove());
}

function showErrorOverlay(errorMsg) {
  const overlay = document.getElementById('riskrunway-blocking-overlay');
  if (!overlay) return;
  
  // Remove blocking listeners so clicks can dismiss
  if (overlay._blockers) {
    overlay.removeEventListener('click', overlay._blockers.blockClick, true);
    overlay.removeEventListener('keydown', overlay._blockers.blockKey, true);
    overlay.removeEventListener('mousedown', overlay._blockers.blockMouse, true);
  }

  overlay.style.cursor = 'pointer';
  const content = document.getElementById('riskrunway-overlay-content');
  if (content) {
    content.style.pointerEvents = 'auto';
    content.style.cursor = 'pointer';
    content.innerHTML = `
      <div style="font-size: 24px; font-weight: 700; color: #e74c3c; margin-bottom: 10px;">
        ✗ Export Failed
      </div>
      <div style="font-size: 14px; color: #fca5a5;">
        ${errorMsg}
      </div>
      <div style="font-size: 12px; color: #6b7394; margin-top: 16px;">
        Click anywhere to dismiss
      </div>
    `;
  }
  overlay.addEventListener('click', () => overlay.remove());
}

// ─── Main Fill Logic ─────────────────────────────────────────────────────────

async function handleEnumerateAndFill({ job_id, server_url }) {
  try {
    // Show blocking overlay immediately
    showBlockingOverlay();

    let totalFilled = 0;
    let scrollAttempts = 0;
    const maxScrolls = 6;

    while (scrollAttempts <= maxScrolls) {
      // 1. Enumerate all empty form fields
      const fields = enumerateFields();
      
      if (fields.length === 0) {
        if (scrollAttempts === 0 && totalFilled === 0) {
          showErrorOverlay('No empty form fields found on this page');
          return { success: false, error: 'No form fields found on this page' };
        }
        break;
      }

      // 2. Send field list to server for AI matching
      const response = await fetch(`${server_url}/api/ams/extension-fill`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id, fields })
      });

      if (!response.ok) {
        const err = await response.json();
        showErrorOverlay(err.error || 'Server error');
        return { success: false, error: err.error || 'Server error' };
      }

      const data = await response.json();
      if (!data.success) {
        showErrorOverlay(data.error || 'Matching failed');
        return { success: false, error: data.error || 'Matching failed' };
      }

      // 3. Fill fields one by one with visual feedback (top to bottom)
      const fills = data.fills || {};
      let filledThisPass = 0;

      // Sort fills by vertical position on page so we fill top-to-bottom
      const sortedFills = Object.entries(fills).sort((a, b) => {
        const elA = resolveElement(a[0]);
        const elB = resolveElement(b[0]);
        const yA = elA ? elA.getBoundingClientRect().top : 0;
        const yB = elB ? elB.getBoundingClientRect().top : 0;
        return yA - yB;
      });

      for (const [selector, fillData] of sortedFills) {
        const filled = await fillFieldWithAnimation(selector, fillData);
        if (filled) filledThisPass++;
      }

      totalFilled += filledThisPass;

      // 4. Scroll down to reveal more fields
      const scrolled = scrollDown();
      if (!scrolled) break;
      
      scrollAttempts++;
      await new Promise(r => setTimeout(r, 500));
    }

    // Show review overlay
    showReviewOverlay(totalFilled);
    return { success: true, filled: totalFilled };

  } catch (error) {
    showErrorOverlay(error.message);
    return { success: false, error: error.message };
  }
}

// ─── Fill with animation ─────────────────────────────────────────────────────

async function fillFieldWithAnimation(selector, fillData) {
  const el = resolveElement(selector);
  if (!el) return false;

  const value = fillData.value;
  if (!value) return false;

  try {
    // Scroll into view only if not visible
    const rect = el.getBoundingClientRect();
    const inView = rect.top >= 0 && rect.bottom <= window.innerHeight;
    if (!inView) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      await new Promise(r => setTimeout(r, 300));
    }

    // Brief pause so user can see which field is being filled
    await new Promise(r => setTimeout(r, 150));

    // Fill the value
    if (el.tagName === 'SELECT') {
      const option = Array.from(el.options).find(opt => 
        opt.value.toLowerCase() === value.toLowerCase() ||
        opt.textContent.trim().toLowerCase() === value.toLowerCase()
      ) || Array.from(el.options).find(opt =>
        opt.textContent.trim().toLowerCase().includes(value.toLowerCase()) ||
        value.toLowerCase().includes(opt.textContent.trim().toLowerCase())
      );
      if (option) {
        el.value = option.value;
      } else {
        return false;
      }
    } else {
      el.value = value;
    }

    // Dispatch events
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));

    // Green highlight animation
    el.style.transition = 'all 0.3s';
    el.style.borderColor = '#059669';
    el.style.backgroundColor = 'rgba(5, 150, 105, 0.08)';
    el.style.boxShadow = '0 0 0 3px rgba(5, 150, 105, 0.2)';
    
    // Fade highlight after 2 seconds
    setTimeout(() => {
      el.style.borderColor = '';
      el.style.backgroundColor = '';
      el.style.boxShadow = '';
    }, 2000);

    return true;
  } catch (e) {
    console.error(`Failed to fill ${selector}:`, e);
    return false;
  }
}

// ─── Field Enumeration ───────────────────────────────────────────────────────

function enumerateFields() {
  const fields = [];
  
  // Collect fields from main document
  collectFieldsFromRoot(document, fields, '');
  
  // Collect fields from iframes
  const iframes = document.querySelectorAll('iframe');
  for (let i = 0; i < iframes.length; i++) {
    try {
      const iframeDoc = iframes[i].contentDocument || iframes[i].contentWindow?.document;
      if (iframeDoc) {
        collectFieldsFromRoot(iframeDoc, fields, `iframe[${i}]:`);
      }
    } catch (e) {
      // Cross-origin iframe — can't access, skip
    }
  }

  return fields;
}

function collectFieldsFromRoot(root, fields, prefix) {
  const elements = root.querySelectorAll('input, select, textarea');
  processElements(elements, fields, prefix, root);

  // Pierce Shadow DOM — check all elements for shadow roots
  const allElements = root.querySelectorAll('*');
  for (const el of allElements) {
    if (el.shadowRoot) {
      const shadowElements = el.shadowRoot.querySelectorAll('input, select, textarea');
      processElements(shadowElements, fields, prefix + 'shadow:', el.shadowRoot);
    }
  }
}

function processElements(elements, fields, prefix, root) {
  for (const el of elements) {
    if (el.type === 'hidden' || el.disabled || el.readOnly) continue;
    if (el.offsetParent === null) continue;
    if (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) continue;

    // Skip fields that already have data (except selects which always have a value)
    if (el.value && el.value.trim() !== '' && el.tagName !== 'SELECT') continue;

    const label = getFieldLabel(el, root);
    const selector = prefix + buildSelector(el, root);

    const fieldInfo = {
      selector: selector,
      tag: el.tagName.toLowerCase(),
      type: el.type || '',
      label: label,
      name: el.name || '',
      id: el.id || '',
      placeholder: el.placeholder || '',
      current_value: el.value || '',
    };

    if (el.tagName === 'SELECT') {
      fieldInfo.options = Array.from(el.options).map(opt => ({
        value: opt.value,
        text: opt.textContent.trim()
      }));
    }

    fields.push(fieldInfo);
  }
}

function getFieldLabel(el, root) {
  const queryRoot = root || document;
  if (el.id) {
    const label = queryRoot.querySelector(`label[for="${el.id}"]`);
    if (label) return label.textContent.trim();
  }
  const parentLabel = el.closest('label');
  if (parentLabel) return parentLabel.textContent.trim();
  if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
  const prev = el.previousElementSibling;
  if (prev && prev.tagName === 'LABEL') return prev.textContent.trim();
  const parent = el.parentElement;
  if (parent) {
    const prevSib = parent.previousElementSibling;
    if (prevSib && (prevSib.tagName === 'LABEL' || prevSib.classList.contains('label'))) {
      return prevSib.textContent.trim();
    }
    const sibLabel = parent.querySelector('label');
    if (sibLabel && sibLabel !== el) return sibLabel.textContent.trim();
  }
  return el.placeholder || el.name || el.id || '';
}

function buildSelector(el, root) {
  if (el.id) return `#${el.id}`;
  if (el.name) return `[name="${el.name}"]`;
  const path = [];
  let current = el;
  const stopAt = (root && root.host) ? root.host : document.body;
  while (current && current !== document.body && current !== stopAt) {
    let selector = current.tagName.toLowerCase();
    if (current.id) {
      path.unshift(`#${current.id}`);
      break;
    }
    const parent = current.parentElement;
    if (parent) {
      const siblings = Array.from(parent.children).filter(c => c.tagName === current.tagName);
      if (siblings.length > 1) {
        const idx = siblings.indexOf(current) + 1;
        selector += `:nth-of-type(${idx})`;
      }
    }
    path.unshift(selector);
    current = current.parentElement;
  }
  return path.join(' > ');
}

function scrollDown() {
  const before = window.scrollY;
  window.scrollBy(0, window.innerHeight * 0.8);
  return window.scrollY > before;
}

// ─── Element Resolution (handles iframe/shadow prefixes) ─────────────────────

function resolveElement(selector) {
  // Handle iframe prefix: "iframe[0]:#fieldId" or "iframe[1]:[name="x"]"
  const iframeMatch = selector.match(/^iframe\[(\d+)\]:(.+)$/);
  if (iframeMatch) {
    const idx = parseInt(iframeMatch[1]);
    const innerSelector = iframeMatch[2];
    const iframes = document.querySelectorAll('iframe');
    if (idx >= iframes.length) return null;
    try {
      const iframeDoc = iframes[idx].contentDocument || iframes[idx].contentWindow?.document;
      if (!iframeDoc) return null;
      // Handle shadow inside iframe: "iframe[0]:shadow:#fieldId"
      const shadowMatch = innerSelector.match(/^shadow:(.+)$/);
      if (shadowMatch) {
        return findInShadowRoots(iframeDoc, shadowMatch[1]);
      }
      return iframeDoc.querySelector(innerSelector);
    } catch (e) {
      return null;
    }
  }

  // Handle shadow prefix: "shadow:#fieldId"
  const shadowMatch = selector.match(/^shadow:(.+)$/);
  if (shadowMatch) {
    return findInShadowRoots(document, shadowMatch[1]);
  }

  // Standard selector
  return document.querySelector(selector);
}

function findInShadowRoots(root, selector) {
  const allElements = root.querySelectorAll('*');
  for (const el of allElements) {
    if (el.shadowRoot) {
      const found = el.shadowRoot.querySelector(selector);
      if (found) return found;
    }
  }
  return null;
}
