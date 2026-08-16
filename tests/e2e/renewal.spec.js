const { test, expect } = require('@playwright/test');

async function login(page) {
  await page.goto('/login');
  await page.fill('#username', 'renewal_tester');
  await page.fill('#password', 'demo123!');
  await page.click('#loginButton');
  await expect(page).toHaveURL(/\/$/);
}

async function installBrowserClock(page) {
  await page.addInitScript(() => {
    const RealDate = Date;
    const getNow = () => {
      try {
        const raw = window.localStorage.getItem('__pw_test_now__');
        return raw ? Number(raw) : RealDate.now();
      } catch (error) {
        return RealDate.now();
      }
    };

    class MockDate extends RealDate {
      constructor(...args) {
        super(...(args.length ? args : [getNow()]));
      }

      static now() {
        return getNow();
      }
    }

    MockDate.parse = RealDate.parse;
    MockDate.UTC = RealDate.UTC;
    Object.setPrototypeOf(MockDate, RealDate);
    MockDate.prototype = RealDate.prototype;
    window.Date = MockDate;
  });
}

async function fastForwardBoardClock(page, days) {
  await page.evaluate((daysToAdvance) => {
    const current = new Date();
    current.setDate(current.getDate() + daysToAdvance);
    window.localStorage.setItem('__pw_test_now__', String(current.getTime()));
  }, days);
}

test.describe('Renewal Flow', () => {
  test('uses workflow status for board placement and shows renewal countdown states', async ({ page }) => {
    await installBrowserClock(page);
    await login(page);

    const atlasRow = page.locator('.runway-row', { has: page.locator('.card-name', { hasText: 'Atlas Manufacturing' }) });
    await expect(atlasRow.locator('.runway-lane[data-lane="submission"] .submission-card')).toBeVisible();
    await expect(atlasRow.locator('.quote-meta')).toContainText('Quotes received/sent: 1/0');

    await atlasRow.locator('.runway-lane[data-lane="submission"] .submission-card .card-name').click();
    await page.getByRole('button', { name: 'Submit to Market' }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(
      page.locator('.runway-row', { has: page.locator('.card-name', { hasText: 'Atlas Manufacturing' }) })
        .locator('.runway-lane[data-lane="quoting"] .submission-card')
    ).toBeVisible();

    const beaconRow = page.locator('.runway-row', { has: page.locator('.card-name', { hasText: 'Beacon Logistics' }) });
    await expect(beaconRow.locator('.runway-lane[data-lane="quoting"] .submission-card')).toBeVisible();
    await expect(beaconRow.locator('.renewal-meta')).toContainText(/Up for renewal: T-\d+d/);

    const deltaRow = page.locator('.runway-row', { has: page.locator('.card-name', { hasText: 'Delta Services' }) });
    await expect(deltaRow.locator('.runway-lane[data-lane="bind"] .submission-card')).toBeVisible();

    const cascadeRow = page.locator('.runway-row', { has: page.locator('.card-name', { hasText: 'Cascade Foods' }) });
    await expect(cascadeRow.locator('.runway-lane[data-lane="quoting"] .submission-card')).toBeVisible();
    await expect(cascadeRow.locator('.renewal-meta')).toHaveText('Countdown till renewal: lapsed');
  });

  test('moves a bound submission back to quoting when renewal time is reached', async ({ page }) => {
    await installBrowserClock(page);
    await login(page);

    const frontierRow = page.locator('.runway-row', { has: page.locator('.card-name', { hasText: 'Frontier Warehousing' }) });
    await expect(frontierRow.locator('.runway-lane[data-lane="quoting"] .submission-card')).toBeVisible();

    await frontierRow.locator('.runway-lane[data-lane="quoting"] .submission-card .card-name').click();
    await page.getByRole('button', { name: 'Bind' }).click();
    await page.goto('/');

    const frontierBoundRow = page.locator('.runway-row', { has: page.locator('.card-name', { hasText: 'Frontier Warehousing' }) });
    await expect(frontierBoundRow.locator('.runway-lane[data-lane="bind"] .submission-card')).toBeVisible();

    await fastForwardBoardClock(page, 31);
    await page.reload();

    const frontierRenewalRow = page.locator('.runway-row', { has: page.locator('.card-name', { hasText: 'Frontier Warehousing' }) });
    await expect(frontierRenewalRow.locator('.runway-lane[data-lane="quoting"] .submission-card')).toBeVisible();
    await expect(frontierRenewalRow.locator('.renewal-meta')).toContainText('Up for renewal');
  });
});
