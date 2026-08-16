// @ts-check
const { defineConfig, devices } = require('@playwright/test');
const path = require('path');
const fs = require('fs');

const testDbPath = path.join(__dirname, 'data', 'e2e-ipfs-mapper.db');
const configuredDbPath = process.env.DATABASE_PATH || testDbPath;
const shouldSeedTestDb = !process.env.DATABASE_PATH && process.env.PLAYWRIGHT_E2E_SEED !== '0';
const pythonBin = fs.existsSync(path.join(__dirname, 'myenv', 'bin', 'python'))
  ? path.join(__dirname, 'myenv', 'bin', 'python')
  : 'python3';
const webServerCommand = shouldSeedTestDb
  ? `PYTHONPATH=. DATABASE_PATH="${configuredDbPath}" "${pythonBin}" tests/e2e/setup_test_data.py && DATABASE_PATH="${configuredDbPath}" "${pythonBin}" run.py`
  : `PYTHONPATH=. DATABASE_PATH="${configuredDbPath}" "${pythonBin}" run.py`;

module.exports = defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  timeout: 60_000,
  expect: {
    timeout: 10_000
  },
  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }]
  ],
  use: {
    baseURL: 'http://127.0.0.1:5001',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    viewport: { width: 1600, height: 950 }
  },
  webServer: {
    command: webServerCommand,
    url: 'http://127.0.0.1:5001/login',
    timeout: 120_000,
    reuseExistingServer: !process.env.CI
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] }
    }
  ]
});
