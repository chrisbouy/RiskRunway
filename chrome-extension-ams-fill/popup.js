/**
 * RiskRunway AMS Fill — Popup Script
 * Shows a list of open tabs for user to select the AMS target.
 * Only works when there's a pending job (user clicked "Export to AMS" in RiskRunway).
 */

const tabListEl = document.getElementById('tabList');
const errorEl = document.getElementById('error');

// Check if there's a pending job
chrome.runtime.sendMessage({ action: 'get_pending_job' }, (job) => {
  if (!job) {
    tabListEl.innerHTML = '<div class="status">No pending export.<br><br>Click "Export to AMS" in RiskRunway first, then click this extension.</div>';
    return;
  }

  // Load tabs
  loadTabs();
});

function loadTabs() {
  chrome.tabs.query({}, (tabs) => {
    const eligibleTabs = tabs.filter(tab =>
      tab.url &&
      !tab.url.startsWith('chrome://') &&
      !tab.url.startsWith('chrome-extension://') &&
      !tab.url.startsWith('about:')
    );

    if (eligibleTabs.length === 0) {
      tabListEl.innerHTML = '<div class="status">No eligible tabs found</div>';
      return;
    }

    tabListEl.innerHTML = '';
    for (const tab of eligibleTabs) {
      const item = document.createElement('div');
      item.className = 'tab-item';
      item.innerHTML = `
        <div class="tab-title">${escapeHtml(tab.title || 'Untitled')}</div>
        <div class="tab-url">${escapeHtml(truncateUrl(tab.url))}</div>
      `;
      item.addEventListener('click', () => selectTab(tab));
      tabListEl.appendChild(item);
    }
  });
}

async function selectTab(tab) {
  tabListEl.innerHTML = '<div class="status" style="color:#4f8ef7">Exporting to this tab...</div>';

  // Switch to the selected tab
  await chrome.tabs.update(tab.id, { active: true });

  // Tell background to start filling
  chrome.runtime.sendMessage({
    action: 'fill_tab',
    tabId: tab.id
  }, (result) => {
    if (result && result.success) {
      tabListEl.innerHTML = `<div class="status" style="color:#059669">✓ Done — ${result.filled} fields filled</div>`;
    } else {
      showError(result ? result.error : 'Unknown error');
    }
  });

  // Close popup
  setTimeout(() => window.close(), 800);
}

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.style.display = 'block';
  tabListEl.innerHTML = '';
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function truncateUrl(url) {
  try {
    const u = new URL(url);
    return u.hostname + u.pathname.substring(0, 30);
  } catch {
    return url.substring(0, 40);
  }
}
