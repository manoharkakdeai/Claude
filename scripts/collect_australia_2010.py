#!/usr/bin/env python3
"""Collect Australian (S&P/ASX 200) corporate events, Jan 2010 - Sep 2021.

Source: ASX "Search for recent and past announcements" (www.asx.com.au/asx/v2/statistics/announcements.do).
The per-date listing (timeframe=R, no code) returns *every* announcement released that day, with
the ASX code used at the time, so it also covers companies that have since delisted or changed code.

Index membership: S&P Dow Jones Indices index announcements are released on the ASX platform under
code ZSP (plus the affected companies). The sweep captures every ZSP release; their PDFs are parsed
for S&P/ASX 200 additions/removals (quarterly reviews and intra-quarter changes). Membership is
rebuilt backwards from an anchor constituent list (see ANCHOR_* below) applying those changes.

Stages (each resumable; raw responses cached under the scratchpad dir):
  sweep     fetch the per-date listing for every weekday in range
  notices   download + parse the ZSP (S&P index) announcement PDFs
  build     reconstruct membership, classify events, write data/history/history_australia_2010.json

Usage: python3 -I scripts/collect_australia_2010.py [sweep|notices|build|all] [--cache DIR]
"""
import argparse
import datetime as dt
import gzip
import html
import json
import os
import re
import subprocess
import sys
import time

import requests

UA = "MarketEventsTracker/1.0 historical-backfill (github.com/manoharkakdeai/claude)"
BASE = "https://www.asx.com.au/asx/v2/statistics"
DAY_URL = BASE + "/announcements.do?by=asxCode&asxCode=&timeframe=R&dateReleased={d}"
DOC_URL = BASE + "/displayAnnouncement.do?display=pdf&idsId={ids}"
START, END = dt.date(2010, 1, 1), dt.date(2021, 9, 30)
SWEEP_START = dt.date(2009, 12, 1)  # Dec 2009 so the Dec 2009 S&P review is seen too
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "data", "history", "history_australia_2010.json")
DEFAULT_CACHE = "/tmp/claude-0/-home-user-claude/acd3cf6e-52bc-5be0-ba82-933351f9e351/scratchpad/australia"
THROTTLE = 0.6

_session = None


def session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = UA
    return _session


def get(url, tries=4):
    for i in range(tries):
        try:
            r = session().get(url, timeout=60)
            time.sleep(THROTTLE)
            if r.status_code == 200:
                return r
            print(f"  HTTP {r.status_code} {url}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  error {e} {url}", file=sys.stderr)
        time.sleep(5 * (i + 1))
    return None


# ----------------------------------------------------------------------------- sweep
ROW_RE = re.compile(
    r'<td>([A-Z0-9]{3})</td>\s*<td>\s*(\d\d)/(\d\d)/(\d{4})<br>\s*<span class="dates-time">([^<]+)</span>'
    r'\s*</td>(.*?)idsId=(\d+)">\s*(.*?)\s*<br', re.S)


def parse_day(text):
    rows = []
    for code, d, m, y, tm, mid, ids, head in ROW_RE.findall(text):
        tm = tm.strip()
        mt = re.match(r"(\d+):(\d\d)\s*([ap]m)", tm)
        hhmm = None
        if mt:
            h = int(mt.group(1)) % 12 + (12 if mt.group(3) == "pm" else 0)
            hhmm = f"{h:02d}:{mt.group(2)}"
        rows.append({"code": code, "date": f"{y}-{m}-{d}", "time": hhmm,
                     "ps": "price sensitive" in mid, "ids": ids,
                     "headline": html.unescape(re.sub(r"\s+", " ", head)).strip()})
    return rows


def sweep(cache, reverse=False):
    """Fetch every weekday's full listing. reverse=True walks END->START (a second worker can
    run in reverse; both skip days already cached)."""
    ddir = os.path.join(cache, "days")
    os.makedirs(ddir, exist_ok=True)
    days = [SWEEP_START + dt.timedelta(days=i) for i in range((END - SWEEP_START).days + 1)]
    for day in (reversed(days) if reverse else days):
        if day.weekday() < 5:
            raw = os.path.join(ddir, f"{day}.html.gz")
            parsed = os.path.join(ddir, f"{day}.json")
            if not os.path.exists(parsed):
                r = get(DAY_URL.format(d=day.strftime("%d/%m/%Y")))
                if r is None or "announcement_data" not in r.text:
                    print(f"FAILED {day}", file=sys.stderr)
                else:
                    with gzip.open(raw, "wt", encoding="utf-8") as f:
                        f.write(r.text)
                    rows = parse_day(r.text)
                    with open(parsed, "w") as f:
                        json.dump(rows, f)
                    print(f"{day} {len(rows)}", flush=True)


def load_days(cache):
    ddir = os.path.join(cache, "days")
    out, missing = [], []
    day = SWEEP_START
    while day <= END:
        if day.weekday() < 5:
            p = os.path.join(ddir, f"{day}.json")
            if os.path.exists(p):
                out.extend(json.load(open(p)))
            else:
                missing.append(str(day))
        day += dt.timedelta(days=1)
    return out, missing


# ----------------------------------------------------------------------------- notices
def notices(cache):
    rows, _ = load_days(cache)
    ndir = os.path.join(cache, "zsp")
    os.makedirs(ndir, exist_ok=True)
    zsp = {}
    for r in rows:
        if r["code"] == "ZSP":
            zsp[r["ids"]] = r
    print(f"{len(zsp)} ZSP announcements", flush=True)
    for ids, r in sorted(zsp.items(), key=lambda kv: kv[1]["date"]):
        pdf = os.path.join(ndir, f"{ids}.pdf")
        txt = os.path.join(ndir, f"{ids}.txt")
        if not os.path.exists(pdf):
            p = get(DOC_URL.format(ids=ids))
            if p is None:
                continue
            m = re.search(r'name="pdfURL" value="([^"]+)"', p.text)
            if not m:
                print(f"  no pdfURL for {ids}", file=sys.stderr)
                continue
            f = get(m.group(1))
            if f is None or not f.content.startswith(b"%PDF"):
                continue
            open(pdf, "wb").write(f.content)
        if not os.path.exists(txt):
            subprocess.run(["pdftotext", "-layout", pdf, txt], check=False)
        with open(os.path.join(ndir, f"{ids}.meta.json"), "w") as fh:
            json.dump(r, fh)
    print("notices done", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["sweep", "notices", "build", "all"])
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--reverse", action="store_true", help="sweep from the end of the range backwards")
    a = ap.parse_args()
    if a.stage in ("sweep", "all"):
        sweep(a.cache, a.reverse)
    if a.stage in ("notices", "all"):
        notices(a.cache)
    if a.stage in ("build", "all"):
        from_build = globals().get("build")
        from_build(a.cache)
