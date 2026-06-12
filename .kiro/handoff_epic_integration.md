# Epic Integration Handoff - RiskRunway

## Project Context

RiskRunway (RR) is a Flask-based insurance submission workflow tool. It tracks surplus lines insurance submissions through stages: Submission → Quoting → Binding. It serves as a marketing workspace between the agency's AMS (Applied Epic) and the MGAs/carriers.

The project is at `/Users/chrisbouy/code_base/IPFS Mapper/`.

## What Was Built (Applied Epic API Integration)

### Business Context

The pilot agency uses Applied Epic as their AMS. Agents create clients and generate ACORD 125 forms in Epic. The integration allows:
1. **Import from Epic**: Pull client data + ACORD 125 PDF + any quote PDFs from Epic into RR (one click)
2. **Export to Epic**: Push bound policy data + all collected documents back to Epic (one click)

This eliminates the manual download/upload process and solves the "people don't attach because there's too many steps" problem.

### Architecture

Two API layers in Epic:
- **REST API** (`/epic/client/v1`, `/epic/policy/v2`, `/epic/attachment/v2`): Modern HAL+JSON. Used for reads, attachment operations, creating policies/lines.
- **SDK Module** (`/sdk/v1`): Legacy SOAP-wrapper-over-REST. Has PUT on policies, lines, clients — the REST API doesn't have update/delete on policies or lines.

Auth: OAuth2 client_credentials flow. Token URL: `{base}/v1/auth/connect/token`.

### Files Created/Modified

**New files:**
- `app/epic_client.py` — API client singleton. Token management, all CRUD ops, file download/upload.
- `app/epic_routes.py` — Flask blueprint (`epic_bp`) with 6 endpoints.
- `epic_mock_server.py` — Local Flask app (port 5002) mimicking Epic API with real sample PDFs.
- `scripts/explore_epic_mock.py` — Utility to dump Applied's hosted mock data.

**Modified files:**
- `app/models.py` — Added to Submission: `ams_type`, `epic_client_id`, `epic_policy_id`, `epic_line_id`, `epic_exported_at`
- `app/database.py` — Added auto-migration for new columns in `_add_missing_columns()`
- `app/__init__.py` — Registered `epic_bp` blueprint
- `.env` — Added `EPIC_CLIENT_ID`, `EPIC_CLIENT_SECRET`, `EPIC_BASE_URL` (currently pointed at localhost:5002)
- `app/templates/kanban.html` — "Import from Epic" button + search modal + EPIC badge on cards + JS flow
- `app/templates/submission.html` — Modified Export to AMS button to detect Epic submissions and call Epic API export

### API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/epic/status` | Check if Epic configured |
| GET | `/api/epic/clients/search?q=X` | Search clients by name |
| GET | `/api/epic/clients/{id}/policies` | Get prospective policies for client |
| GET | `/api/epic/policies/{id}/lines` | Get lines for a policy |
| POST | `/api/epic/import` | Full import: pulls 125 + quotes, parses, creates submission |
| POST | `/api/epic/export/{submission_id}` | Full export: updates line/policy via SDK, attaches docs |

### Import Flow (what happens on POST /api/epic/import)

1. Receives client_id, policy_id, client_name, effective_date, etc.
2. Calls `GET /attachments?policy={id}&systemGenerated=true` → finds ACORD 125
3. Downloads PDF from `file.url`
4. Runs `process_application_two_pass()` → extracts insured name, state, effective date, coverage types
5. Creates Submission with parsed data + Epic IDs
6. Saves 125 PDF to uploads folder, creates Document record
7. Calls `GET /attachments?policy={id}&systemGenerated=false` → finds quote PDFs
8. Downloads each, runs `process_quote_two_pass()`, creates Quote records
9. If quotes found → sets status to IN_PROGRESS (Quoting column)

### Export Flow (what happens on POST /api/epic/export/{id})

1. Reads submission's Epic IDs
2. Builds line update payload (carrier, premium, policy number, status)
3. Calls `PUT /sdk/v1/lines` (SDK module)
4. Optionally calls `PUT /sdk/v1/policies`
5. For each document on the submission: `POST /attachments` → gets uploadUrl → `PUT` file bytes
6. Marks `epic_exported_at` timestamp

### Mock Server (epic_mock_server.py)

Currently has 2 demo clients:
- **Acme Manufacturing Corp** (`c-acme-0001`) — Prospect, GL policy, NOT_SUBMITTED stage, has ACORD 125 only
- **Tree Frogs Adventure Park, LLC** (`c-frogs-0001`) — Insured, Commercial Package, SUBMITTED stage, has ACORD 125 + quote_frogA.pdf

Files served from `sample_docs/Acme/` and `sample_docs/Frogs/`.

### Frontend (kanban.html)

The "Import from Epic" modal is a 3-step flow:
1. Search (type name → hit API → show results)
2. Select Policy (click client → shows their prospective policies)
3. Confirm (shows summary → click "Create Submission" → calls import API → navigates to new submission)

Cards from Epic get a small blue "EPIC" badge prefix on the kanban board.

### Frontend (submission.html)

The "Export to AMS" button in the Bind stage detects `submissionData.ams_type === 'epic'` and calls `executeEpicExport()` which POSTs to `/api/epic/export/{id}`. Button label changes to "Export to Epic" when it's an Epic submission.

---

## What Needs To Be Done Next

### 1. Quote filename display (IMMEDIATE)

The quote file saved during import currently uses a generated name like `CarrierName_2026-09-01.pdf`. The user wants it to show **MGA name + effective date - expiration date** instead of carrier name.

**Problem:** The parsed quote data extracts `carrier` (the insurer, e.g. "Nautilus") not the MGA (e.g. "Burns & Wilcox"). In surplus lines, the MGA is the wholesaler who brokers the deal.

**Options discussed:**
- Use the Epic attachment `description` field (which contains what the agent typed, like "Quote - Frog A MGA")
- Add MGA extraction to the quote parser
- Use `carrier_name` from the quote record as-is (it's often the MGA name depending on how the quote PDF is formatted)

**User preference:** MGA name / effective date - expiration date. Needs user decision on data source.

**Code location:** `app/epic_routes.py`, around line 310 in the quote import section. Look for `readable_filename`.

### 2. Filter import by assigned agent (NEXT)

User wants the "Import from Epic" search to default to showing only clients assigned to the logged-in agent, with a toggle to show all.

**What's needed:**
- Add `epic_employee_code` field to User model (maps RR user to Epic employee lookup code)
- Add employee servicing contact data to mock clients
- Update `GET /api/epic/clients/search` to accept `employee` param, pass to Epic API as `servicingContacts.employee`
- Update the modal UI to show "My Accounts" / "All Accounts" toggle
- Default to "My Accounts"

**Mock data assignments (per user):**
- `chrisbouy` → Tree Frogs (+ possibly "Wolf" - user mentioned but didn't confirm)
- `brookebouy` → Acme Manufacturing Corp

**Epic API filter:** `GET /clients?servicingContacts.employee={employee_uuid}` — the REST API supports this. On the SDK it's `ServicingRoleEmployeeCode`.

### 3. Add "Wolf" as third demo client (MAYBE)

User mentioned Wolf but didn't confirm details. There's a `sample_docs/quote_wolf.pdf` file. May need a Wolf client with its own 125 + that quote.

### 4. Production deployment prep

When ready for real agency connection:
1. Submit production app request on Applied Dev Center (needs agency's Enterprise ID + database name)
2. Wait for Applied approval
3. Swap `EPIC_BASE_URL` in .env to production URL
4. Swap `EPIC_CLIENT_ID` / `EPIC_CLIENT_SECRET` to production credentials
5. The `file.url` on attachments will point to `documentservice.appliedsystems.com` — code already handles this (just downloads from whatever URL)

Real credentials are already in .env for Applied's hosted mock:
```
EPIC_CLIENT_ID=nHAsy9UBHyCCJ2y1RlpssLKEOtvpMWGngBrb17SoQpaMmoLJ
EPIC_CLIENT_SECRET=pKO1vc1SfWOGWnCj1UBSf40daqzTZuJ5fQ4yga5XRLHudXhA46rpDi10vblVUaJA
```

Currently pointed at local mock: `EPIC_BASE_URL=http://localhost:5002`

---

## Key Design Decisions Made

1. **Epic users vs non-Epic users**: Same core app, different connectors. `ams_type='epic'` on submission flags it. Non-Epic users continue using manual entry or ACORD PDF upload.
2. **Don't remove local agent functionality**: Epic integration is additive. The existing "Upload Application" flow still works.
3. **SDK Module for updates**: The newer REST Policy API (v2) only has GET/POST on lines/policies. The SDK Module at `/sdk/v1` has PUT. Both are needed.
4. **One export point**: User exports to Epic only after everything is bound and ready for finance (not after each stage).
5. **Import pulls everything**: 125 + any quotes. Places card in correct kanban column based on whether quotes exist.

---

## Relevant File Paths

```
app/epic_client.py          — API client (token, search, CRUD, download, upload)
app/epic_routes.py          — Flask routes for Epic integration
epic_mock_server.py         — Local mock (port 5002), serves sample PDFs
app/models.py               — Submission model with Epic fields
app/database.py             — Schema migration for new columns
app/__init__.py             — Blueprint registration
app/templates/kanban.html   — Import button + modal + EPIC badge
app/templates/submission.html — Export button logic
.env                        — Epic credentials + base URL
sample_docs/Acme/           — ACORD_125_Application.pdf
sample_docs/Frogs/          — ACORD125.pdf, quote_frogA.pdf, quote_frogB.pdf
scripts/explore_epic_mock.py — Utility to dump Applied's hosted mock data
```

## Running the Demo

Terminal 1: `python epic_mock_server.py` (port 5002)
Terminal 2: Start RR as usual (port 5001)

1. Click "Import from Epic" on kanban
2. Search "acme" or "tree" or "frog"
3. Pick client → pick policy → confirm
4. Card created with parsed data + documents attached
