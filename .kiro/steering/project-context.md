# RiskRunway Project Context

## What This Is
RiskRunway is a SaaS workflow and quoting platform for surplus lines (SL) insurance agents. It sits between submission receipt and policy binding. Core pipeline: submission intake → quote PDF parsing/comparison (kanban) → bind → export to AMS. Built on Python/Flask, hosted on AWS (ECS Fargate + RDS PostgreSQL). UI is Tailwind + Vanilla JS.

Core problem: commercial insurance workflows are fragmented. Agents juggle quotes in email, track binds on paper, re-key data into AMS systems, no visibility across departments. RiskRunway fixes that with a single pipeline UI.

Positioning: NOT marketed as an AMS replacement (yet). Integrates with Applied Epic, AMS360, etc. — import clients/quotes, do the quoting/comparison work, export back to AMS.

## Key People
- **Chris Bouy** — builder, product owner, chrisbouy@gmail.com, 225-210-2064
- **Brooke Bouy** — co-founder, premium finance professional, industry connections. Has a Windows laptop.


## Current Business Status
- **Pilot agency LOST**: Paulin Insurance Associates (Bryce/Alyssa) was the pilot. Applied Systems sales called the agency directly (after Chris asked them to route to him), causing the agency to fear their Epic license was at risk. Not Chris's fault — Applied's internal handoff was the problem.
- **Classic Insurance** (chris, brooke, korrin) — current active pilot tenant on `classic.risk-runway.com`
- Need to find additional pilot agencies through Brooke's network. Ideal: agency that already has Applied API access or is willing to use RiskRunway for quoting/comparison without AMS export while that's being developed.

## The Three-Stage Kanban Workflow
Cards represent insureds, move left to right. Each stage has a distinct color.

**Stage 1 — Submission (blue)**: Client info, coverage types, effective date. Document upload. "Submit to Markets" → emails application to brokers, moves card to Quoting.

**Stage 2 — Quoting (orange)**: Quote PDF upload + AI extraction. Side-by-side comparison table (color-coded per carrier). Auto-calculated financial summary. "Bind" → moves card to Bound.

**Stage 3 — Selection & Bind (green)**: Selected quote locked in. Binder status tracking. "Export to AMS" action. Finance agreement generation (not yet implemented).

**Card lifecycle (circular)**: Created → Submission → Quoting → Bound → [120 days before expiration] → Quoting (renewal) → repeat. Renewal card shows countdown. Prior bound quote carries forward as reference.

## Developer Environment
- Python virtual environment: `source "/Users/chrisbouy/code_base/IPFS Mapper/myenv/bin/activate"`
- Local Postgres databases: riskrunway (prod mirror), riskrunway_dev (development), riskrunway_use_cases, riskrunway_test
- App runs locally on port 5001: `flask run --port 5001`
- No Docker needed for local dev — Docker is only for prod builds via GitHub Actions
- Deploy: push to `main` → GitHub Actions builds image → pushes to ECR → deploys to ECS
- AWS CLI user: `ams-agent-dev` (has read + write access)
- **Important**: Space in project folder name ("IPFS Mapper") breaks aws CLI when run from virtualenv — always deactivate first
- **Important**: zsh and exclamation marks — always use single quotes for strings containing `!`

## Architecture

### Storage
- Dual-provider: local filesystem (`./uploads`) or AWS S3 (`riskrunway-uploads` bucket)
- Controlled by `STORAGE_PROVIDER` env var (local in dev, s3 in prod)
- Storage key format: `{tenant}/{insured_name}/{type}_{filename}`
- Config: `UPLOAD_FOLDER = './uploads'`, max 16MB, allowed: pdf/png/jpg/jpeg
- STORAGE_PROVIDER=s3 must be set in ECS task or uploaded files will be lost on container restart

### Multi-Tenancy
- Database-per-tenant on same RDS instance (db.t3.micro, Postgres 15)
- Tenant resolved from subdomain: `classic.risk-runway.com` → `riskrunway_classic` database
- Routing: `database.py` has `resolve_tenant_from_host()`, `get_tenant_database_url()`
- `TENANT_DATABASE_MAP` env var holds JSON mapping of tenant → DB URL
- `BASE_DOMAIN=risk-runway.com`
- S3 keys prefixed by tenant name
- Wildcard cert already covers any new subdomains

### Current Tenants
- `default` → `riskrunway` database (Chris's dev/test, accessed via `app.risk-runway.com`)
- `classic` → `riskrunway_classic` database (Classic Insurance pilot)
  - Users: chris, brooke, korrin (all Admin, password: Classic2026!)
- `paulin` — DROPPED (pilot fell through, DB can be deleted, do not restore)

### DNS Layout
- `risk-runway.com` → CloudFront (marketing site) — never touch
- `app.risk-runway.com` → ALB → ECS (the Flask app)
- `www.risk-runway.com` → CloudFront (marketing site) — fixed during teardown, was incorrectly pointing at ALB
- `classic.risk-runway.com` → ALB → ECS (same app, different DB)

### Key AWS Resources
- Account ID: 703671916421
- Region: us-east-1
- ECS cluster: `riskrunway`, service: `riskrunway-service`, task def family: `riskrunway`
- ECS config: 1024 CPU / 2048 MB memory, Fargate, port 5001
- ECR repo: `703671916421.dkr.ecr.us-east-1.amazonaws.com/riskrunway-mapper`
- RDS: `riskrunway-db` (endpoint: `riskrunway-db.cu54eyu4cy2j.us-east-1.rds.amazonaws.com`, user: `riskrunway`, pass: `RiskRunway2026!`)
- ALB: `riskrunway-alb`, SG: `sg-025209b993acf6cf0`
- ECS SG: `sg-0ea045d25d7e220d6`, RDS SG: `sg-0cf6d2d2e3b4d7813`
- S3: `riskrunway-uploads` (documents + agent setup files), `risk-runway-site` (marketing)
- 14 secrets in Secrets Manager (prefixed `riskrunway/`)
- ACM cert: wildcard `*.risk-runway.com` (ARN: `arn:aws:acm:us-east-1:703671916421:certificate/b639d555-aff4-42e0-a9c0-c77889f70f6a`)
- CloudFront: `E1MVD7UJM45V89` / `d2v7sob7c0452c.cloudfront.net`
- Route 53 hosted zone: `Z06154331VLPY7FN6TJRH`
- IAM execution role: `riskrunway-ecs-execution-role`
- Bedrock model: `us.anthropic.claude-sonnet-4-20250514-v1:0` (all Claude 3.x are legacy on this account)

### IAM Execution Role Permissions
- AmazonECSTaskExecutionRolePolicy (managed) — ECR pull + CloudWatch logs
- secretsmanager:GetSecretValue on `riskrunway/*`
- s3:PutObject, s3:GetObject, s3:DeleteObject on `riskrunway-uploads/*`
- bedrock:InvokeModel on `*`
- logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents on `*`

## Document Types & Parsing

### Three-Stage Parsing Pipeline
1. **Submission stage (ACORD 125 parse)** — `application_parser.py`
   - Extracts: insured name, address, state, effective date, coverage types
   - Fast (2-3 pages, simple schema)
   - Result stored in `Submission.submission_intake` (JSON column)

2. **Quoting stage (quote parse)** — `two_pass_parser.py`
   - Pass 1: Extract layout (text for digital PDFs, page images for scanned, max 5 pages)
   - Pass 2: LLM normalizes to JSON schema (carrier, premium, dates, fees, MGA, financing)
   - Result stored in `Quote.extracted_json`
   - Pre-fills from app parse data (insured info, effective date)
   - Quote fields are editable in both Quoting and Bind stage to correct OCR errors

3. **Export stage (Epic export parse)** — `epic_export_parser.py`
   - Always uses vision path (page images) regardless of PDF type
   - Processes ALL pages (no 5-page cap) — limits often on page 6+
   - Extracts SDK-specific fields: commission %, policy number, line type code
   - Does NOT parse coverage limits (Epic SDK has no endpoint to write them)
   - Pre-fills from both previous parses, only asks LLM for non-overlapping data

### Quote Data Schema (key parsing notes)
- "Minimum earned" (e.g. 25%) = cancellation penalty. "Minimum and advance premium" (e.g. 100%) = full premium due upfront. Different fields.
- "Certain underwriters at Lloyd's and other insurers" = carrier (Security field). Individual underwriter name is separate.
- "Renewal of #" = prior policy number, important for renewal tracking.
- Submission # = MGA's internal reference.
- Capture both contact_name and contact_email on broker/MGA objects.

### Document Model
- `Document` table: tracks files with storage_provider, storage_key, document_type, version
- Types: APPLICATION, SOV, LOSS_RUN, QUOTE, BINDER, FINANCE_AGREEMENT, CORRESPONDENCE, OTHER
- Note: historically all docs saved as APPLICATION regardless of type (known issue in comment)
- `submission_intake` lives on Submission model (not in audit logs — that was the old anti-pattern)

## Data Transfer Methods (Export to AMS)

### Architecture (current — as of 7/1/26)
One "Export to AMS" button. Two paths depending on whether target is web-based or desktop:

**Web-based AMS (primary path):** Chrome extension (`chrome-extension-ams-fill/`)
- Extension popup shows list of open tabs → user picks AMS tab
- Content script enumerates all empty form fields (inputs, selects, textareas) with labels/types/options
- Sends field list + job_id to server `POST /api/ams/extension-fill`
- Server loads quote page images, sends to Claude: "match these fields to quote data"
- Returns fill map: {selector → value}
- Extension fills fields via DOM with visual animation (scroll into view, green highlight, 150ms delay between fills)
- Full-page blocking overlay during fill: "Exporting... Do not click or type"
- On complete: "Review Transferred Data Before Saving" (large, prominent)
- Extension manifest `externally_connectable` must include `https://*.risk-runway.com/*` (hyphen!)
- Extension needs quote page images accessible on prod (S3, not local paths)
- DOM access cannot be blocked by the AMS vendor — Chrome extensions bypass CSP
- Potential issues: Shadow DOM (needs piercing), iframes (need separate injection), custom dropdowns (non-native `<select>`)

**Desktop AMS (fallback — via `riskrunway://` protocol):** Local agent (`local_agent.py`)
- Pass 1 (vision/textboxes): Screenshot → Claude returns textbox coordinates + values → pyautogui click+paste
  - +18px Y offset applied to all fields (Claude points at labels, not inputs)
  - No select-all before paste, no AX accessibility attempt, no verification loop
  - Single click + paste per field, no retries
- Pass 2 (computer-use/dropdowns): Anthropic Computer Use agentic loop
  - Model: `us.anthropic.claude-sonnet-4-6` with `computer-use-2025-11-24`, tool type `computer_20251124`
  - Screenshots resized to 1280x720, display_width/height match, coordinates scaled back to native
  - Scroll: pyautogui.press('pagedown') + pyautogui.hotkey('fn', 'down') (covers Windows + Mac)
  - `wait` action supported (capped 5s), `triple_click` supported
  - Prompt warns about "exporting..." spinner overlay
  - Loop stops when: Claude responds without tool_use, timeout (45s), or max steps (30)
- Launched via `riskrunway://export?job_id=X&server=Y` protocol URL → RiskRunwayLauncher app

### Test Pages for Extension
Located in `sample_docs/misc/`:
- `test_obfuscated.html` — random IDs, labels via adjacent text
- `test_shadow_dom.html` — fields inside Shadow DOM (extension FAILS currently)
- `test_iframe.html` — form inside iframe
- `test_react_form.html` — controlled inputs with state tracking
- `test_custom_dropdowns.html` — non-native div dropdowns (extension FAILS currently)

### Epic API Export (Applied Epic specifically)
- Location: `epic_routes.py`, `epic_client.py`
- Endpoint: `POST /api/epic/prepare-export/<id>` (parse + confirmation modal) → `POST /api/epic/export/<id>` (execute)
- Uses SDK Module (`/sdk/v1`) for PUT /lines and PUT /policies
- Conversion API available for bridging SDK ↔ REST API IDs
- Confirmation popup shows editable fields before pushing
- Attaches all documents via attachment API
- Does NOT push coverage limits (no API support — lives in Epic's Policy Detail Engine)
- **Production access blocked**: Applied requires agency to purchase separate API license. Ticket #05869154 with Robert Rae at Applied support. On hold until new pilot found.
- SDK files on hand: `applied-epicSDK-classic-v1.yml`, `applied-epic-policy-v1.txt`, `applied-epic-policy-v2.txt`

### "Import from Epic" button — currently HIDDEN (display:none on kanban)

### Key Design Decisions
- RiskRunway has NO knowledge of what the target AMS looks like. AI figures out matching at export time.
- Quote PDF page images are the source of truth during export (not pre-extracted JSON). AI reads the actual document.
- Anchor Browser (competitor) extracts data OUT of AMS using headless browsers. We push data IN via user's live authenticated session — fundamentally different approach.

## Email System
- Sends via Microsoft Outlook Graph API (OAuth)
- `_send_email_via_oauth()` handles attachments from both local and S3
- Email scraping: polls OAuth inbox for broker replies, filters by broker emails + insured names + has_attachments
- Submit to Market: sends to configured brokers with selected document attachments
- Known: documents with "quote", "indication", or "proposal" in filename are unchecked by default in send modal
- Email scraper downloads attachments to S3 via the storage provider abstraction (same as document uploads)
- Emails are NOT saved in RiskRunway — only parsed/ingested or discarded

## SMS / Text Message Integration
- AI agent with a Twilio phone number
- When email arrives matching broker filters → texts agent with AI summary
- Agent can text back "draft" to have AI draft a reply (with confirmation before sending)
- Twilio A2P 10DLC registration: one Brand (Risk Runway LLC, EIN in hand), one Campaign (transactional alerts)
- Twilio trial account set up. Low-Volume Standard tier (~$4.50 brand + $15 campaign vetting + $1.50-10/mo)
- Confirmation loop required: AI never auto-sends email on agent's behalf without explicit text confirmation
- Built on existing OAuth email scraping infrastructure
- Uses Claude Haiku for summary (cheap), Sonnet only for draft reply
- Phone: +18882546161

## Installer / Agent Setup
- S3 bucket: `riskrunway-uploads/agent-setup/`
- Files: `RiskRunwayLauncher.app.zip` (macOS), `RiskRunway-Windows-Setup.zip` (Windows)
- Requires `AMS_AGENT_S3_BUCKET=riskrunway-uploads` in env
- macOS installer served as .zip (preserves execute permissions via zipfile metadata)
- Windows installer served as .bat that downloads + extracts from S3 to `%LOCALAPPDATA%\RiskRunway`
- macOS .app bundle has a known recursive symlink issue from build_macos.sh (`ln -sf` creates infinite nesting) — needs fixing
- Users are mostly Windows
- Installer refers to the one-time setup download (Install-RiskRunway.command on Mac / .bat on Windows) that shows progress in a terminal window — user wants this replaced with a GUI progress bar eventually
- The launcher (RiskRunwayLauncher) runs the local_agent — it opens a minimized Terminal window on Mac to host the process. This is fine/intended behavior.
- To rebuild and re-upload Windows installer: `cp local_agent.py launcher/dist/RiskRunway-Windows-Setup/` → `cd launcher/dist && zip -r RiskRunway-Windows-Setup.zip RiskRunway-Windows-Setup/` → `cd ../.. && launcher/upload_to_s3.sh riskrunway-uploads`
- macOS Gatekeeper blocks unsigned .command files — right-click → Open bypasses it for testing
- Chrome Web Store: extension submitted but not published yet. For demos, load unpacked (`chrome://extensions` → Developer Mode → Load unpacked → select `chrome-extension-ams-fill/` folder)

## AWS Snapshot & Restore
- `scripts/aws_snapshot.sh` — dumps all AWS config to `aws_snapshot/` (JSON files, in .gitignore)
- `scripts/aws_teardown.sh` — tears down ECS/ALB/RDS (saves final RDS snapshot)
- `scripts/aws_restore.sh` — recreates from snapshot values
- Note: teardown script has zsh compatibility issues (silently fails with `> /dev/null 2>&1` pattern). If it fails, run commands manually step-by-step.
- Monthly cost when running: ~$60-75 (RDS $15-18, Fargate $25-35, ALB $16-22)
- Monthly cost when torn down: ~$6 (Secrets Manager + Route 53)

## Quote Parsing Performance
- Current best approach (as of 6/2/26): hybrid — Groq text-based LLM for digital PDFs, Groq vision (Llama 4 Scout) for scanned PDFs
- Digital PDFs: pdfplumber extracts full text → send to Groq generate_json (text-only, fast)
- Scanned PDFs: save page images → send to Groq vision model (generate_json_with_images)
- Page cap: first 5 pages max (hardcoded in pass1_extract_quote_layout)
- Best timings achieved: avg ~6s total (1.2-1.8s Pass 1 + 3.9-5.2s Pass 2)
- Bedrock (Claude Sonnet) was faster for small payloads (1-3 pages) but slower for larger ones
- Groq free tier has strict rate limits — upgrade to Developer/Enterprise pending (applied, waiting — Developer tier has been "temporarily unavailable" for 2+ months)
- Key insight: Pass 2 (LLM call) is always the bottleneck for digital PDFs, Pass 1 text extraction is instant
- For scanned PDFs: _find_last_relevant_page scans ALL pages with OCR to find financial data — this is slow (16s for rooster). The vision approach bypasses this entirely.
- Image resizing: page images resized to 1000px wide before sending to vision model (saves tokens, no quality loss for text reading)
- Settings: LLM_PROVIDER, GROQ_API_KEY, GROQ_MODEL, BEDROCK_MODEL, BEDROCK_REGION in settings.py (reads from .env)
- Bedrock model IDs need `us.` prefix for inference profiles (e.g., `us.anthropic.claude-sonnet-4-20250514-v1:0`)
- Legacy Bedrock models (claude-3-haiku, claude-3-5-haiku) are blocked — use `us.anthropic.claude-haiku-4-5-20251001-v1:0` for cheap/fast

## Trust & Certification Strategy
- **Microsoft Publisher Verification** — removes "This app isn't verified" warning from OAuth consent. Requires MPN account (free). DO NOW.
- **AWS Partner Network** — free badge for website/materials. DO NOW.
- **Applied Epic App Exchange listing** — highest value trust signal for the Epic segment
- **E&O insurance** — table stakes for enterprise
- Small agencies trust: IIABA/PIA association recommendations, peer agent referrals, Applied Epic App Exchange listing, E&O coverage more than AWS/Microsoft badges

## Subjectivities (Binding Requirements)
- Quotes often list "subjectivities" — documents required prior to binding (e.g., signed ACORD, loss runs, MVRs)
- Pass 2 schema includes `subjectivities` field (array of strings or null)
- Parser prompt instructs LLM to strip redundant trailing qualifiers ("- PRIOR TO BINDING") when all items share the same one
- Stored in both `Quote.extracted_json` (within the JSON) and `Quote.subjectivities_json` (dedicated column for fast access)
- `Quote.subjectivities_checked` — JSON array of booleans tracking which items are satisfied (bind stage checklist)
- API: `PUT /api/quote/<id>/subjectivities` — update list or checked state
- Frontend display: `cleanSubjectivities()` strips common trailing timing qualifiers at display time (handles legacy data)
- Quoting stage: "📋 Subjectivities" link under each row in Quotes by Broker table → opens modal
- Bind stage: checklist card with checkboxes (2-column grid layout), items strike-through when checked
- Fallback: when a quote has no subjectivities, uses tenant-configured defaults (from TenantSettings)
- Modal allows adding/removing custom subjectivities per quote

## Tenant Settings
- Model: `TenantSettings` (single-row key-value JSON store per tenant database, `tenant_settings` table)
- Auto-created on first PUT; `_add_missing_columns` not needed — `create_all` handles it
- API: `GET /api/tenant-settings` (any user), `PUT /api/tenant-settings` (admin only, merges into existing)
- Current settings keys:
  - `default_required_docs` — array of strings, fallback subjectivities for bind checklist
  - `custom_required_docs` — array of custom additions beyond the pre-built list
  - `renewal_highlight_threshold` — integer %, highlight renewal vs prior term differences above this
  - `quote_compare_threshold` — integer %, highlight quote-vs-quote differences above this
- UI: "⚙️ Quote Settings" button in kanban Settings dropdown → modal with 3 sections
- **Important**: threshold values can be 0 — do NOT use `|| defaultValue` (falsy), use `?? defaultValue` or explicit `!== undefined` checks
- Pre-built required docs list (15 items) in kanban.html `COMMON_REQUIRED_DOCS` array

## Quote Comparison Highlighting
- Policy Details table compares numeric columns (premium, tax, fee, broker_fee) across quotes
- Groups quotes by coverage type — only compares quotes with matching coverage
- If all quotes have different coverages (no groups), compares all against each other
- Higher value → red tint + red left border; lower value → green tint + green left border
- Only highlights when `pctDiff > quoteCompareThreshold`
- Percentage calculated as `|max - min| / |base| * 100` where base = min (or max if min is 0)
- `highlightQuoteDifferences()` runs after `populatePolicyDetailsTable()` builds rows
- `reapplyQuoteHighlighting()` called after tenant settings async load completes (race condition fix)
- Renewal comparison threshold stored but highlighting logic NOT yet implemented (TODO)

## Kanban Widgets
- "Needs Attention" widget field name: `s.quote_count` (not `quotes_count` — was a bug, fixed)
- `updateWidgets()` called after every `loadSubmissions()` — widgets refresh on any board action
- Docs dropdown z-index fix: `.submission-card:has(.docs-dropdown.open) { z-index: 100 }` elevates card when dropdown is open

## Known Issues & Gotchas
- `routes.py` is 7000+ lines — tools may truncate it. Search for specific functions.
- Table row hover effects disabled on submission.html (was distracting)
- All documents historically saved as `DocumentType.APPLICATION` — the type enum exists but isn't properly used on upload
- When deleting a submission, Document files are NOT cleaned from disk (only DB records cascade-delete)
- Storage keys are based on insured name, not submission ID — name collisions between deleted/recreated submissions
- The email "Check Email" button applies default filters (brokers + insured names + has_attachments) on initial load
- ProtonMail threads emails by subject — repeated test sends with same insured name show combined attachment previews
- AWS CLI v1 doesn't have `aws logs tail` — use `aws logs filter-log-events` instead
- CloudWatch filter patterns don't allow colons or slashes (`/`) — can't filter on URL paths directly
- CloudWatch logs only contain `[TENANT]` resolution lines — Flask werkzeug access logs (request method/path) are NOT captured
- `<unknown>:6142: SyntaxWarning: invalid escape sequence '\l'` — pre-existing, unrelated regex in routes.py
- RDS is publicly accessible — was enabled to run create_admin_user.py. Consider disabling after go-live.
- ~~Email scraper writes attachments to disk~~ — FIXED: now uses S3 storage provider on Fargate
- Mobile and desktop kanban do NOT share rendering logic — they have separate templates with independent JS. Fixes to one don't propagate to the other.


## Conventions
- LLM provider: Bedrock (Claude) in prod, Groq for quote parsing, configurable via `LLM_PROVIDER` env var
- Auto-migration: `_add_missing_columns()` in `database.py` handles schema updates without formal migrations
- User creation: `create_admin_user.py` in project root
- Tenant setup: `scripts/setup_classic.py` (force DATABASE_URL with `os.environ[...] = ...` BEFORE any app imports to avoid .env override)
- Test emails: cbouy@protonmail.com, chris.bouy@icloud.com
- OAuth redirect URIs: `https://app.risk-runway.com/oauth/outlook/callback`, `/oauth/gmail/callback`
- Marketing site deploy: `aws s3 sync /Users/chrisbouy/code_base/PF_Site s3://risk-runway-site --delete --exclude ".git/*" --exclude ".DS_Store"` then invalidate CloudFront
- `User.last_login_at` — tracks last successful login timestamp (added 8/4/26). Visible in `/api/users` response.

## Open Actions / TODO
- Apply for Microsoft Publisher Verification
- Join AWS Partner Network
- Get Epic import/export smoothed out in mock
- Applied Epic App Exchange listing
- Find new pilot agency (through Brooke's network)
- Get Applied Epic DB name and Epic ID from a real agency for production API use
- Figure out coverage limit workflow (agents enter manually in Epic — no API for it)
- Finance agreement generation (PFConverge integration — not yet implemented)
- SMS: Twilio A2P 10DLC brand/campaign registration

## Things NOT to Do
- Don't touch `risk-runway.com` DNS (marketing site on CloudFront)
- Don't restore the Paulin tenant (pilot fell through due to Applied miscommunication)
- Don't point `www.risk-runway.com` at ALB (it should point to CloudFront)
- Don't suppress AWS CLI errors with `> /dev/null 2>&1` in scripts (zsh issue)
- Don't assume Docker Desktop is needed — deploy via git push to main
- Don't use audit logs as a primary data store (use model columns instead)
- Don't use any Claude 3.x Bedrock model IDs — they're all legacy on this account
- Don't run aws CLI commands from inside the virtualenv (folder space issue)
