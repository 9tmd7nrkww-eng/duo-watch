#!/usr/bin/env python3
"""
iPhone Duo stock watcher for Apple UK (online delivery estimates).

How it works
  Apple's per-store pickup lookup is not available to us, so this watches the
  same delivery estimate you see on the Duo buy page ("Ships 22 Jan"). When
  stock lands, that estimate suddenly gets much sooner. You get a phone push
  (via ntfy.sh) when, for any watched model:
    * the estimate becomes SOON_DAYS days away or less (or says today/tomorrow), or
    * the estimate improves by IMPROVE_DAYS days or more since the last check.
  Tapping the notification opens the Duo buy page so you can order.

Cloud mode (GitHub Actions) reads settings from environment variables:
  NTFY_TOPIC   your ntfy topic
  PARTS        PART1=Label one;PART2=Label two

Commands
  python duo_stock_watch.py --discover   list Duo part numbers + current estimates
  python duo_stock_watch.py --test       send a test notification
  python duo_stock_watch.py --once       one check (what GitHub runs every 5 min)
  python duo_stock_watch.py              loop forever (for a Pi / PC / server)

It only reads public delivery estimates. It never logs in, adds to bag or buys.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

# ----------------------------- CONFIG ---------------------------------------

PARTS = {
    # "MK2E4QN/A": "Duo 256GB Night Sky",
}
if os.environ.get("PARTS"):
    for _item in os.environ["PARTS"].split(";"):
        if "=" in _item:
            _p, _l = _item.split("=", 1)
            PARTS[_p.strip()] = _l.strip()

NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or "change-me-to-something-long-and-random"

COUNTRY = "uk"
POLL_SECONDS = 120      # loop mode only; keep at 60 or more
SOON_DAYS = 7           # alert if the estimate is this many days away or less
IMPROVE_DAYS = 7        # ...or if it improves by at least this many days
STATE_FILE = Path(os.environ.get("STATE_FILE") or Path(__file__).with_name("duo_stock_state.json"))

# ----------------------------------------------------------------------------

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BASE = f"https://www.apple.com/{COUNTRY}"
BUY_PAGE = f"{BASE}/shop/buy-iphone/iphone-duo"
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
NEG = re.compile(r"unavailable|not available|out of stock|sold out|currently", re.I)
ALL_FAILED = False


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def delivery_url(parts):
    q = "&".join(f"parts.{i}={p}" for i, p in enumerate(parts))
    return f"{BASE}/shop/delivery-message?{q}&little=true"


def parse_days(text, today=None):
    """Days until the earliest date in the message, or None if there isn't one."""
    today = today or date.today()
    if re.search(r"\btoday\b", text, re.I):
        return 0
    if re.search(r"\btomorrow\b", text, re.I):
        return 1
    m = re.search(r"\b(\d{1,2})(?:\s*[-–]\s*\d{1,2})?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", text, re.I)
    if not m:
        return None
    try:
        d = date(today.year, MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None
    if (d - today).days < -30:
        d = date(d.year + 1, d.month, d.day)
    return (d - today).days


def messages(regular):
    out = []
    for m in regular.get("deliveryOptionMessages") or []:
        if isinstance(m, dict) and m.get("displayName"):
            out.append(str(m["displayName"]))
    if not out:
        for o in regular.get("deliveryOptions") or []:
            if isinstance(o, dict):
                for k in ("displayName", "date"):
                    if o.get(k):
                        out.append(str(o[k]))
    return out


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


def mark_broken(state, why):
    global ALL_FAILED
    ALL_FAILED = True
    print(f"  ! {why}")
    if not state.get("_broken"):
        notify("Duo watcher is not working", f"{why} Check the GitHub run log.", priority="default")
    state["_broken"] = True
    save_state(state)


def check_once(state):
    """One check of every watched part. Returns alert count, or None if Apple blocked us."""
    parts = list(PARTS)
    try:
        payload = json.loads(http_get(delivery_url(parts)))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429, 503):
            print(f"  ! Apple returned {e.code}; backing off", file=sys.stderr)
            return None
        mark_broken(state, f"Apple returned HTTP {e.code}.")
        return 0
    except Exception as e:
        mark_broken(state, f"Request failed: {e}")
        return 0

    content = (payload.get("body", {}).get("content", {}) or {}).get("deliveryMessage", {}) or {}
    alerts = failed = 0
    for n, (part, label) in enumerate(PARTS.items()):
        regular = (content.get(part) or {}).get("regular") or {}
        if n == 0:
            print("   raw:", json.dumps(regular)[:500])
        text = " / ".join(messages(regular))
        if not text:
            failed += 1
            print(f"[{time.strftime('%H:%M:%S')}] {label}: no delivery message returned")
            continue
        days = parse_days(text)
        signal = (not NEG.search(text)) and (
            (days is not None and days <= SOON_DAYS)
            or bool(re.search(r"\b(today|tomorrow|in stock|available)\b", text, re.I))
        )
        prev = state.get(part, {}) if isinstance(state.get(part), dict) else {}
        pdays = prev.get("days")
        improved = days is not None and pdays is not None and days <= pdays - IMPROVE_DAYS
        print(f"[{time.strftime('%H:%M:%S')}] {label}: {text} "
              f"({'no date' if days is None else str(days) + ' days'})"
              f"{'  <-- STOCK SIGNAL' if signal else ''}{'  <-- IMPROVED' if improved else ''}")
        if (signal and not prev.get("signal")) or improved:
            was = f" (was: {prev['text']})" if prev.get("text") else ""
            notify(f"{label}: {text}",
                   f"Apple UK now shows \"{text}\" for {label}{was}. Tap to open the buy page and order.",
                   click=BUY_PAGE)
            alerts += 1
        state[part] = {"text": text, "days": days, "signal": signal}

    if failed == len(PARTS):
        mark_broken(state, "Apple returned no delivery message for any watched model.")
        return 0
    state["_broken"] = False
    save_state(state)
    return alerts


def discover():
    print(f"Fetching {BUY_PAGE} ...")
    html = http_get(BUY_PAGE)
    named = re.findall(r'"partNumber":"([A-Z0-9]+/A)","price":\{[^}]*\},"category":"iphone","name":"([^"]+)"', html)
    if not named:
        print("Could not read model names from the page. Raw candidates:")
        for p in sorted(set(re.findall(r"\b[A-Z0-9]{5,6}[A-Z]{1,2}/A\b", html))):
            print("  ", p)
        return
    seen, models = set(), []
    for p, name in named:
        if p not in seen:
            seen.add(p)
            models.append((p, name))
    print("\nDuo models found:")
    for p, name in models:
        print(f"  {p}={name}")
    print("\nCurrent Apple UK delivery estimates:")
    try:
        data = json.loads(http_get(delivery_url([p for p, _ in models])))
        dm = data.get("body", {}).get("content", {}).get("deliveryMessage", {}) or {}
        for p, name in models:
            txt = " / ".join(messages((dm.get(p) or {}).get("regular") or {})) or "(no message)"
            print(f"  {name}: {txt}")
    except Exception as e:
        print(f"  (could not fetch estimates: {e})")
    print("\nPARTS value for everything:")
    print(";".join(f"{p}={name}" for p, name in models))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single check then exit")
    ap.add_argument("--test", action="store_true", help="send a test notification")
    ap.add_argument("--discover", action="store_true", help="list Duo part numbers and estimates")
    args = ap.parse_args()

    if args.discover:
        return discover()
    if args.test:
        notify("Duo watcher test", "If you can read this, notifications work.", click=BUY_PAGE)
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
            if ALL_FAILED:
                sys.exit(1)  # make the GitHub run show red instead of a false green
            return
        delay = min(delay * 2, 1800) if result is None else POLL_SECONDS
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
