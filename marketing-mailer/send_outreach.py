#!/usr/bin/env python3
"""
Risk-Runway cold-outreach mail merge  —  Resend edition.

WHAT THIS DOES
  Reads the SIAA / Iroquois agency spreadsheets, builds one personalized
  email per contact (first name + company), and EITHER:
    * writes each email to the preview/ folder for you to read  (default), or
    * actually sends them, slowly and throttled, through the Resend API.

WHY RESEND (not raw SMTP / Gmail)
  A 5,000-contact cold list needs a real sending service with a warmed,
  reputation-managed IP pool and proper SPF/DKIM/DMARC on your own domain.
  This tool sends via https://resend.com using their API. The mail leaves
  Resend's servers, not this machine and not any AWS box. Your only job here
  is pacing (how fast) and content (what).

SAFE BY DESIGN
  * Default mode is a DRY RUN. It sends nothing. It just writes .txt previews.
  * Real sending requires TWO things on purpose:
        1. the  --send  flag
        2. typing  SEND  when it asks you to confirm
  * It throttles: a random pause between every email, and a hard cap per run.
  * It keeps sent-log.csv so re-running never emails the same person twice.
  * Every email includes the CAN-SPAM required footer (physical address +
    unsubscribe line) pulled from your config.

BEFORE YOU CAN SEND (one time)
  1. Buy/own the sending domain (you have: try-risk-runway.com).
  2. Add it in Resend -> Domains, and add the DNS records Resend gives you
     (SPF TXT, DKIM CNAMEs, DMARC TXT) to that domain's Route 53 hosted zone.
     Wait until Resend shows the domain as "Verified".
  3. Create a Resend API key (Resend -> API Keys) and put it in config.ini.
  4. Set from_email to something @try-risk-runway.com.

TYPICAL USE
    python send_outreach.py                 # dry run -> preview/ folder
    python send_outreach.py --limit 5       # only build the first 5
    python send_outreach.py --send          # actually send (asks to confirm)

Read README.md first.
"""

import argparse
import configparser
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    import pandas as pd
except ImportError:
    sys.exit(
        "Missing dependency 'pandas'.\n"
        "Install the requirements first:\n"
        "    python -m pip install -r requirements.txt"
    )

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.ini")
TEMPLATE_DIR = os.path.join(HERE, "templates")
PREVIEW_DIR = os.path.join(HERE, "preview")
SENT_LOG = os.path.join(HERE, "sent-log.csv")

RESEND_ENDPOINT = "https://api.resend.com/emails"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def is_valid_email(value):
    return isinstance(value, str) and bool(EMAIL_RE.match(value.strip()))


def first_name_from(person, company):
    """Best-effort first name. Falls back to 'there' so we never send 'Hi ,'."""
    if isinstance(person, str) and person.strip():
        token = person.strip().split()[0]
        if token.isupper():
            token = token.capitalize()
        if token.isalpha() and len(token) > 1:
            return token
    return "there"


def clean_company(name):
    if not isinstance(name, str):
        return "your agency"
    name = name.strip()
    if not name:
        return "your agency"
    name = re.split(r"\s+d/?b/?a\s+", name, flags=re.IGNORECASE)[0].strip()
    trimmed = re.sub(
        r"[,]?\s+(LLC|L\.L\.C\.|Inc\.?|Incorporated|Corp\.?|Co\.?|Ltd\.?|"
        r"LTD|LP|LLP|PLLC)\.?$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    return trimmed if trimmed else name


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def safe_filename(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:80]


# ---------------------------------------------------------------------------
# Config + templates
# ---------------------------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            "No config.ini found.\n"
            "Copy config.example.ini to config.ini and fill in your details:\n"
            f"    {CONFIG_PATH}"
        )
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg


def load_template(name):
    path = os.path.join(TEMPLATE_DIR, name + ".txt")
    if not os.path.exists(path):
        sys.exit(f"Template not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    lines = raw.splitlines()
    if not lines or not lines[0].lower().startswith("subject:"):
        sys.exit(f"Template {name}.txt must start with a 'Subject:' line.")
    subject = lines[0].split(":", 1)[1].strip()
    body = "\n".join(lines[1:]).lstrip("\n")
    return subject, body


def build_footer(cfg):
    addr = cfg.get("sender", "physical_address", fallback="").strip()
    unsub = cfg.get("sender", "unsubscribe_email", fallback="").strip()
    parts = ["\n\n---"]
    if addr:
        parts.append(addr)
    if unsub:
        parts.append(
            f"This is a one-time note, not a mailing list. If you'd rather not "
            f"hear from us, just reply \"unsubscribe\" or email {unsub} and "
            "we'll make sure we don't reach out again."
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Data loading  (unchanged parsing logic — handles the messy sheet layouts)
# ---------------------------------------------------------------------------
def _find_col(df, *candidates):
    lookup = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        if cand in lookup:
            return lookup[cand]
    for cand in candidates:
        for key, orig in lookup.items():
            if key.startswith(cand):
                return orig
    return None


def load_siaa(path, sheets):
    contacts = []
    if not os.path.exists(path):
        print(f"  ! SIAA file not found, skipping: {path}")
        return contacts

    available = pd.ExcelFile(path).sheet_names
    for sheet in sheets:
        sheet = sheet.strip()
        if not sheet:
            continue
        if sheet not in available:
            print(f"  ! sheet '{sheet}' not in {os.path.basename(path)}; "
                  f"available: {available}")
            continue

        df = pd.read_excel(path, sheet_name=sheet)
        email_col = _find_col(df, "email")
        name_col = _find_col(df, "primary contact", "contact")
        company_col = _find_col(df, "account name", "account", "agency")

        # Shifted layout (e.g. 'Brooke Tennessee'): no usable headers ->
        # fall back to positional columns based on the known 10-col schema.
        if email_col is None and df.shape[1] >= 10:
            company_col = df.columns[0]
            name_col = df.columns[6]
            email_col = df.columns[9]

        if email_col is None:
            print(f"  ! no email column in sheet '{sheet}', skipping")
            continue

        n = 0
        for _, row in df.iterrows():
            email = row.get(email_col)
            if not is_valid_email(email):
                continue
            person = row.get(name_col) if name_col is not None else None
            company = row.get(company_col) if company_col is not None else None
            contacts.append(
                {
                    "email": email.strip(),
                    "first_name": first_name_from(person, company),
                    "company": clean_company(company),
                    "source": f"SIAA/{sheet}",
                }
            )
            n += 1
        print(f"  + SIAA '{sheet}': {n} contacts")
    return contacts


def load_iroquois(path):
    contacts = []
    if not os.path.exists(path):
        print(f"  ! Iroquois file not found, skipping: {path}")
        return contacts

    df = pd.read_excel(path, sheet_name=0, header=1)
    email_col = _find_col(df, "email")
    name_col = _find_col(df, "principal", "primary contact", "contact")
    company_col = _find_col(df, "agency", "account name", "company")
    if email_col is None:
        print("  ! no email column found in Iroquois file, skipping")
        return contacts

    n = 0
    for _, row in df.iterrows():
        email = row.get(email_col)
        if not is_valid_email(email):
            continue
        person = row.get(name_col) if name_col is not None else None
        company = row.get(company_col) if company_col is not None else None
        contacts.append(
            {
                "email": email.strip(),
                "first_name": first_name_from(person, company),
                "company": clean_company(company),
                "source": "Iroquois",
            }
        )
        n += 1
    print(f"  + Iroquois: {n} contacts")
    return contacts


def dedupe(contacts):
    seen = set()
    out = []
    for c in contacts:
        key = c["email"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def load_already_sent():
    sent = set()
    if os.path.exists(SENT_LOG):
        with open(SENT_LOG, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                addr = (row.get("email") or "").strip().lower()
                if addr:
                    sent.add(addr)
    return sent


def append_sent_log(email, company, template):
    new_file = not os.path.exists(SENT_LOG)
    with open(SENT_LOG, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["timestamp_utc", "email", "company", "template", "resend_id"])
        # backward-compatible: callers pass a 5-tuple via wrapper below
        w.writerow([utc_now_iso(), email, company, template, ""])


def append_sent_log_row(email, company, template, resend_id):
    new_file = not os.path.exists(SENT_LOG)
    with open(SENT_LOG, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(["timestamp_utc", "email", "company", "template", "resend_id"])
        w.writerow([utc_now_iso(), email, company, template, resend_id or ""])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(subject_tpl, body_tpl, footer, cfg, contact):
    fields = {
        "first_name": contact["first_name"],
        "company": contact["company"],
        "from_name": cfg.get("sender", "from_name", fallback=""),
        "from_email": cfg.get("sender", "from_email", fallback=""),
    }
    try:
        subject = subject_tpl.format(**fields)
        body = body_tpl.format(**fields) + footer
    except KeyError as e:
        sys.exit(f"Template uses unknown placeholder {e}. "
                 "Allowed: {first_name} {company} {from_name} {from_email}")
    return subject, body


# ---------------------------------------------------------------------------
# Sending via Resend API  (only reached with --send + typed confirmation)
# ---------------------------------------------------------------------------
class ResendError(Exception):
    pass


def resend_send(api_key, from_header, to_email, subject, text_body, reply_to=None):
    """
    Send one email via the Resend API. Returns the Resend message id on success.
    Raises ResendError on any non-2xx response.

    Docs: https://resend.com/docs/api-reference/emails/send-email
    """
    payload = {
        "from": from_header,          # e.g. 'Brooke Bouy <brooke@try-risk-runway.com>'
        "to": [to_email],
        "subject": subject,
        "text": text_body,
    }
    if reply_to:
        payload["reply_to"] = reply_to

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        RESEND_ENDPOINT,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "risk-runway-mailer/1.0 (+https://risk-runway.com)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return parsed.get("id", "")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            pass
        raise ResendError(f"HTTP {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise ResendError(f"network error: {e.reason}")


def from_header(cfg):
    name = cfg.get("sender", "from_name", fallback="").strip()
    addr = cfg.get("sender", "from_email", fallback="").strip()
    return f"{name} <{addr}>" if name else addr


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Risk-Runway throttled cold-outreach mail merge (Resend)."
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send emails. Without this flag it's a dry run (previews only).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N contacts (after dedupe / sent-log filtering).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="(Advanced) skip the typed confirmation. Still respects --send.",
    )
    args = parser.parse_args()

    cfg = load_config()
    template_name = cfg.get("send", "template", fallback="version_a")
    subject_tpl, body_tpl = load_template(template_name)
    footer = build_footer(cfg)

    print(f"Template: {template_name}")
    print("Loading contacts...")

    contacts = []
    contacts += load_siaa(
        os.path.join(HERE, cfg.get("data", "siaa_file")),
        cfg.get("data", "siaa_sheets", fallback="").split(","),
    )
    if cfg.getboolean("data", "include_iroquois", fallback=False):
        contacts += load_iroquois(
            os.path.join(HERE, cfg.get("data", "iroquois_file"))
        )

    before = len(contacts)
    contacts = dedupe(contacts)
    print(f"Total {before} rows -> {len(contacts)} unique emails")

    already = load_already_sent()
    if already:
        contacts = [c for c in contacts if c["email"].lower() not in already]
        print(f"Skipping {len(already)} already in sent-log -> {len(contacts)} remaining")

    if args.limit is not None:
        contacts = contacts[: args.limit]
        print(f"--limit applied -> {len(contacts)} this run")

    if not contacts:
        print("Nothing to do. Exiting.")
        return

    # ---- DRY RUN (default) --------------------------------------------------
    if not args.send:
        # Clean out any old per-contact preview files from earlier versions.
        if os.path.isdir(PREVIEW_DIR):
            for f in os.listdir(PREVIEW_DIR):
                if f.endswith(".txt"):
                    os.remove(os.path.join(PREVIEW_DIR, f))
        os.makedirs(PREVIEW_DIR, exist_ok=True)
        combined_path = os.path.join(PREVIEW_DIR, "ALL_PREVIEWS.txt")
        sender = from_header(cfg)
        with open(combined_path, "w", encoding="utf-8") as out:
            out.write(f"DRY RUN — {len(contacts)} emails — {utc_now_iso()}\n")
            out.write(f"From: {sender}\n")
            out.write(f"Template: {template_name}\n")
            out.write("NOTHING WAS SENT. This is a preview only.\n")
            out.write("=" * 72 + "\n\n")
            for i, contact in enumerate(contacts, 1):
                subject, body = render(subject_tpl, body_tpl, footer, cfg, contact)
                out.write(f"### {i} of {len(contacts)}\n")
                out.write(f"To: {contact['email']}\n")
                out.write(f"Source: {contact['source']}\n")
                out.write(f"Subject: {subject}\n")
                out.write("-" * 72 + "\n")
                out.write(body + "\n")
                out.write("\n" + "=" * 72 + "\n\n")
        print()
        print(f"DRY RUN complete. Wrote {len(contacts)} previews into ONE file:")
        print(f"   {combined_path}")
        print("No emails were sent. Open it and scan through.")
        print("When you're happy, run again with  --send  to actually send.")
        return

    # ---- REAL SEND (requires --send AND confirmation) -----------------------
    api_key = cfg.get("resend", "api_key", fallback="").strip()
    if not api_key or api_key.startswith("re_your"):
        sys.exit(
            "No Resend API key set. Put your key in config.ini under [resend].\n"
            "Get one at https://resend.com -> API Keys."
        )
    sender = from_header(cfg)
    if "try-risk-runway.com" not in sender and not cfg.getboolean(
        "resend", "allow_any_domain", fallback=False
    ):
        sys.exit(
            f"from_email ({sender}) is not on your verified sending domain "
            "(try-risk-runway.com). Fix from_email in config.ini, or set "
            "allow_any_domain = yes under [resend] if you know what you're doing."
        )
    reply_to = cfg.get("sender", "reply_to", fallback="").strip() or None

    cap = cfg.getint("send", "max_per_run", fallback=40)
    to_send = contacts[:cap]
    min_d = cfg.getint("send", "min_delay_seconds", fallback=45)
    max_d = cfg.getint("send", "max_delay_seconds", fallback=90)
    if max_d < min_d:
        max_d = min_d

    print()
    print("=" * 64)
    print("  LIVE SEND  (via Resend)")
    print(f"  From:     {sender}")
    print(f"  Contacts: {len(to_send)} (per-run cap {cap})")
    print(f"  Pause:    random {min_d}-{max_d}s between each email")
    est_min = (len(to_send) * (min_d + max_d) / 2) / 60
    print(f"  ETA:      roughly {est_min:.0f} minutes")
    print("=" * 64)

    if not args.yes:
        answer = input('Type  SEND  (all caps) to send for real, anything else to abort: ')
        if answer.strip() != "SEND":
            print("Aborted. Nothing was sent.")
            return

    sent = 0
    for i, contact in enumerate(to_send, 1):
        subject, body = render(subject_tpl, body_tpl, footer, cfg, contact)
        try:
            msg_id = resend_send(api_key, sender, contact["email"], subject, body, reply_to)
            append_sent_log_row(contact["email"], contact["company"], template_name, msg_id)
            sent += 1
            print(f"  [{i}/{len(to_send)}] sent -> {contact['email']}  ({msg_id or 'no-id'})")
        except ResendError as e:
            print(f"  [{i}/{len(to_send)}] FAILED -> {contact['email']}: {e}")
        if i < len(to_send):
            time.sleep(random.randint(min_d, max_d))

    print()
    print(f"Done. Sent {sent}/{len(to_send)}. Logged to sent-log.csv.")
    if len(contacts) > cap:
        print(f"{len(contacts) - cap} contacts remain (hit the per-run cap). "
              "Run again later to continue where you left off.")


if __name__ == "__main__":
    main()
