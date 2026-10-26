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


def sweep(cache, reverse=False, start=None):
    """Fetch every weekday's full listing. reverse=True walks END->START (a second worker can
    run in reverse; both skip days already cached)."""
    ddir = os.path.join(cache, "days")
    os.makedirs(ddir, exist_ok=True)
    first = start or SWEEP_START
    days = [first + dt.timedelta(days=i) for i in range((END - first).days + 1)]
    for day in (reversed(days) if reverse else days):
        if day.weekday() < 5:
            raw = os.path.join(ddir, f"{day}.html.gz")
            parsed = os.path.join(ddir, f"{day}.json")
            if not os.path.exists(parsed):
                r = get(DAY_URL.format(d=day.strftime("%d/%m/%Y")))
                holiday = r is not None and "No company announcements have been published" in r.text
                if not holiday and (r is None or "announcement_data" not in r.text):
                    print(f"FAILED {day}", file=sys.stderr)
                else:  # holidays are cached as an empty listing
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
SP_HEAD = re.compile(r"^\s*ZSP\s*:|^\s*(SandP|S&P DJI|S&P DJ Indices|S&P Dow Jones Indices|S and P)\b.*"
                     r"(remove|rebalanc|treatment|replace|review)|to be removed from the S&P/ASX", re.I)


def notices(cache):
    rows, _ = load_days(cache)
    ndir = os.path.join(cache, "zsp")
    os.makedirs(ndir, exist_ok=True)
    zsp = {}
    for r in rows:
        # S&P index notices: released under code ZSP, and occasionally only under the affected company with
        # the headline prefixed "ZSP:" or in S&P's own wording
        if r["code"] == "ZSP" or SP_HEAD.search(r["headline"]):
            zsp.setdefault(r["ids"], r)
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



# ----------------------------------------------------------------------------- notice parsing
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
DATE_RE = (r"(?:(?P<mon1>[A-Z][a-z]{2,8})\.?\s*(?P<d1>\d{1,2}),?\s+(?P<y1>20\d\d)"
           r"|(?P<d2>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon2>[A-Z][a-z]{2,8})\.?,?\s+(?P<y2>20\d\d))")
EFF_RE = re.compile(r"(?P<kind>after\s+the\s+close|prior\s+to\s+the\s+open|before\s+the\s+open|"
                    r"at\s+the\s+open|close\s+of\s+(?:trading|business))[^.]{0,60}?" + DATE_RE, re.I | re.S)


def _mkdate(mon, d, y):
    m = MONTHS.get(mon[:3].lower())
    if not m:
        return None
    try:
        return dt.date(int(y), m, int(d))
    except ValueError:
        return None


def find_effective(text):
    """First effective date in text -> date the new membership applies from."""
    m = EFF_RE.search(text)
    if not m:
        return None
    g = m.groupdict()
    d = _mkdate(g["mon1"], g["d1"], g["y1"]) if g["mon1"] else _mkdate(g["mon2"], g["d2"], g["y2"])
    if d is None:
        return None
    after_close = "close" in m.group("kind").lower()
    return d + dt.timedelta(days=1) if after_close else d


TABROW_RE = re.compile(r"^\s*S&P\s*/\s*ASX\s+(?P<idx>[\w ]+?)\s{2,}(?P<act>Addition|Removal|Deletion|Add|Delete)\s+"
                       r"(?P<code>[A-Z][A-Z0-9]{2})\s+(?P<name>\S.*)$")
HEAD_RE = re.compile(r"^\s*S&P\s*/\s*ASX\s+(.*)$")
ROW_LINE = re.compile(r"^\s*(?:(Addition|Additions|Add|Removal|Removals|Deletion|Deletions|Delete|Drop)\s+)?"
                      r"([A-Z][A-Z0-9]{2})\s{2,}(\S.*?)\s*$")


def is_200_heading(rest):
    """rest = text after 'S&P/ASX ' on a heading line."""
    r = rest.strip()
    return bool(re.match(r"^200(\s+Index)?(\s*[–—-].*)?$", r)) or bool(
        re.match(r"^200\s+(Index\s+)?(–|-|—)?\s*(Effective|After|Prior|As of)", r, re.I))


def parse_notice(text):
    """Return list of (effective_from, action, code, name) for the S&P/ASX 200 found in one notice."""
    lines = [l for l in text.splitlines() if "For personal use only" not in l]
    doc_eff = find_effective(text)
    out = []
    in200, mode, sec_eff, para_eff = False, None, None, None
    for idx, l in enumerate(lines):
        h = HEAD_RE.match(l)
        if not h:
            pe = find_effective(" ".join(x.strip() for x in lines[idx:idx + 2]))
            if pe and re.search(r"effective|after the close|prior to the open", l, re.I):
                para_eff = pe
        tr = TABROW_RE.match(l)
        if tr:  # "S&P/ASX 200   Addition   AZT   Aston Resources Limited" (index named on every row)
            if re.fullmatch(r"200(\s+Index)?", tr.group("idx").strip()):
                a = "add" if tr.group("act").lower().startswith("add") else "remove"
                out.append((sec_eff or para_eff or doc_eff, a, tr.group("code"), tr.group("name").strip()))
            continue
        if h and len(l.strip()) < 120:
            rest = h.group(1)
            in200 = is_200_heading(rest)
            mode = None
            sec_eff = find_effective(l) if in200 else None
            if in200 and sec_eff is None:
                m = re.search(DATE_RE, rest)
                if m:
                    g = m.groupdict()
                    d = _mkdate(g["mon1"], g["d1"], g["y1"]) if g["mon1"] else _mkdate(g["mon2"], g["d2"], g["y2"])
                    if d:
                        sec_eff = d + dt.timedelta(days=1) if re.search(r"after|close", rest, re.I) else d
            continue
        if not in200:
            continue
        st = l.strip()
        if re.match(r"^(ADDITIONS?|INCLUSIONS?)\b", st, re.I) and len(st) < 30:
            mode = "add"; continue
        if re.match(r"^(REMOVALS?|DELETIONS?|EXCLUSIONS?)\b", st, re.I) and len(st) < 30:
            mode = "remove"; continue
        m = ROW_LINE.match(l)
        if m and m.group(2) not in ("ASX", "THE", "FOR", "AND") or (m and m.group(1)):
            act = m.group(1)
            a = mode
            if act:
                a = "add" if act.lower().startswith("add") else "remove"
            if a and not m.group(3).lower().startswith(("name", "company")):
                out.append((sec_eff or para_eff or doc_eff, a, m.group(2), re.sub(r"\s{2,}.*$", "", m.group(3))))
            continue
        if st and len(st) > 60 and a_is_prose_end(st):
            in200 = False
    if not out:
        out = parse_prose(lines, doc_eff)
    return out


CODE_P = r"\(\s*(?:X?ASX|ASX\s*code)\s*:\s*(?P<{g}>[A-Z][A-Z0-9]{{2}})\s*\)"
NAME_P = r"(?P<{g}>[A-Z][\w&.,'\-]*(?:\s+[\w&.,'()\-]+){{0,9}}?)"
IDX200 = (r"S&P\s*/\s*ASX\s+200\b(?!\s*(?:A-REIT|Resources|Financ|Energy|Materials|Industrials|Consumer|Health|"
          r"Information|Telecom|Utilities|Real|Communication|Gold|Metals|Banks|Property|Equal|Shariah|ESG|Dividend))")
PROSE = [
    ("remove", re.compile(r"remove\s+" + NAME_P.format(g="n") + r"\s*" + CODE_P.format(g="c") +
                          r"\s+from\s+the\s+" + IDX200)),
    ("remove_ind", re.compile(r"remove\s+" + NAME_P.format(g="n") + r"\s*" + CODE_P.format(g="c") +
                              r"\s+from\s+the\s+S&P\s*/\s*ASX\s+indices", re.I)),
    ("add", re.compile(r"replaced\s+by\s+" + NAME_P.format(g="n") + r"\s*" + CODE_P.format(g="c") +
                       r"\s+in\s+the\s+" + IDX200)),
    ("add", re.compile(r"add\s+" + NAME_P.format(g="n") + r"\s*" + CODE_P.format(g="c") +
                       r"\s+to\s+the\s+" + IDX200)),
    ("add", re.compile(NAME_P.format(g="n") + r"\s*" + CODE_P.format(g="c") +
                       r"\s+will\s+(?:be\s+added\s+to|join)\s+the\s+" + IDX200)),
    ("replace", re.compile(NAME_P.format(g="n") + r"\s*" + CODE_P.format(g="c") + r"\s+will\s+replace\s+" +
                           NAME_P.format(g="n2") + r"\s*" + CODE_P.format(g="c2") + r"\s+in\s+the\s+" + IDX200)),
]


def parse_prose(lines, doc_eff):
    """Sentence-level parsing for notices without change tables. Each sentence uses its own effective
    date when it states one. Additions phrased 'include X in all S&P/ASX Indices of which Y is a
    constituent' are conditional (cond=True) because they depend on Y's membership."""
    flat = re.sub(r"\s+", " ", " ".join(lines))
    codes = {}
    for m in re.finditer(NAME_P.format(g="n") + r"\s*" + CODE_P.format(g="c"), flat):
        codes[m.group("n").strip()] = m.group("c")

    def code_of(name):
        name = name.strip()
        if name in codes:
            return codes[name]
        for n, c in codes.items():
            if n.endswith(name) or name.endswith(n):
                return c
        return None

    out = []
    sentences = re.split(r"(?<=[a-z0-9)\]])\.\s+(?=[A-Z])", flat)
    for sent in sentences:
        eff = find_effective(sent) or doc_eff
        adds, rems, rem_ind = {}, {}, {}
        for kind, rx in PROSE:
            for m in rx.finditer(sent):
                if kind == "add":
                    adds[m.group("c")] = m.group("n")
                elif kind == "remove":
                    rems[m.group("c")] = m.group("n")
                elif kind == "remove_ind":
                    rem_ind[m.group("c")] = m.group("n")
                else:
                    adds[m.group("c")] = m.group("n")
                    rems[m.group("c2")] = m.group("n2")
        for m in re.finditer(NAME_P.format(g="n") + r"\s+will\s+be\s+added\s+to\s+the\s+" + IDX200, sent):
            c = code_of(m.group("n"))
            if c:
                adds.setdefault(c, m.group("n"))
        for m in re.finditer(NAME_P.format(g="n") + r"\s+will\s+(?:not\s+)?be\s+removed\s+from\s+the\s+"
                             r"(?:S&P\s*/\s*ASX\s+[\w ]+?,\s+|S&P\s*/\s*ASX\s+[\w ]+?\s+and\s+)*" + IDX200, sent):
            c = code_of(m.group("n"))
            if c:
                rems.setdefault(c, m.group("n"))
        for m in re.finditer(r"include\s+" + NAME_P.format(g="n") + r"(?:\s*" + CODE_P.format(g="c") + r")?\s+in\s+all\s+"
                             r"S&P\s*/\s*ASX\s+Indices\s+of\s+which", sent):
            c = m.group("c") or code_of(m.group("n"))
            if c and c not in adds:
                out.append((eff, "add?", c, m.group("n").strip()))
        if adds:
            for c, n in rem_ind.items():
                rems.setdefault(c, n)
        out += [(eff, "add", c, n.strip()) for c, n in adds.items()]
        out += [(eff, "remove", c, n.strip()) for c, n in rems.items() if c not in adds]
    # keep one entry per (action, code): the last sentence wins (later sentences restate/qualify)
    last = {}
    for e in out:
        last[(e[1].rstrip("?"), e[2])] = e
    return list(last.values())


def a_is_prose_end(st):
    return bool(re.match(r"^(Company additions|For more information|About S&P|S&P Dow Jones Indices|Any changes|"
                         r"The S&P/ASX|Index changes|Please note)", st))



# ----------------------------------------------------------------------------- membership
INDEX = "S&P/ASX 200"
# Anchor constituent list: Wikipedia "S&P/ASX 200" article, revision 1047236919 (2021-09-29), whose table
# reflects the June 2021 review plus intra-quarter changes to July 2021 (edit 1033242282 "update for changes
# 11 June-10 July") and NOT yet the 20 Sep 2021 review. It is treated as the membership in force on
# 2021-09-19 and every change before that date is undone backwards from it; the S&P notices are then
# checked for consistency against it (see report).
ANCHOR_REVID = "1047236919"
ANCHOR_URL = "https://en.wikipedia.org/w/index.php?oldid=1047236919&action=raw"
ANCHOR_DATE = dt.date(2021, 9, 19)


def fetch_anchor(cache):
    raw = os.path.join(cache, f"wiki_{ANCHOR_REVID}.txt")
    if not os.path.exists(raw):
        r = get(ANCHOR_URL)
        open(raw, "w").write(r.text)
    t = open(raw).read()
    tab = t[t.find("wikitable"):]
    tab = tab[:tab.find("\n|}")]
    out = {}
    for r in tab.split("\n|-")[1:]:
        cells = []
        for line in r.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("!"):
                continue
            cells.extend(c.strip() for c in line.lstrip("|").split("||"))
        if len(cells) < 2:
            continue
        code = cells[0].split("|")[0].strip()
        name = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", cells[1]).strip()
        name = re.sub(r"<[^>]+>|\{\{[^}]*\}\}", "", name).strip()
        if re.fullmatch(r"[A-Z0-9]{3}", code):
            out[code] = name
    return out


MARCH_2020_POSTPONED = (dt.date(2020, 3, 1), dt.date(2020, 3, 20))


def load_changes(cache):
    ndir = os.path.join(cache, "zsp")
    changes, unparsed = [], []
    for fn in sorted(os.listdir(ndir)):
        if not fn.endswith(".meta.json"):
            continue
        meta = json.load(open(os.path.join(ndir, fn)))
        txtp = os.path.join(ndir, meta["ids"] + ".txt")
        if not os.path.exists(txtp):
            unparsed.append((meta, "pdf not retrieved"))
            continue
        res = parse_notice(open(txtp, errors="replace").read())
        mentions200 = "ASX 200" in open(txtp, errors="replace").read()
        txt = open(txtp, errors="replace").read()
        if re.search(r"S&P\s*/\s*ASX\s+200(\s+Index)?\s*[\u2013\u2014:-]?\s*No\s+change", txt, re.I):
            mentions200 = False  # explicit "no change" for the S&P/ASX 200
        if not res and mentions200 and not re.search(r"Index Values|Consultation|Methodology|Results|"
                                                      r"Sector|Confirmation|Annual|Market Cap", meta["headline"], re.I):
            unparsed.append((meta, "mentions S&P/ASX 200 but no change rows parsed"))
        for eff, act, code, name in res:
            rel = dt.date.fromisoformat(meta["date"])
            if eff is None:
                eff = rel + dt.timedelta(days=1)
            if MARCH_2020_POSTPONED[0] <= rel <= MARCH_2020_POSTPONED[1] and eff == dt.date(2020, 3, 23):
                # S&P DJI postponed the March 2020 review (ZSP notices 02216370, 02221274); its changes
                # took effect prior to the open on 22 June 2020
                eff = dt.date(2020, 6, 22)
            if eff < rel - dt.timedelta(days=3):  # e.g. a notice of Mar 2011 stating "March 9, 2010"
                try:
                    eff = eff.replace(year=rel.year)
                except ValueError:
                    pass
            changes.append({"eff": eff, "act": act.rstrip("?"), "cond": act.endswith("?"), "code": code, "name": name,
                            "ids": meta["ids"], "released": meta["date"], "headline": meta["headline"]})
    # the same change is often announced more than once (preliminary + confirmation/update): keep the
    # latest-released notice for a (code, action) pair within 75 days of each other
    changes.sort(key=lambda c: (c["code"], c["act"], c["released"]))
    dedup = []
    for c in changes:
        if dedup and dedup[-1]["code"] == c["code"] and dedup[-1]["act"] == c["act"] and \
                abs((c["eff"] - dedup[-1]["eff"]).days) <= 75:
            dedup[-1] = c
            continue
        dedup.append(c)
    return dedup, unparsed


def correct_anchor(anchor, changes, seen_codes_2021):
    """S&P notices take precedence over the Wikipedia table for changes in the 24 months before the
    anchor date: a code whose latest such change is a removal is dropped; one whose latest change is an
    addition is added if it still has ASX announcements under that code in 2021 (else it changed code)."""
    fixed, notes = dict(anchor), []
    last = {}
    for c in sorted(changes, key=lambda c: c["eff"]):
        if c["eff"] <= ANCHOR_DATE:
            last[c["code"]] = c
    for code, c in last.items():
        if c["eff"] < ANCHOR_DATE - dt.timedelta(days=730):
            continue
        if c["act"] == "remove" and code in fixed:
            del fixed[code]
            notes.append(f"dropped {code} (S&P removal effective {c['eff']}, notice {c['released']})")
        elif c["act"] == "add" and code not in fixed and code in seen_codes_2021:
            fixed[code] = c["name"]
            notes.append(f"added {code} (S&P addition effective {c['eff']}, notice {c['released']})")
    return fixed, notes


def reconstruct(anchor, changes):
    """Backwards from the anchor set; returns (timeline, issues).
    timeline: list of (from_date, frozenset) sorted ascending; set applies from from_date."""
    state = set(anchor)
    issues = []
    by_eff = {}
    for c in changes:
        by_eff.setdefault(c["eff"], []).append(c)
    timeline = []
    # forward over changes after the anchor date (to END)
    fwd = set(state)
    for eff in sorted(e for e in by_eff if e > ANCHOR_DATE and e <= END):
        for c in by_eff[eff]:
            if c["act"] == "add":
                fwd.add(c["code"])
            else:
                fwd.discard(c["code"])
        timeline.append((eff, frozenset(fwd)))
    cur_from = ANCHOR_DATE
    for eff in sorted((e for e in by_eff if e <= ANCHOR_DATE), reverse=True):
        timeline.append((eff, frozenset(state)))
        grp = by_eff[eff]
        both = {c["code"] for c in grp if c["act"] == "add"} & {c["code"] for c in grp if c["act"] == "remove"}
        for c in grp:
            if c["code"] in both:
                continue  # added and removed with the same effective date: no net change
            if c["act"] == "add":
                if c["code"] in state:
                    state.discard(c["code"])
                elif not c.get("cond"):
                    issues.append(dict(c, problem="addition not in later set"))
            else:
                if c["code"] not in state:
                    state.add(c["code"])
                else:
                    issues.append(dict(c, problem="removal already in earlier set"))
        if eff < dt.date(2009, 12, 1):
            break
    timeline.append((dt.date(2000, 1, 1), frozenset(state)))
    timeline.sort(key=lambda t: t[0])
    return timeline, issues


def member_set(timeline, d):
    cur = timeline[0][1]
    for f, sset in timeline:
        if f <= d:
            cur = sset
        else:
            break
    return cur


# ----------------------------------------------------------------------------- classification
EXCL_NOTICE = re.compile(r"\b(date|dates|timing|teleconference|webcast|web cast|invitation|reminder|advance notice|"
                         r"conference call|briefing details|to be released|release of|notification|notice of|"
                         r"live stream|livestream|dial[- ]in|audio|arrangements|transcript|template|templates|"
                         r"excel|aware letter|response to|available|availability|upcoming|schedule)\b", re.I)
AMEND = re.compile(r"\b(amend|amended|correct|corrected|correction|revised|replacement|erratum|clarif|updated|"
                   r"update to|restated)\w*", re.I)
RES0 = re.compile(r"appendi(?:x|ces)\s*4\s*[de]\b|app(?:x|\.)?\s*4\s*[de]\b|\b4[de]\b.*\b(report|accounts|results)\b|"
                  r"preliminary\s+final\s+(report|results)|half[- ]?year(?:ly)?\s+(financial\s+)?(report|accounts)|"
                  r"interim\s+financial\s+(report|statements)|financial\s+report\s+for\s+the\s+half|"
                  r"(full|half)[- ]?year(ly)?\s+(statutory\s+)?(accounts|report|financial\s+(report|statements))|"
                  r"statutory\s+accounts|condensed\s+financial\s+report|financial\s+report\s+(-\s+)?half[- ]year|"
                  r"^\s*([\w&.'-]+\s+)?(19|20)\d\d\s+financial\s+report\s*$|\binterim\s+report\b|"
                  r"\b(1h|h1|hy|2h|fy)\s*'?\d{2,4}\s+(financial|investor)\s+report", re.I)
RES1 = re.compile(r"\b(full|half|first[- ]half|second[- ]half|interim|annual|final|fy|hy|[12]h|h[12])"
                  r"[- ]?(year|yr)?\s*('?(fy)?\d{2,4}\s*)?(financial\s+)?(results?|earnings|profit|accounts)\b|"
                  r"\bresults?\s+for\s+(the\s+)?(half|full|year|six|twelve|financial|period|quarter|qtr|first|second|"
                  r"third|fourth|nine|three|fiscal|\d{1,2}\s+months)|"
                  r"\b(half|full|financial|fiscal)[- ]year\b.{0,30}\bresults?\b|"
                  r"^\s*([\w&.'-]+\s+){0,3}(19|20)\d\d\s+(full[- ]year\s+|half[- ]year\s+)?results?\s*$|"
                  r"\bresults?\s+presentation\b|"
                  r"\b(fy|hy|[12]h|h[12])\s*'?\d{2,4}\s+(results?|earnings|profit)|"
                  r"\b(profit|results?|earnings)\s+(announcement|release|report)\b|"
                  r"\b(19|20)\d\d\s+(full|half|interim|annual)[- ]year\s+(results?|profit)|"
                  r"^\s*(\d{4}\s+)?results\s+presentation|"
                  r"(half|full)[- ]?year(ly)?\s+(investor\s+)?(presentation|media\s+release)", re.I)
RES_BAD = re.compile(r"\bresults?\s+of\b|\bagm\b|annual general|to announce|will announce|details|impact|letter|"
                     r"calendar|"
                     r"anticipat|expected|guidance|forecast|outlook|market update|upgrade|"
                     r"downgrade|unaudited|estimate|drill|assay|exploration|trial|study|clinical|scheme|offer|meeting|"
                     r"retail|placement|entitlement|buy-?back|tender|drilling|bookbuild|sale|spp|"
                     r"share purchase|vote|ballot|auction|fund[s]?\b.*\bresults? of", re.I)
AGM_RES = re.compile(r"(results?|outcome|poll results|voting results)\b.*\b(annual general meeting|agm)\b|"
                     r"\b(annual general meeting|agm)\b.*\b(results?|outcome|poll|voting results|resolutions)\b", re.I)
AGM_BAD = re.compile(r"date|form|guide|registration|details|online|arrangements|notice|letter|financial results?|"
                     r"full[- ]year|half[- ]year|\bfy\d|\bhy\d|[12]h\d|profit|q&a|questions", re.I)
AGM_ADDR = re.compile(r"\b(annual general meeting|agm)\b.*\b(address|addresses|speech|speeches|presentation|"
                      r"presentations|slides|remarks)\b|"
                      r"\b(address|addresses|speech|speeches|presentation|slides|remarks)\b.*\b(annual general meeting|agm)\b",
                      re.I)
CHAIR_ADDR = re.compile(r"\b(chairman|chairman's|chairman`s|chair's|chair|chairmans)\s+(and\s+.{0,40})?(address|speech)",
                        re.I)
AGM_NOTICE = re.compile(r"notice\s+of\s+(the\s+)?(\d{4}\s+)?(annual\s+general\s+meeting|agm|annual\s+meeting)|"
                        r"\b(annual\s+general\s+meeting|agm)\s+notice|notice\s+of\s+meeting", re.I)
GEN_RES = re.compile(r"^\s*results?\s+of\s+(the\s+)?(\d{4}\s+)?meeting\b|^\s*meeting\s+results\b|"
                     r"^\s*results\s+of\s+(the\s+)?general\s+meeting", re.I)
NOT_AGM = re.compile(r"extraordinary|\begm\b|scheme|court|bondholder|noteholder|unitholder meeting of|"
                     r"general meeting of .*holders|class meeting|notice of general meeting", re.I)
AR = re.compile(r"\bannual\s+(financial\s+)?reports?\b|\b20-?F\b", re.I)
AR_BAD = re.compile(r"semi|letter|availab|access|online|appendix\s*4g|corporate governance|sustainab|notice of|"
                    r"proxy|date|lodg(e)?ment of|electronic|reminder|election|mailing|shareholder communication|"
                    r"despatch|dispatch|errat|half[- ]year", re.I)
INVDAY = re.compile(r"investor\s+(day|days|strategy\s+day|open\s+day|briefing\s+day)|strategy\s+(day|briefing\s+day)|"
                    r"capital\s+markets?\s+day|investor\s+and\s+analyst\s+day|analyst\s+(and\s+investor\s+)?day", re.I)
QTR = re.compile(r"quarterly\s+(activities|activity|report|cash\s*-?\s*flow|production|operations|operational|review)|"
                 r"appendi(?:x|ces)\s*4c\b|appendi(?:x|ces)\s*5b\b|\b(4c|5b)\b.*quarter|quarter.*\b(4c|5b)\b|"
                 r"\b(first|second|third|fourth|march|june|september|december|mar|jun|sep|sept|dec)\s+(\d{4}\s+)?"
                 r"quarter(ly)?\s+(\d{4}\s+)?(activities|activity|report|production|operations|operational|review|"
                 r"update|results)|^\s*quarterly\s+report\s*$|\bq[1-4]\s+(fy)?\d{2,4}\s+(activities|production|report)", re.I)
QTR_BAD = re.compile(r"announce|results|monthly|filed|form 10|distribution|dividend|interest|coupon|investment update|\bnta\b|portfolio|fund|"
                     r"options|teleconference|webcast|conference call|date|briefing|presentation", re.I)

PERIOD_RE = re.compile(r"\b(?P<q>Q[1-4])\s*(?P<qfy>FY)?\s*'?(?P<qy>\d{2,4})\b|"
                       r"\b(?P<h>[12]H|H[12]|HY)\s*(?P<hfy>FY)?\s*'?(?P<hy>\d{2,4})\b|"
                       r"\bFY\s*'?(?P<fy>\d{2,4})\b", re.I)


def norm_year(y):
    y = int(y)
    return y + 2000 if y < 100 else y


def extract_period(headline):
    m = PERIOD_RE.search(headline)
    if not m:
        return None
    g = m.groupdict()
    try:
        if g["q"]:
            y = norm_year(g["qy"])
            return f"{g['q'].upper()} FY{y % 100:02d}" if 2009 <= y <= 2022 else None
        if g["h"]:
            y = norm_year(g["hy"])
            h = {"1H": "H1", "H1": "H1", "2H": "H2", "H2": "H2", "HY": "H1"}[g["h"].upper()]
            return f"{h} FY{y % 100:02d}" if 2009 <= y <= 2022 else None
        y = norm_year(g["fy"])
        if not 2009 <= y <= 2022:
            return None
        if re.search(r"half[- ]?year|interim|\b4d\b|\bhy\b|1st half|first half", headline, re.I):
            return f"H1 FY{y % 100:02d}"
        return f"FY{y % 100:02d}"
    except (TypeError, ValueError):
        return None


def classify(h):
    """Return list of (type, priority, kind) for a headline. Lower priority = better document."""
    out = []
    h = h.replace("_", " ").replace("`", "'")
    if SP_HEAD.search(h):  # S&P index notices are also tagged with every affected company's code
        return out
    if RES0.search(h) and not EXCL_NOTICE.search(h):
        out.append(("Results", 3 if AMEND.search(h) else 0, "results"))
    elif RES1.search(h) and not RES_BAD.search(h) and not EXCL_NOTICE.search(h):
        pr = 2 if re.search(r"presentation|slides|briefing|webcast|call", h, re.I) else 1
        out.append(("Results", 3 if AMEND.search(h) else pr, "results"))
    agm_ok = not NOT_AGM.search(h) and not AGM_NOTICE.search(h) and not AGM_BAD.search(h) \
        and not EXCL_NOTICE.search(h)
    if AGM_RES.search(h) and agm_ok:
        out.append(("AGM", 0, "agm_results"))
    elif AGM_ADDR.search(h) and agm_ok:
        out.append(("AGM", 2, "agm_address"))
    elif GEN_RES.search(h) and not NOT_AGM.search(h):
        out.append(("AGM?", 1, "meeting_results"))
    elif CHAIR_ADDR.search(h) and agm_ok and not re.search(r"general meeting", h, re.I):
        out.append(("AGM?", 2, "chair_address"))
    if AGM_NOTICE.search(h) and not NOT_AGM.search(h):
        out.append(("AGMNOTICE", 9, "agm_notice"))
    if AR.search(h) and not AR_BAD.search(h):
        out.append(("Annual report", 0, "annual_report"))
    if INVDAY.search(h) and not EXCL_NOTICE.search(h) and not re.search(
            r"presenting at|conference|confirms|to host|will host|details|virtual investor day|switzer|announces|"
            r"to be held|upcoming", h, re.I):
        out.append(("Investor day", 0, "investor_day"))
    if QTR.search(h) and not QTR_BAD.search(h):
        out.append(("Filing", 0, "quarterly"))
    return out



# ----------------------------------------------------------------------------- build
def _d(x):
    return dt.date.fromisoformat(x)


def cluster(items, gap_days):
    """items sorted by date; groups items whose dates are within gap_days of the previous item."""
    groups = []
    for it in items:
        if groups and (_d(it["date"]) - _d(groups[-1][-1]["date"])).days <= gap_days:
            groups[-1].append(it)
        else:
            groups.append([it])
    return groups


def window_cluster(items, days):
    """items sorted by date; a new group starts once a date is more than `days` after the group's first."""
    groups = []
    for it in sorted(items, key=lambda r: (r["date"], r["time"] or "")):
        if groups and (_d(it["date"]) - _d(groups[-1][0]["date"])).days <= days:
            groups[-1].append(it)
        else:
            groups.append([it])
    return groups


HDATE_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|"
                      r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?,?\s+(20\d\d)\b", re.I)


def future_dated(r):
    """Headline names a date after its release date, e.g. 'FPH announce half year results 24 November 2010'
    released on 9 Nov: a notice of an upcoming event, not the event."""
    for d, mon, y in HDATE_RE.findall(r["headline"]):
        x = _mkdate(mon, d, y)
        if x and x > _d(r["date"]):
            return True
    return False


def best(group):
    return sorted(group, key=lambda r: (r["prio"], r["date"], r["time"] or "99", r["ids"]))[0]


def build(cache):
    rows, missing_days = load_days(cache)
    anchor = fetch_anchor(cache)
    changes, unparsed = load_changes(cache)
    seen21 = {r["code"] for r in rows if r["date"] >= "2021-01-01"}
    anchor, anchor_notes = correct_anchor(anchor, changes, seen21)
    timeline, issues = reconstruct(anchor, changes)
    # a removal that finds the code already in the earlier set means a re-addition was missed: the code's
    # membership between that removal and its next recorded change is unknown, so it is treated as non-member
    uncertain = {}
    for i in issues:
        if i["problem"].startswith("removal"):
            nxt = [c["eff"] for c in changes if c["code"] == i["code"] and c["eff"] > i["eff"]]
            uncertain.setdefault(i["code"], []).append((i["eff"], min(nxt) if nxt else END + dt.timedelta(days=1)))

    names = {}  # code -> list of (date, name)
    for code, nm in anchor.items():
        names.setdefault(code, []).append((ANCHOR_DATE, nm))
    for c in changes:
        names.setdefault(c["code"], []).append((c["eff"], c["name"]))

    def name_for(code, d):
        return min(names[code], key=lambda x: abs((x[0] - d).days))[1]

    universe = set()
    for f, sset in timeline:
        universe |= sset

    # announcements per code, for diagnostics
    seen = {}
    for r in rows:
        seen.setdefault(r["code"], []).append(r["date"])
    # member spans per code (contiguous, clipped to START..END)
    spans = {}
    for k, (f, sset) in enumerate(timeline):
        to = timeline[k + 1][0] if k + 1 < len(timeline) else END + dt.timedelta(days=1)
        lo, hi = max(f, START), min(to, END + dt.timedelta(days=1))
        if lo >= hi:
            continue
        for code in sset:
            sp = spans.setdefault(code, [])
            if sp and sp[-1][1] == lo:
                sp[-1][1] = hi
            else:
                sp.append([lo, hi])
    # spans with no ASX announcements under the code: before the code's first or after its last
    # announcement in the archive (the archive drops delisted issuers and pre-code-change history)
    diag = []
    for code, sp in sorted(spans.items()):
        ds = sorted(seen.get(code, []))
        first = _d(ds[0]) if ds else None
        last = _d(ds[-1]) if ds else None
        for lo, hi in sp:
            parts = [(lo, hi)] if first is None else [(lo, min(hi, first)), (max(lo, last + dt.timedelta(days=1)), hi)]
            for a, b in parts:
                if (b - a).days > 30:
                    diag.append({"code": code, "from": str(a), "to": str(b - dt.timedelta(days=1)),
                                 "problem": "no ASX announcements under this code"})
    cand = {}
    for r in rows:
        if r["code"] not in universe or not (START.isoformat() <= r["date"] <= END.isoformat()):
            continue
        for typ, prio, kind in classify(r["headline"]):
            cand.setdefault((r["code"], kind), []).append(dict(r, type=typ, prio=prio, kind=kind))

    def member(code, date):
        d = _d(date)
        if any(a <= d < b for a, b in uncertain.get(code, [])):
            return False
        return code in member_set(timeline, d)

    events = []

    def emit(r, typ, extra_heads=()):
        if not member(r["code"], r["date"]):
            return
        period = None
        if typ in ("Results", "Filing"):
            period = extract_period(r["headline"])
            for h in extra_heads:
                period = period or extract_period(h)
        ev = {"date": r["date"], "market": "Australia", "index": INDEX, "symbol": r["code"],
              "company": name_for(r["code"], _d(r["date"])), "type": typ}
        if r.get("time"):
            ev["time"] = r["time"]
        if period:
            ev["period"] = period
        ev.update({"title": r["headline"], "doc_url": DOC_URL.format(ids=r["ids"]),
                   "doc_source": "exchange", "source": "ASX announcements"})
        events.append(ev)

    codes = sorted({k[0] for k in cand})
    for code in codes:
        get_ = lambda kind: sorted(cand.get((code, kind), []), key=lambda r: (r["date"], r["time"] or ""))
        # Results: one row per reporting event. Releases within 60 days of the first one (results
        # announcement, presentation, later glossy half-year report) are one event, dated by the earliest
        # primary release (4D/4E/results announcement; a presentation only when nothing else exists).
        res = [r for r in get_("results") if not future_dated(r)]
        for g in window_cluster(res, 60):
            prim = [r for r in g if r["prio"] <= 1] or [r for r in g if r["prio"] == 2] or g
            first_day = min(r["date"] for r in prim)
            b = best([r for r in prim if r["date"] == first_day])
            emit(b, "Results", [x["headline"] for x in g])
        # AGM
        notices_ = [_d(r["date"]) for r in get_("agm_notice")]
        sure = [r for r in get_("agm_results") + get_("agm_address") if not future_dated(r)]
        gated = [r for r in get_("meeting_results") + get_("chair_address")
                 if any(0 <= (_d(r["date"]) - n).days <= 100 for n in notices_)]
        sure_groups = window_cluster(sorted(sure, key=lambda r: r["date"]), 60)
        gated_groups = [g for g in window_cluster(sorted(gated, key=lambda r: r["date"]), 60)
                        if not any(abs((_d(g[0]["date"]) - _d(sg[0]["date"])).days) <= 150 for sg in sure_groups)]
        for g in sure_groups + gated_groups:
            first = min(g, key=lambda r: r["date"])
            b = best([r for r in g if r["date"] == first["date"]])
            row = b
            emit(row, "AGM", [x["headline"] for x in g])
        # Annual report: one per 60-day window (annual report, concise report, 20-F), earliest non-20-F copy
        ars = [r for r in get_("annual_report") if not future_dated(r)]
        for g in window_cluster(ars, 60):
            non20f = [r for r in g if not re.search(r"20-?F", r["headline"])] or g
            first_day = min(r["date"] for r in non20f)
            b = sorted([r for r in non20f if r["date"] == first_day], key=lambda r: (r["time"] or "", r["ids"]))[0]
            emit(b, "Annual report", [x["headline"] for x in g])
        # Investor day
        for g in cluster([r for r in get_("investor_day") if not future_dated(r)], 3):
            emit(min(g, key=lambda r: (r["date"], r["time"] or "")), "Investor day", [x["headline"] for x in g])
        # Quarterly filings
        for g in cluster(get_("quarterly"), 5):
            emit(min(g, key=lambda r: (r["date"], r["time"] or "")), "Filing", [x["headline"] for x in g])

    # de-duplicate on the merge key
    uniq = {}
    for e in sorted(events, key=lambda e: (e["date"], e["symbol"], e["type"], e.get("time", ""))):
        k = (e["market"], e["symbol"], e["type"], e["date"], e.get("period", ""))
        uniq.setdefault(k, e)
    events = sorted(uniq.values(), key=lambda e: (e["date"], e["symbol"], e["type"]))

    gaps = build_gaps(missing_days, unparsed, issues, diag, timeline)
    for n in anchor_notes:
        gaps.append({"scope": "membership anchor", "reason": "Wikipedia anchor list corrected from S&P notices: " + n})
    for code, spans in uncertain.items():
        gaps.append({"scope": f"symbol {code}", "reason": "index membership unknown (missed S&P re-addition) for "
                     + "; ".join(f"{a}..{b}" for a, b in spans) + "; no rows emitted for those dates"})
    doc = {
        "market": "Australia",
        "range": {"from": "2010-01", "to": "2021-09"},
        "sources": [
            "ASX announcements search, per-date full-market listing: " + DAY_URL.format(d="DD/MM/YYYY"),
            "ASX announcement documents: " + DOC_URL.format(ids="<idsId>"),
            "S&P Dow Jones Indices S&P/ASX index announcements released on the ASX platform under code ZSP "
            "(quarterly reviews and intra-quarter changes), parsed for S&P/ASX 200 additions/removals",
            "Anchor constituent list: Wikipedia 'S&P/ASX 200' article revision " + ANCHOR_REVID + " (" + ANCHOR_URL + ")",
        ],
        "membership_method": (
            "S&P/ASX 200 membership on each event date was rebuilt backwards from the anchor list (taken as the "
            "membership in force on " + str(ANCHOR_DATE) + ") by undoing every S&P/ASX 200 addition/removal parsed "
            "from S&P DJI index announcements released on the ASX under code ZSP between Dec 2009 and Sep 2021, "
            "then the 20 Sep 2021 review was applied forward. Only events of companies that were members on the "
            "event date are included; delisted and since-removed members are included."),
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "rows": events,
        "gaps": gaps,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    with open(os.path.join(cache, "diagnostics.json"), "w") as f:
        json.dump({"issues": [dict(i, eff=str(i["eff"])) for i in issues], "diag": diag,
                   "unparsed": [(m, why) for m, why in unparsed],
                   "set_sizes": [(str(f), len(sset)) for f, sset in timeline]}, f, indent=1)
    print(f"{len(events)} rows -> {OUT}; {len(issues)} membership issues; {len(diag)} interval warnings; "
          f"{len(missing_days)} days missing", flush=True)


def build_gaps(missing_days, unparsed, issues, diag, timeline):
    gaps = []
    if missing_days:
        gaps.append({"scope": f"{len(missing_days)} weekday listings", "reason":
                     "ASX per-date listing could not be fetched: " + ", ".join(missing_days[:50])})
    gaps.append({"scope": "all rows / doc_url", "reason":
                 "Company-website copies of 2010-2021 documents were not verified, so every row links the ASX "
                 "announcement copy (doc_source=exchange). No company URLs were constructed."})
    gaps.append({"scope": "AGM dates", "reason":
                 "AGM rows are dated by the meeting-day release (results of meeting / chair or CEO address). A "
                 "bare 'Results of Meeting' or chair address is counted as the AGM only within 100 days after a "
                 "'Notice of Annual General Meeting'; AGMs announced only by notice, with no meeting-day release "
                 "found in headlines, are not included."})
    gaps.append({"scope": "event classification", "reason":
                 "Event types are assigned from ASX announcement headlines (the listing has no document-type "
                 "field for 2010-2021); documents with non-standard headlines can be missed. 'period' is set only "
                 "where the headline states it (e.g. FY15, 1H15, Q3 FY15)."})
    gaps.append({"scope": "Investor day / Trading statement", "reason":
                 "Investor/strategy days are captured only when the headline names the event; trading updates "
                 "and guidance releases were not collected."})
    for m, why in unparsed:
        gaps.append({"scope": f"S&P notice {m['date']} idsId {m['ids']} ({m['headline']})", "reason": why})
    for i in issues:
        if i["problem"].startswith("addition"):
            why = ("the S&P addition is not matched by membership in the later reconstructed set (the code later "
                   "changed, the company delisted, or an S&P removal notice was not found); the code is treated as "
                   "a non-member from this addition until its next recorded change, so rows may be missing")
        else:
            why = ("the code was already in the earlier reconstructed set (an S&P re-addition notice was not "
                   "found or the removal was re-announced); membership after this removal is treated as unknown")
        gaps.append({"scope": f"membership {i['code']} {i['act']} effective {i['eff']}",
                     "reason": why + f"; notice {i['released']} '{i['headline']}'"})
    gaps.append({"scope": "delisted / renamed index members (all event types)", "reason":
                 "The ASX public announcement search only serves announcements of issuers that are listed today, "
                 "under their current ASX code and only from the date they adopted it (e.g. AGL from Dec 2014, AZJ "
                 "from Dec 2012; nothing for WPL/Woodside now WDS, OSH, SYD, NCM, AWC, BLD, CSR, ABC, Asciano AIO, "
                 "Foster's FGL, ...). Their 2010-2021 announcements are not listed by date or by code, so events of "
                 "S&P/ASX 200 members that have since delisted or changed code were not collected. Each affected "
                 "symbol and span is listed below."})
    by_code = {}
    for d in diag:
        spans = by_code.setdefault(d["code"], [])
        if spans and _d(d["from"]) <= _d(spans[-1][1]) + dt.timedelta(days=60):
            spans[-1][1] = max(spans[-1][1], d["to"])
        else:
            spans.append([d["from"], d["to"]])
    for code, spans in sorted(by_code.items()):
        gaps.append({"scope": f"symbol {code}", "reason":
                     "reconstructed as an S&P/ASX 200 member under this code but the ASX archive has no "
                     "announcements under it for " + "; ".join(f"{a}..{b}" for a, b in spans)
                     + " (issuer since delisted or code changed, or not listed under this code then); no rows"})
    return gaps


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["sweep", "notices", "build", "all"])
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--reverse", action="store_true", help="sweep from the end of the range backwards")
    ap.add_argument("--start", type=dt.date.fromisoformat, help="sweep starting at this date (extra worker)")
    a = ap.parse_args()
    if a.stage in ("sweep", "all"):
        sweep(a.cache, a.reverse, a.start)
    if a.stage in ("notices", "all"):
        notices(a.cache)
    if a.stage in ("build", "all"):
        build(a.cache)
