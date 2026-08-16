const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');

const SAMPLE_DOCS_DIR = path.resolve(__dirname, '..', '..', 'sample_docs');
const FIXTURES_DIR = path.resolve(__dirname, 'fixtures', 'frog-quote-parsing');
const PARSING_SUBMISSION_NAME = 'Parsing Test';
const LOGIN_USERNAME = process.env.E2E_USERNAME || 'renewal_tester';
const LOGIN_PASSWORD = process.env.E2E_PASSWORD || 'demo123!';

function loadCases() {
  return fs.readdirSync(FIXTURES_DIR)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((fixtureFile) => {
      const baseName = path.basename(fixtureFile, '.json');
      const pdfPath = findSamplePdf(baseName);
      const expected = JSON.parse(
        fs.readFileSync(path.join(FIXTURES_DIR, fixtureFile), 'utf8')
      );
      return {
        baseName,
        fixtureFile,
        pdfPath,
        expected
      };
    });
}

function findSamplePdf(baseName) {
  const candidates = [
    path.join(SAMPLE_DOCS_DIR, `${baseName}.pdf`),
    path.join(SAMPLE_DOCS_DIR, `${baseName}.PDF`)
  ];

  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  throw new Error(`Could not find sample PDF for fixture "${baseName}" in ${SAMPLE_DOCS_DIR}`);
}

async function login(page) {
  await page.goto('/login');
  await page.fill('#username', LOGIN_USERNAME);
  await page.fill('#password', LOGIN_PASSWORD);
  await page.click('#loginButton');
  await page.waitForURL(/\/$/);
}

async function apiFetch(page, url, options = {}) {
  return page.evaluate(async ({ url: fetchUrl, options: fetchOptions }) => {
    const response = await fetch(fetchUrl, fetchOptions);
    const data = await response.json();
    return {
      ok: response.ok,
      status: response.status,
      data
    };
  }, { url, options });
}

async function getParsingSubmission(page) {
  const response = await apiFetch(page, '/api/submissions');
  expect(response.ok).toBeTruthy();
  expect(response.data.success).toBeTruthy();

  const submission = response.data.submissions.find(
    (item) => item.insured_name === PARSING_SUBMISSION_NAME
  );

  expect(
    submission,
    `Expected a seeded submission named "${PARSING_SUBMISSION_NAME}" in the active test database`
  ).toBeTruthy();

  return submission;
}

async function getSubmissionDetail(page, submissionId) {
  const response = await apiFetch(page, `/api/submission/${submissionId}`);
  expect(response.ok).toBeTruthy();
  expect(response.data.success).toBeTruthy();
  return response.data;
}

async function deleteAllQuotes(page, submissionId) {
  const detail = await getSubmissionDetail(page, submissionId);

  for (const quote of detail.quotes) {
    const response = await apiFetch(page, `/api/quote/${quote.id}`, {
      method: 'DELETE'
    });
    expect(response.ok).toBeTruthy();
    expect(response.data.success).toBeTruthy();
  }
}

async function uploadQuoteThroughUi(page, pdfPath) {
  const quoteInput = page.locator('input.dz-hidden-input[type="file"]');
  await expect(quoteInput).toHaveCount(1);
  await quoteInput.setInputFiles(pdfPath);
}

async function waitForParsedQuote(page, submissionId, timeoutMs = 4 * 60 * 1000) {
  const startedAt = Date.now();
  const intervals = [1000, 2000, 5000, 10000];
  let attempt = 0;

  while (Date.now() - startedAt < timeoutMs) {
    const current = await getSubmissionDetail(page, submissionId);
    if (current.quotes.length === 1) {
      const [quote] = current.quotes;
      if (quote.parsed_data) {
        return quote.parsed_data;
      }
    }

    const delay = intervals[Math.min(attempt, intervals.length - 1)];
    await page.waitForTimeout(delay);
    attempt += 1;
  }

  throw new Error(`Timed out waiting for parsed quote data on submission ${submissionId}`);
}

test.describe('quote parsing fixtures', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    const submission = await getParsingSubmission(page);
    await deleteAllQuotes(page, submission.id);
    await page.goto(`/submission/${submission.id}`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#uploadForm')).toBeVisible();
  });

  test.afterEach(async ({ page }) => {
    const submission = await getParsingSubmission(page);
    await deleteAllQuotes(page, submission.id);
  });

  for (const quoteCase of loadCases()) {
    test(`parses ${path.basename(quoteCase.pdfPath)} into the expected JSON`, async ({ page }) => {
      test.setTimeout(5 * 60 * 1000);

      const submission = await getParsingSubmission(page);
      await uploadQuoteThroughUi(page, quoteCase.pdfPath);

      const parsedData = await waitForParsedQuote(page, submission.id);

      expect.soft(
        parsedData,
        `Parsed JSON did not match fixture ${quoteCase.fixtureFile}`
      ).toEqual(quoteCase.expected);
    });
  }
});
