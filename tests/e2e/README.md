# E2E Tests (Playwright)

This suite currently covers:
- login
- renewal workflow checks
- quote parsing against fixture JSON

## Install

```bash
npm install
npx playwright install chromium
```

## Run

```bash
npm run test:e2e
```

By default, Playwright uses a dedicated automated-test database at:

```bash
data/e2e-riskrunway-mapper.db
```

That database is reset and reseeded at the start of each run unless you explicitly provide `DATABASE_PATH`.

To run against a manually seeded database instead of the disposable tmp DB:

```bash
DATABASE_PATH="/absolute/path/to/your.db" npm run test:e2e
```

When `DATABASE_PATH` is provided, Playwright will not call `tests/e2e/setup_test_data.py`.

The default seeded test DB includes:
- the renewal workflow submissions used by `renewal.spec.js`
- a `Parsing Test` submission in quoting used by `quote-parsing.spec.js`

## Quote Parsing Suite

The quote parsing suite expects a submission named `Parsing Test` already in `In Progress`.

If you use the default Playwright DB created by `tests/e2e/setup_test_data.py`, that submission is seeded automatically.

If you use your own database via `DATABASE_PATH`, keep a `Parsing Test` submission available in the quoting stage before you run the suite.

Expected parser output lives in:

```bash
tests/e2e/fixtures/quote-parsing/*.json
```

Each fixture file maps to a PDF in `sample_docs/` with the same basename. Example:

```bash
tests/e2e/fixtures/quote-parsing/quote_frogA.json
sample_docs/quote_frogA.pdf
```

The suite:
- logs in
- finds the seeded `Parsing Test` submission
- uploads one sample quote PDF through the submission page
- fetches `/api/submission/:id`
- compares `parsed_data` to the fixture JSON
- deletes all quotes from that submission after each test

Run only the quote parsing suite:

```bash
DATABASE_PATH="/absolute/path/to/your.db" npx playwright test tests/e2e/quote-parsing.spec.js
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
