# E2E Renewal Tests (Playwright)

This suite validates the renewal workflow on the Kanban board:
- login
- renewal countdown/window display
- drag/drop lane transition
- status note persistence

## Install

```bash
npm install
npx playwright install chromium
```

## Run

```bash
npm run test:e2e
```

## Visual confidence artifacts

Every run creates a rich report and keeps debugging artifacts on failures:
- HTML report: `playwright-report/index.html`
- traces: `test-results/**/trace.zip`
- screenshots/video on failures: `test-results/**`

Open report:

```bash
npm run test:e2e:report
```
