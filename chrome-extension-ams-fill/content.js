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
      <div data-rr-status style="font-size: 12px; color: #6b7394; margin-top: 10px;">
        Starting...
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

// Update the sub-line of the blocking overlay so the user can see progress.
function setOverlayStatus(text) {
  const content = document.getElementById('riskrunway-overlay-content');
  if (!content) return;
  const sub = content.querySelector('[data-rr-status]');
  if (sub) sub.textContent = text;
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

// Max server round trips.
//
// Round 1 asks about every field on the page and the model answers every one of
// them explicitly (match or no-match), so there is nothing for a retry to
// "recover". Re-asking about a field the model already declined is not free:
// measured against a real quote, a second pass over the same fields produced
// mostly wrong values (the underwriter's name pushed into Account Executive, an
// operations description pushed into the Products/Completed Ops limit, a fee
// total the quote never stated). That is what the old 6-pass loop was doing.
//
// So round 2 exists only for selectors that did not exist during round 1 — a
// section the form revealed after being filled. Same fields are never re-asked.
const MAX_ROUNDS = 2;

async function handleEnumerateAndFill({ job_id, server_url }) {
  try {
    // Show blocking overlay immediately
    showBlockingOverlay();

    // Some AMS forms render sections lazily on scroll. Sweep the page once up
    // front so everything is in the DOM before we enumerate. This used to be
    // interleaved with the server calls, which cost one LLM call per scroll step.
    await revealLazyContent();

    const filledSelectors = new Set();
    const askedSelectors = new Set();
    let totalFilled = 0;

    for (let round = 1; round <= MAX_ROUNDS; round++) {
      // 1. Enumerate empty fields, skipping anything already filled or already
      //    declined by the model. `askedSelectors` is what prevents re-asking.
      const fields = enumerateFields(askedSelectors);

      if (fields.length === 0) {
        if (round === 1) {
          showErrorOverlay('No empty form fields found on this page');
          return { success: false, error: 'No form fields found on this page' };
        }
        // Nothing new appeared — done.
        break;
      }

      if (round > 1) {
        console.log(`[AMS Fill] ${fields.length} field(s) appeared after filling, matching those too`);
      }
      fields.forEach(f => askedSelectors.add(f.selector));

      setOverlayStatus(round === 1 ? 'Reading quote and matching fields...' : 'Matching newly revealed fields...');

      // 2. Send field list to server for AI matching
      const response = await fetch(`${server_url}/api/ams/extension-fill`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          job_id,
          fields,
          already_filled: Array.from(filledSelectors)
        })
      });

      if (!response.ok) {
        let msg = 'Server error';
        try { msg = (await response.json()).error || msg; } catch (e) {}
        // A failed top-up round should not discard a successful first round.
        if (round > 1 && totalFilled > 0) break;
        showErrorOverlay(msg);
        return { success: false, error: msg };
      }

      const data = await response.json();
      if (!data.success) {
        if (round > 1 && totalFilled > 0) break;
        showErrorOverlay(data.error || 'Matching failed');
        return { success: false, error: data.error || 'Matching failed' };
      }

      const fills = data.fills || {};
      const entries = Object.entries(fills);
      if (entries.length === 0) break;

      // 3. Fill fields with visual feedback, top to bottom.
      //    Resolve elements and cache positions once instead of per comparison.
      setOverlayStatus('Filling form...');
      const targets = entries
        .map(([selector, fillData]) => {
          const el = resolveElement(selector);
          return el ? { selector, fillData, el, y: absoluteTop(el) } : null;
        })
        .filter(Boolean)
        .sort((a, b) => a.y - b.y);

      // Keep the whole animation inside a fixed time budget so a 60-field form
      // is not 60x slower than a 5-field one.
      const perFieldDelay = Math.max(8, Math.min(60, Math.floor(1500 / Math.max(targets.length, 1))));

      let filledThisRound = 0;
      for (const target of targets) {
        const filled = await fillFieldWithAnimation(target, perFieldDelay);
        if (filled) {
          filledSelectors.add(target.selector);
          filledThisRound++;
        }
      }

      totalFilled += filledThisRound;

      // Nothing landed this round — another round will not do better.
      if (filledThisRound === 0) break;

      // Give the form a moment to render anything the fills unlocked, then the
      // next iteration checks whether any genuinely new field appeared.
      await new Promise(r => setTimeout(r, 250));
    }

    // Show review overlay
    showReviewOverlay(totalFilled);
    return { success: true, filled: totalFilled };

  } catch (error) {
    showErrorOverlay(error.message);
    return { success: false, error: error.message };
  }
}

// Scroll to the bottom and back to trigger lazy rendering / virtualized sections,
// then restore the original scroll position.
async function revealLazyContent() {
  const original = window.scrollY;
  const step = window.innerHeight * 0.9;
  let guard = 0;
  let last = -1;

  while (guard++ < 12 && window.scrollY !== last) {
    last = window.scrollY;
    window.scrollBy(0, step);
    await new Promise(r => setTimeout(r, 60));
    if (window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) break;
  }

  window.scrollTo(0, original);
  await new Promise(r => setTimeout(r, 60));
}

function absoluteTop(el) {
  return el.getBoundingClientRect().top + window.scrollY;
}

// ─── Fill with animation ─────────────────────────────────────────────────────

async function fillFieldWithAnimation(target, perFieldDelay) {
  const { el, fillData } = target;
  if (!el) return false;

  const value = fillData && fillData.value;
  if (!value) return false;

  try {
    // Scroll into view only if not visible. Instant scroll — 'smooth' forced a
    // 300ms wait per off-screen field, which dominated runtime on long forms.
    const rect = el.getBoundingClientRect();
    const inView = rect.top >= 0 && rect.bottom <= window.innerHeight;
    if (!inView) {
      el.scrollIntoView({ behavior: 'auto', block: 'center' });
    }

    // Brief pause so user can see which field is being filled
    if (perFieldDelay > 0) {
      await new Promise(r => setTimeout(r, perFieldDelay));
    }

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
    console.error(`Failed to fill ${target.selector}:`, e);
    return false;
  }
}

// ─── Field Enumeration ───────────────────────────────────────────────────────

function enumerateFields(filledSelectors) {
  const fields = [];
  const skip = filledSelectors || new Set();

  // Collect fields from main document
  collectFieldsFromRoot(document, fields, '', skip);

  // Collect fields from iframes
  const iframes = document.querySelectorAll('iframe');
  for (let i = 0; i < iframes.length; i++) {
    try {
      const iframeDoc = iframes[i].contentDocument || iframes[i].contentWindow?.document;
      if (iframeDoc) {
        collectFieldsFromRoot(iframeDoc, fields, `iframe[${i}]:`, skip);
      }
    } catch (e) {
      // Cross-origin iframe — can't access, skip
    }
  }

  return fields;
}

function collectFieldsFromRoot(root, fields, prefix, skip) {
  const elements = root.querySelectorAll('input, select, textarea');
  processElements(elements, fields, prefix, root, skip);

  // Pierce Shadow DOM — check all elements for shadow roots
  const allElements = root.querySelectorAll('*');
  for (const el of allElements) {
    if (el.shadowRoot) {
      const shadowElements = el.shadowRoot.querySelectorAll('input, select, textarea');
      processElements(shadowElements, fields, prefix + 'shadow:', el.shadowRoot, skip);
    }
  }
}

function processElements(elements, fields, prefix, root, skip) {
  for (const el of elements) {
    if (el.type === 'hidden' || el.disabled || el.readOnly) continue;
    if (el.offsetParent === null) continue;
    if (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) continue;

    // Skip fields that already have data (except selects which always have a value)
    if (el.value && el.value.trim() !== '' && el.tagName !== 'SELECT') continue;

    const label = getFieldLabel(el, root);
    const selector = prefix + buildSelector(el, root);

    // Selects always report a value, so the check above never excludes them.
    // Without this, every already-set dropdown gets re-sent to the model on each
    // round and re-answered forever.
    if (skip && skip.has(selector)) continue;

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
