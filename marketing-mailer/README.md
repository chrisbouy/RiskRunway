# Risk-Runway cold-outreach mail merge (Resend)

A small command-line tool that reads the SIAA / Iroquois agency spreadsheets,
writes one personalized email per contact (their first name + agency name), and
sends them **slowly and safely** through [Resend](https://resend.com) from your
own marketing domain (`try-risk-runway.com`).

It is built to protect your sending reputation, not blast a list. Read this
whole file once before you run anything.

> **This tool is standalone.** It is NOT part of the RiskRunway Flask app and
> does not deploy to ECS. It runs on your own computer and talks to Resend's
> API over HTTPS. No AWS infrastructure is involved in sending.

---

## What it does

- Pulls contacts from the two spreadsheets in this folder.
- Personalizes each email with `{first_name}` and `{company}`.
- **Default mode is a dry run**: it writes each email to a `preview/` folder so
  you can read them. It sends nothing.
- Sending for real requires the `--send` flag **and** typing `SEND` to confirm.
- Throttles: a random pause (default 45-90 sec) between every email, and a hard
  cap per run (default 40). This is what keeps you out of the spam folder.
- Keeps a `sent-log.csv` so re-running never emails the same person twice.
- Adds the legally required CAN-SPAM footer (your physical address + an
  unsubscribe line) to every message.

---

## One-time setup

### 1. Own and verify the sending domain in Resend

You've registered **`try-risk-runway.com`** (separate from `risk-runway.com` on
purpose — cold-email reputation must stay off your app's domain).

1. In Resend: **Domains -> Add Domain -> `try-risk-runway.com`**.
2. Resend shows a set of DNS records (an SPF `TXT`, a few DKIM `CNAME`s, a
   DMARC `TXT`, and usually a return-path `MX`/`CNAME`).
3. Add those records to the domain's **Route 53 hosted zone**. (Since the
   domain is registered in Route 53, this is quick — Kiro can add them via the
   AWS CLI for you.)
4. Wait until Resend shows the domain as **Verified** (minutes to a couple
   hours). You cannot send until it's verified.

### 2. Get a Resend API key

Resend -> **API Keys** -> create one. It looks like `re_xxxx…`.

### 3. Install Python + libraries

```
python3 -m pip install -r requirements.txt
```

### 4. Make your config

```
cp config.example.ini config.ini
```

Open `config.ini` and fill in:
- `[resend] api_key` — your Resend key
- `[sender] from_email` — an address on `try-risk-runway.com`
- `[sender] reply_to` — a mailbox you actually read (replies land here)
- `[sender] physical_address` — your **real** mailing address (required by law)
- `[data] siaa_sheets` — which spreadsheet tab(s) to email

`config.ini` is git-ignored — it holds your API key, never commit it.

---

## Running it

**Step 1 — dry run (safe, sends nothing):**
```
python3 send_outreach.py
```
Fills the `preview/` folder with one `.txt` per email. Open a handful and read
them. Check the name, the agency name, and the footer look right.

**Step 2 — try a tiny real batch first.** Send just a few to yourself:
```
python3 send_outreach.py --send --limit 3
```
It shows a summary and waits for you to type `SEND`. Anything else aborts.

**Step 3 — send for real, in throttled batches:**
```
python3 send_outreach.py --send
```
Sends up to `max_per_run` (default 40), pausing randomly between each, then
stops. Run it again later to continue — already-emailed contacts are skipped
automatically via `sent-log.csv`.

---

## Warmup (important for a cold list of ~5,000)

A brand-new domain that suddenly sends thousands looks exactly like a spammer.
Ramp gradually:

- **Days 1-3:** `max_per_run = 20-40`, once a day.
- **Week 1-2:** raise by ~50/day if replies look healthy and bounces stay low.
- Keep `min_delay`/`max_delay` at 45-90 sec or higher.
- Honor every unsubscribe the same day.
- Never add scraped/purchased lists on top of this.

At one email every ~5 minutes, 5,000 contacts would take ~17 days of the script
running nonstop — not practical on a laptop. Prefer "N per day" batches, or use
Resend's Broadcasts/Audiences feature for true bulk (Resend paces and sends it
server-side, nothing running on your machine).

---

## Choosing who gets emailed

In `config.ini` under `[data]`:

- `siaa_sheets` — the SIAA workbook has several sheets. `ISM List` is the full
  national master (~4,800 agencies). `Brooke Tennessee` is just the TN subset.
  List one or more, comma-separated.
- `include_iroquois` — set to `yes` to also email the Mississippi/Iroquois list.

Fewer, more relevant contacts beats emailing everyone.

---

## Files in this folder

| File | What it is |
|------|-----------|
| `send_outreach.py` | The script (sends via the Resend API). |
| `config.example.ini` | Template config — copy to `config.ini`. |
| `config.ini` | **Your** config with the API key. Git-ignored. Don't share. |
| `templates/version_a.txt` | Cold email, "direct / one ask" version. |
| `templates/version_b.txt` | Cold email, "problem-first" version. |
| `requirements.txt` | The Python libraries to install. |
| `*.xlsx` | The SIAA / Iroquois contact spreadsheets. |
| `preview/` | Dry-run output. Safe to delete anytime. Git-ignored. |
| `sent-log.csv` | Who's already been emailed (+ Resend message id). Keep this. |

---

## Troubleshooting

- **"No Resend API key set"** — fill in `[resend] api_key` in `config.ini`.
- **`from_email … is not on your verified sending domain`** — set `from_email`
  to an address `@try-risk-runway.com` (or, deliberately, set
  `allow_any_domain = yes`).
- **`HTTP 403` / `domain is not verified`** from Resend — finish domain
  verification in Resend (DNS records not yet live/propagated).
- **`HTTP 422`** — usually a malformed `from` or `to` address.
- **"Missing dependency 'pandas'"** — run the pip install step above.
