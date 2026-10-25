#!/usr/bin/env python3
"""
iPhone Duo stock watcher for Apple UK stores.

What it does
  * Polls Apple's own store-pickup availability endpoint (the same one the
    "Check availability" box on apple.com/uk uses) for your chosen model(s),
    across a spread of UK postcodes so it covers stores nationwide.
  * Sends a phone push notification (via ntfy.sh, free, no account) the moment a
    store flips from "unavailable" to "available", with a tap-through link.
  * Alerts on changes only, so you are not spammed every poll.

Setup (5 minutes)
  1. Python 3.8+ is all you need. No packages to install.
  2. Install the free "ntfy" app on your phone, subscribe to a long, random topic
     name, and put the same name in NTFY_TOPIC below.
  3. Find your part number(s): run   python duo_stock_watch.py --discover
     (or open the Duo buy page, pick colour + storage, and look for a code like
     MXXXXQN/A in the page source). Paste them into PARTS below.
  4. Run:  python duo_stock_watch.py            (loops forever)
           python duo_stock_watch.py --once     (single check, for testing / cron)
           python duo_stock_watch.py --test     (sends a test notification)

Leave it running on something that stays on (a Raspberry Pi, a spare PC, a cheap
VPS). A laptop that sleeps will miss restocks.

Be polite: keep POLL_SECONDS at 60 or more. If Apple returns 403/429 the script
backs off automatically. It only reads availability; it never logs in, adds to
bag, or buys anything for you.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ----------------------------- CONFIG ---------------------------------------

# Part numbers to watch -> friendly label. Fill these in (see --discover).
PARTS = {
    # "MXXXXQN/A": "Duo 256GB Night Sky",
    # "MXXXYQN/A": "Duo 256GB Star White",
}
# Cloud mode: set a PARTS environment variable instead, formatted
#   PART1=Label one;PART2=Label two
if os.environ.get("PARTS"):
    for _item in os.environ["PARTS"].split(";"):
        if "=" in _item:
            _p, _l = _item.split("=", 1)
            PARTS[_p.strip()] = _l.strip()

# Your ntfy topic (make it long and unguessable; anyone who knows it can read it).
# In cloud mode it is read from the NTFY_TOPIC environment variable.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "change-me-to-something-long-and-random"

# Postcodes spread across the UK. Each lookup returns the stores nearest to that
# postcode, so more spread = better national coverage. Add/remove freely.
POSTCODES = [
    "WS10 0AA",   # Wednesbury / West Midlands
    "B1 1AA",     # Birmingham
    "SW1A 1AA",   # London (central)
    "E20 1EJ",    # London (east)
    "M1 1AE",     # Manchester
    "L1 8JQ",     # Liverpool
    "LS1 1UR",    # Leeds
    "S1 2GU",     # Sheffield
    "NE1 4ST",    # Newcastle
    "G1 1XQ",     # Glasgow
    "EH1 1YZ",    # Edinburgh
    "CF10 1EP",   # Cardiff
    "BS1 3XD",    # Bristol
    "SO14 7FP",   # Southampton
    "BN1 1AA",    # Brighton
    "NG1 2GE",    # Nottingham
    "CB2 1TN",    # Cambridge
    "NR1 3QA",    # Norwich
    "BT1 5GS",    # Belfast
]

COUNTRY = "uk"          # apple.com/<country>
POLL_SECONDS = 90       # minimum sensible value is ~60
ALSO_CHECK_ONLINE = True  # also report if Apple's online store shows it buyable
STATE_FILE = Path(__file__).with_name("duo_stock_state.json")

# ----------------------------------------------------------------------------

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BASE = f"https://www.apple.com/{COUNTRY}"


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def product_link(part):
    return f"{BASE}/shop/product/{part}"


def fulfilment_url(part, postcode):
    q = {
        "pl": "true",
        "mts.0": "regular",
        "mts.1": "compact",
        "parts.0": part,
        "searchNearby": "true",
        "location": postcode,
    }
    return f"{BASE}/shop/fulfillment-messages?" + urllib.parse.urlencode(q)


def parse_response(payload, part):
    """Return (stores, online_buyable). stores = {store_key: (name, available_bool)}"""
    body = payload.get("body", {}).get("content", {})
    stores = {}
    for s in body.get("pickupMessage", {}).get("stores", []) or []:
        avail = (s.get("partsAvailability", {}) or {}).get(part, {}) or {}
        name = f'{s.get("storeName", "?")} ({s.get("city", "")})'.replace(" ()", "")
        key = s.get("storeNumber") or name
        stores[key] = (name, str(avail.get("pickupDisplay", "")).lower() == "available")
    online = False
    dm = (body.get("deliveryMessage", {}) or {}).get(part, {}) or {}
    reg = dm.get("regular", {}) or {}
    if reg.get("isBuyable") is True:
        online = True
    return stores, online


def notify(title, message, click=None, priority="urgent"):
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": "iphone,rotating_light"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # never crash the watcher over a failed push
        print(f"  ! notification failed: {e}", file=sys.stderr)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=1))
    except Exception as e:
        print(f"  ! could not save state: {e}", file=sys.stderr)


def check_once(state):
    """One full sweep. Returns number of alerts sent, or None if Apple blocked us."""
    alerts = 0
    for part, label in PARTS.items():
        seen = {}          # store_key -> (name, available) merged across postcodes
        online_now = False
        for pc in POSTCODES:
            try:
                payload = json.loads(http_get(fulfilment_url(part, pc)))
            except urllib.error.HTTPError as e:
                if e.code in (403, 429, 503):
                    print(f"  ! Apple returned {e.code}; backing off", file=sys.stderr)
                    return None
                print(f"  ! {pc}: HTTP {e.code}", file=sys.stderr)
                continue
            except Exception as e:
                print(f"  ! {pc}: {e}", file=sys.stderr)
                continue
            stores, online = parse_response(payload, part)
            online_now = online_now or online
            for k, v in stores.items():
                prev = seen.get(k)
                seen[k] = (v[0], v[1] or (prev[1] if prev else False))
            time.sleep(1.0)  # be gentle between lookups

        in_stock = {k: v[0] for k, v in seen.items() if v[1]}
        print(f"[{time.strftime('%H:%M:%S')}] {label}: {len(seen)} stores seen, "
              f"{len(in_stock)} with stock, online={'yes' if online_now else 'no'}")

        prev_stores = set(state.get(part, {}).get("stores", []))
        for k, name in in_stock.items():
            if k not in prev_stores:
                notify(f"{label} in stock: {name}",
                       f"Apple shows {label} available for pickup at {name}. Tap to open and reserve.",
                       click=product_link(part))
                alerts += 1
        if ALSO_CHECK_ONLINE and online_now and not state.get(part, {}).get("online"):
            notify(f"{label} buyable online", f"Apple's online store shows {label} as buyable.",
                   click=product_link(part))
            alerts += 1

        state[part] = {"stores": list(in_stock.keys()), "online": online_now}
        save_state(state)
    return alerts


def discover():
    url = f"{BASE}/shop/buy-iphone/iphone-duo"
    print(f"Fetching {url} ...")
    html = http_get(url)
    found = sorted(set(re.findall(r"\b[A-Z0-9]{5,6}[A-Z]{1,2}/A\b", html)))
    if not found:
        print("No part numbers found automatically. Open the page in a browser, choose a "
              "colour and storage, then search the page source for '/A'.")
        return
    print("Candidate part numbers (check each against the page; not all will be Duos):")
    for p in found:
        print("  ", p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single sweep then exit")
    ap.add_argument("--test", action="store_true", help="send a test notification")
    ap.add_argument("--discover", action="store_true", help="try to find part numbers")
    args = ap.parse_args()

    if args.discover:
        return discover()
    if args.test:
        notify("Duo watcher test", "If you can read this, notifications work.", click=f"{BASE}/shop/buy-iphone/iphone-duo")
        print("Test notification sent.")
        return
    if not PARTS:
        sys.exit("Add at least one part number to PARTS first (run --discover for help).")
    if NTFY_TOPIC.startswith("change-me"):
        sys.exit("Set NTFY_TOPIC to your own long random topic name first.")

    state = load_state()
    delay = POLL_SECONDS
    while True:
        result = check_once(state)
        if args.once:
            return
        if result is None:
            delay = min(delay * 2, 1800)
        else:
            delay = POLL_SECONDS
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
