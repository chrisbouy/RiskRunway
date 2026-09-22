# Risk-Runway Cold Outreach — Campaign Status

_Human-readable status + handoff notes. The precise, authoritative record of
exactly who has been emailed is **`sent-log.csv`** (auto-maintained by the tool,
git-ignored). This file is the narrative; the CSV is the ledger. If they ever
disagree, trust `sent-log.csv`._

_Last updated: 2026-09-18_

## How sending works (read before sending)
- Tool: `send_outreach.py` (sends via Resend from `chris@try-risk-runway.com`).
- Dry run (no flag) writes previews to `preview/ALL_PREVIEWS.txt` — sends nothing.
- Real send needs `--send`. It sends up to `max_per_run` (in `config.ini`),
  pausing 45–90s between each, then stops.
- **Dedupe is automatic:** every address sent is written to `sent-log.csv`, and
  re-runs skip anyone already in it. This is what prevents double-sends. Do NOT
  hand-track who's been sent — let the CSV do it.
- To send the next batch: bump/confirm `max_per_run`, run `--send` again. It
  continues where it left off.

## Lists
| List | File | Sheet | Total unique | Status |
|---|---|---|---|---|
| SIAA / Tennessee | `SIAA List TN. - Brooke.xlsx` | `Brooke Tennessee` | 137 | IN PROGRESS |
| SIAA / National master | `SIAA List TN. - Brooke.xlsx` | `ISM List` (~4,800) | not started | not started |
| Mississippi / Iroquois | `Mississippi Iroquois member list.xlsx` | (single) | not started | `include_iroquois = no` |

## Progress
### SIAA / Brooke Tennessee (137 unique)
- **Sent so far: 75**
- **Remaining: 62**
- Batches:
  - Batch 1: 25 sent (contacts 1–25), 2026-09-18. 0 failures. Did NOT include
    the website link in the signature (link was added afterward).
  - Batch 2: 25 sent (contacts 26–50), 2026-09-21. 0 failures. First batch to
    include `https://risk-runway.com` in the signature.
  - Batch 3: 25 sent (contacts 51–75), 2026-09-22. 0 failures.
- All sends logged with Resend message ids in `sent-log.csv`.

### How to monitor delivery / spam (no code changes)
- Resend dashboard -> Emails/Logs shows per-message status: Delivered, Bounced,
  Complained (spam report), Deferred. This is the real delivery signal.
- Resend only shows messages that have ACTUALLY been sent. Mid-batch the tool
  drips ~1 email/45–90s, so the dashboard will show fewer than the batch size
  until the batch finishes. It also paginates and can lag a few seconds.
- Inbox-vs-spam placement is NOT visible to any sender. To gauge it, keep a seed
  address or two of your own across providers in a batch and check where it
  lands. (The pre-batch-1 test to 5 personal addresses all landed in inbox.)
- Opens/reads: NOT tracked (plain-text sends, no pixel — intentional, better for
  deliverability). Truest read signal = replies to chris@try-risk-runway.com.
- To reconcile counts: pick a Resend message id from sent-log.csv and search it
  in the Resend dashboard; confirm status = Delivered.
- None landed in spam across Gmail / iCloud / Outlook / Proton / risk-runway.com
  during the 5-address test send that preceded batch 1.

### Environment note (bit us once)
- The tool must run with a Python that has BOTH `pandas` and `openpyxl`. The
  `myenv` virtualenv was missing `openpyxl` and crashed a send attempt at load
  time (0 emails sent, no harm). Fixed by `python3 -m pip install openpyxl`
  inside the active env. If a future run errors with "No module named
  'openpyxl'", install it into whatever Python is active.

## Warmup plan (brand-new sending domain — ramp slowly)
| Day | Send count (`max_per_run`) |
|---|---|
| Day 1 (done) | 25 |
| Day 2 (done) | 25 |
| Day 3–4 | 40 |
| Day 5 | 75 |
| Day 6–7 | 100 |
| Week 2+ | 150–250/day if bounce/complaint rates stay clean |
- Keep the 45–90s delay. Watch `chris@try-risk-runway.com` inbox for bounces
  and replies. Slow down or pause if anything lands in spam or bounces spike.

## Sender / compliance (all set)
- From: `Chris @ Risk-Runway <chris@try-risk-runway.com>` (Resend-verified domain)
- Reply-to / unsubscribe: `chris@try-risk-runway.com` (M365 alias, receiving)
- Footer address (CAN-SPAM): `6554 Florida Blvd, Ste 110 #2132, Baton Rouge, LA 70806`
  (registered CMRA / PMB, Form 1583 notarized + verified). Keep `#2132` exactly.
- Template in use: `version_a` (`templates/version_a.txt`).

## Open items / notes
- Honor any "unsubscribe" reply the same day — remove them so they're skipped
  (add to sent-log or keep a suppression note) and never email them again.
- Personal-domain addresses (gmail/aol/yahoo/att.net) are scattered in the list;
  higher complaint risk than business domains — monitor.
- `version_b.txt` exists but is UNUSED (only `version_a` is sent). Safe to ignore.
