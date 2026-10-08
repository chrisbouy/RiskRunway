#!/usr/bin/env python3
"""
Pull all emails from Resend, find hard bounces, and add them to
suppression.csv so they are never emailed again. Safe to re-run.
"""
import configparser
import csv
import json
import os
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SUPP = os.path.join(HERE, "suppression.csv")
cfg = configparser.ConfigParser()
cfg.read(os.path.join(HERE, "config.ini"))
KEY = cfg.get("resend", "api_key").strip()

HEADERS = {"Authorization": f"Bearer {KEY}", "User-Agent": "risk-runway-mailer/1.0"}


def get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def load_suppressed():
    s = set()
    if os.path.exists(SUPP):
        with open(SUPP, newline="") as fh:
            for row in csv.DictReader(fh):
                a = (row.get("email") or "").strip().lower()
                if a:
                    s.add(a)
    return s


# Page through all emails
bounced = {}  # email -> last_event
after = None
pages = 0
seen_ids = set()
while True:
    url = "https://api.resend.com/emails?limit=100"
    if after:
        url += f"&after={after}"
    data = get(url)
    rows = data.get("data", [])
    if not rows:
        break
    for e in rows:
        eid = e.get("id")
        ev = (e.get("last_event") or "").lower()
        tos = e.get("to") or []
        if "bounce" in ev:
            for addr in tos:
                bounced[addr.strip().lower()] = ev
    pages += 1
    last_id = rows[-1].get("id")
    # stop if no progress or no more
    if not data.get("has_more") or last_id in seen_ids:
        break
    seen_ids.add(last_id)
    after = last_id
    time.sleep(0.3)

print(f"Scanned {pages} page(s). Found {len(bounced)} bounced address(es).")

already = load_suppressed()
new = [(a, ev) for a, ev in bounced.items() if a not in already]
if not new:
    print("No new bounces to add. suppression.csv already current.")
else:
    header_needed = not os.path.exists(SUPP)
    with open(SUPP, "a", newline="") as fh:
        w = csv.writer(fh)
        if header_needed:
            w.writerow(["email", "date_added", "reason"])
        today = time.strftime("%Y-%m-%d")
        for a, ev in new:
            w.writerow([a, today, f"bounced ({ev})"])
    print(f"Added {len(new)} bounced address(es) to suppression.csv:")
    for a, ev in new:
        print(f"  {a}  [{ev}]")
