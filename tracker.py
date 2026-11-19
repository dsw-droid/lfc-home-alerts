#!/usr/bin/env python3
"""
Liverpool FC men's HOME ticket-info tracker.

Every run:
  1. Opens the LFC tickets page, clicks "Load More" until every fixture is shown,
     and keeps only men's games at Anfield.
  2. Opens each fixture's page and pulls out every dated entry
     (members sale registration, ballot registration, sale dates...).
  3. Compares with state.json and sends a phone push (via ntfy) for anything
     NEW or CHANGED.

Usage:
  python tracker.py                 # normal run
  python tracker.py --dry-run       # print alerts, don't send, don't save state
  python tracker.py --test-notify   # send a test push and exit

Environment:
  NTFY_TOPIC   (required) your private ntfy topic name
  NTFY_SERVER  (optional) default https://ntfy.sh
  NTFY_TOKEN   (optional) access token if your topic is protected
  STATE_FILE   (optional) default state.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

LIST_URL = "https://www.liverpoolfc.com/tickets?category=mens"
MATCH_LINK = 'a[href*="/tickets/match/"]'
HOME_VENUE = "anfield"
MAX_FAILURES_BEFORE_ALERT = 4  # consecutive broken runs before you get a "tracker is broken" push

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# Parsing (pure functions - covered by test_tracker.py)
# --------------------------------------------------------------------------- #

_DAYS = "Mon|Tue|Wed|Thu|Fri|Sat|Sun"
# e.g. "Tue 17 Nov 2026, 11:00am"
_DATE = (
    rf"(?:{_DAYS})[a-z]*\.?\s+\d{{1,2}}\s+[A-Z][a-z]{{2,8}}\s+\d{{4}},?"
    rf"\s+\d{{1,2}}[:.]\d{{2}}\s*(?i:[ap]m)"
)
# A line that is just "[STATUS IN CAPS]<date>", e.g. "REGISTRATION ENDEDTue 17 Nov 2026, 11:00am"
_LINE_RE = re.compile(rf"^(?P<status>[A-Z][A-Z0-9 \-/&']*?)?\s*(?P<date>{_DATE})$")
_SKIP_TITLES = {"read more", "read less", "buy now", "register now"}
_KICKOFF_RE = re.compile(r"\b\d{2}:\d{2}\s*/\s*[A-Za-z]{3}\s+\d{1,2}\s+[A-Za-z]{3,9}\b")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def _is_status(line: str) -> bool:
    return line.isupper() and len(line) < 40


def parse_entries(text: str) -> dict[str, dict]:
    """Return {title: {"date": ..., "status": ...}} for every dated block on a match page."""
    lines = [l for l in (clean(x) for x in text.splitlines()) if l]
    entries: dict[str, dict] = {}
    for i, line in enumerate(lines):
        m = _LINE_RE.match(line)
        if not m:
            continue
        status = (m.group("status") or "").strip()
        if not status and i > 0 and _is_status(lines[i - 1]):
            status = lines[i - 1]
        title = next((l for l in lines[i + 1 : i + 4] if l.lower() not in _SKIP_TITLES), None)
        if not title:
            continue
        base, n = title, 2
        while title in entries:  # two blocks with the same heading
            title, n = f"{base} #{n}", n + 1
        entries[title] = {"date": clean(m.group("date")), "status": status}
    return entries


def diff_entries(old: dict[str, str], new: dict[str, dict]) -> list[tuple[str, str, str, str | None]]:
    """Return [(kind, title, new_date, old_date)] where kind is 'new' or 'changed'."""
    out = []
    for title, e in new.items():
        prev = old.get(title)
        if prev is None:
            out.append(("new", title, e["date"], None))
        elif prev != e["date"]:
            out.append(("changed", title, e["date"], prev))
    return out


def is_mens_home(href: str, card_text: str) -> bool:
    slug = href.rstrip("/").rsplit("/", 1)[-1].lower()
    text = card_text.lower()
    if "women" in slug or "women" in text:
        return False
    return HOME_VENUE in text


def slug_of(url: str) -> str:
    return url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]


def pretty_slug(slug: str) -> str:
    return re.sub(r"\s+", " ", slug.replace("-", " ")).title()


# --------------------------------------------------------------------------- #
# Browser scraping
# --------------------------------------------------------------------------- #

def _dismiss_cookie_banner(page) -> None:
    try:
        btn = page.get_by_role("button", name=re.compile(r"accept( all)?( cookies)?", re.I)).first
        if btn.is_visible(timeout=2000):
            btn.click()
    except Exception:
        pass


def discover_home_matches(page) -> list[str]:
    page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector(MATCH_LINK, timeout=45_000)
    _dismiss_cookie_banner(page)

    load_more = page.locator("button, a").filter(has_text=re.compile(r"^\s*load more\s*$", re.I))
    for _ in range(40):
        before = page.locator(MATCH_LINK).count()
        try:
            if load_more.count() == 0 or not load_more.first.is_visible():
                break
            load_more.first.click()
        except Exception:
            break
        page.wait_for_timeout(1500)
        if page.locator(MATCH_LINK).count() <= before:
            break

    cards = page.eval_on_selector_all(
        MATCH_LINK, "els => els.map(e => ({href: e.href, text: e.innerText}))"
    )
    seen, urls = set(), []
    for c in cards:
        href = c["href"].split("?")[0]
        if href in seen:
            continue
        seen.add(href)
        if is_mens_home(href, clean(c["text"])):
            urls.append(href)
    return urls


def fetch_match(page, url: str) -> dict:
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    if resp is not None and resp.status >= 400:
        raise RuntimeError(f"HTTP {resp.status}")
    try:
        page.wait_for_selector("h2", timeout=20_000)
    except Exception:
        raise RuntimeError("page did not render (no headings)")
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    body = page.inner_text("body")
    h2 = [clean(t) for t in page.locator("h2").all_inner_texts()]
    name = None
    if len(h2) >= 2 and h2[0].lower().startswith("liverpool") and len(h2[0]) + len(h2[1]) < 60:
        name = f"{h2[0]} v {h2[1]}"
    kickoff = _KICKOFF_RE.search(body)
    return {
        "name": name or pretty_slug(slug_of(url)),
        "kickoff": clean(kickoff.group(0)) if kickoff else "",
        "entries": parse_entries(body),
    }


# --------------------------------------------------------------------------- #
# Notifications (ntfy)
# --------------------------------------------------------------------------- #

def notify(title: str, message: str, click: str | None = None, priority: int = 4,
           tags: list[str] | None = None) -> None:
    if not NTFY_TOPIC:
        raise SystemExit("NTFY_TOPIC is not set")
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
    headers = {"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}
    r = requests.post(NTFY_SERVER, json=payload, headers=headers, timeout=20)
    r.raise_for_status()


def build_alert(name: str, kickoff: str, changes) -> tuple[str, str]:
    title = f"🎟️ {name} – ticket info"
    if kickoff:
        title += f" ({kickoff.split('/')[-1].strip().title()})"
    lines = []
    for kind, t, new_date, old_date in changes:
        if kind == "new":
            lines.append(f"• {t}: {new_date}")
        else:
            lines.append(f"• {t}: {old_date} → {new_date} (CHANGED)")
    return title, "\n".join(lines)


# --------------------------------------------------------------------------- #
# State + main
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"initialised": False, "consecutive_failures": 0, "last_total_entries": 0, "matches": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def record_failure(state: dict, reason: str, dry_run: bool) -> None:
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    print(f"FAILURE ({state['consecutive_failures']}): {reason}", file=sys.stderr)
    if not dry_run:
        if state["consecutive_failures"] == MAX_FAILURES_BEFORE_ALERT:
            notify("⚠️ LFC tracker may be broken",
                   f"{state['consecutive_failures']} runs in a row failed.\n{reason}\n"
                   "The site layout may have changed or is blocking the checker.",
                   priority=3, tags=["warning"])
        save_state(state)
    sys.exit(1)


def run(dry_run: bool) -> None:
    from playwright.sync_api import sync_playwright  # imported lazily so tests don't need it

    state = load_state()
    first_run = not state.get("initialised")
    matches = state.setdefault("matches", {})
    pending: list[tuple[str, str, list, str]] = []
    total_entries = failed_pages = 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-GB", timezone_id="Europe/London")
        page = ctx.new_page()

        try:
            urls = discover_home_matches(page)
        except Exception as e:
            browser.close()
            record_failure(state, f"could not load fixture list: {e}", dry_run)
        if not urls:
            browser.close()
            record_failure(state, "no men's home fixtures found on the list page", dry_run)
        print(f"Found {len(urls)} men's home fixtures")

        for url in urls:
            slug = slug_of(url)
            try:
                info = fetch_match(page, url)
            except Exception as e:
                failed_pages += 1
                print(f"  ! {slug}: {e}", file=sys.stderr)
                continue
            total_entries += len(info["entries"])

            rec = matches.setdefault(slug, {"entries": {}})
            changes = diff_entries(rec["entries"], info["entries"])
            print(f"  {info['name']}: {len(info['entries'])} entries, {len(changes)} new/changed")
            if changes and not first_run:
                pending.append((info["name"], info["kickoff"], changes, url))

            rec.update(name=info["name"], kickoff=info["kickoff"], url=url)
            rec["entries"].update({t: e["date"] for t, e in info["entries"].items()})  # cumulative
            time.sleep(1.5)
        browser.close()

    if failed_pages > len(urls) / 2:
        record_failure(state, f"{failed_pages}/{len(urls)} fixture pages failed to load", dry_run)
    if state.get("last_total_entries", 0) > 0 and total_entries == 0:
        record_failure(state, "parsed 0 sale/ballot entries across every fixture (layout change?)", dry_run)

    for name, kickoff, changes, url in pending:
        title, msg = build_alert(name, kickoff, changes)
        print(f"\nALERT: {title}\n{msg}")
        if not dry_run:
            notify(title, msg, click=url, tags=["soccer", "ticket"])

    if first_run and not dry_run:
        with_info = sum(1 for m in matches.values() if m["entries"])
        notify("✅ LFC ticket tracker is live",
               f"Watching {len(urls)} men's home games. {with_info} already have dates "
               "(not alerted). You'll be pinged when new info appears or dates change.",
               priority=3, tags=["white_check_mark"])

    state.update(initialised=True, consecutive_failures=0, last_total_entries=total_entries)
    if not dry_run:
        save_state(state)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print alerts; don't send or save state")
    ap.add_argument("--test-notify", action="store_true", help="send a test push and exit")
    args = ap.parse_args()

    if args.test_notify:
        notify("🎟️ LFC tracker test", "If you can read this, phone alerts work.", tags=["soccer"])
        print("Test notification sent.")
        return
    run(args.dry_run)


if __name__ == "__main__":
    main()
