/**
 * RiskRunway AMS Fill — Background Service Worker
 * 
 * Handles two triggers:
 * 1. External message from RiskRunway web app (with job_id + server_url)
 * 2. Internal message from popup (user selected a tab)
 */

let pendingJob = null;

// Listen for messages from the RiskRunway web app (externally_connectable)
chrome.runtime.onMessageExternal.addListener((message, sender, sendResponse) => {
  if (message.action === 'fill_ams') {
    pendingJob = {
      job_id: message.job_id,
      server_url: message.server_url
    };

    // Open tab picker as a small popup window
    chrome.windows.create({
      url: 'popup.html',
      type: 'popup',
      width: 320,
      height: 400,
      top: 100,
      left: 100
    });

    sendResponse({ success: true, message: 'Tab picker opened.' });
  }

  if (message.action === 'get_pending_job') {
    sendResponse(pendingJob);
  }
});

// Listen for internal messages (from popup)
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'fill_tab') {
    handleFillTab(message.tabId, sendResponse);
    return true; // async
  }

  if (message.action === 'get_pending_job') {
    sendResponse(pendingJob);
    return false;
  }
});

async function handleFillTab(tabId, sendResponse) {
  if (!pendingJob) {
    sendResponse({ success: false, error: 'No pending export job. Click "Export to AMS" in RiskRunway first.' });
    return;
  }

  const { job_id, server_url } = pendingJob;

  try {
    // Inject the content script
    await chrome.scripting.executeScript({
      target: { tabId: tabId },
      files: ['content.js']
    });

    // Tell content script to enumerate and fill
    const result = await chrome.tabs.sendMessage(tabId, {
      action: 'enumerate_and_fill',
      job_id: job_id,
      server_url: server_url
    });

    console.log('[AMS Fill] Content script result:', result);

    // Clear pending job
    pendingJob = null;

    sendResponse(result || { success: false, error: 'No response from content script' });
  } catch (error) {
    sendResponse({ success: false, error: error.message });
  }
}
