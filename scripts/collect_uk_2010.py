#!/usr/bin/env python3
"""UK (FTSE 100) historical event backfill, Jan 2010 - Sep 2021.

Stages (resumable; raw responses cached in the scratchpad):
  membership  - monthly snapshots of the FTSE 100 constituent list from
                Wikipedia revision history (action=raw of the last revision
                before each month end), parsed into wiki link targets.
  events      - per company EPIC, Investegate advanced-search listing of all
                RNS announcements 2010-01-01..2021-09-30 (paginated).
  build       - classify headlines into event types, apply as-of membership,
                write data/history/history_uk_2010.json.

Run with: python3 -I scripts/collect_uk_2010.py <stage>
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time

import requests

UA = "MarketEventsTracker/1.0 historical-backfill (github.com/manoharkakdeai/claude)"
CACHE = os.environ.get(
    "UK_CACHE",
    "/tmp/claude-0/-home-user-claude/acd3cf6e-52bc-5be0-ba82-933351f9e351/scratchpad/uk",
)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "data", "history", "history_uk_2010.json")
DELAY = 1.0

S = requests.Session()
S.headers["User-Agent"] = UA
_last = [0.0]


def fetch(url, sub, params=None, headers=None, redirects=True):
    """GET with on-disk cache and throttle. Returns text or None."""
    key = url + ("?" + json.dumps(params, sort_keys=True) if params else "")
    h = hashlib.sha1(key.encode()).hexdigest()
    d = os.path.join(CACHE, sub)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, h + ".txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    for attempt in range(4):
        wait = DELAY - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = S.get(url, params=params, headers=headers, timeout=60, allow_redirects=redirects)
        except requests.RequestException as e:
            print("  net error", e, file=sys.stderr)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(15 * (attempt + 1))
            continue
        if not redirects and 300 <= r.status_code < 400:
            with open(path, "w", encoding="utf-8") as f:
                f.write("__REDIRECT__")
            return "__REDIRECT__"
        if r.status_code != 200:
            print("  HTTP", r.status_code, url, file=sys.stderr)
            return None
        with open(path, "w", encoding="utf-8") as f:
            f.write(r.text)
        return r.text
    return None


# ---------------------------------------------------------------- membership
WIKI = "https://en.wikipedia.org/w/index.php"
WIKI_PAGE = "FTSE_100_Index"


def month_ends():
    """Yield (YYYY-MM, offset timestamp = first instant of following month)."""
    y, m = 2009, 12
    while (y, m) <= (2021, 9):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        yield f"{y:04d}-{m:02d}", f"{ny:04d}{nm:02d}01000000"
        y, m = ny, nm


def snapshot(month, offset):
    hist = fetch(WIKI, "wiki", {"title": WIKI_PAGE, "action": "history", "offset": offset, "limit": "1"})
    if not hist:
        return None, None
    m = re.search(r'oldid=(\d+)', hist)
    if not m:
        return None, None
    oldid = m.group(1)
    raw = fetch(WIKI, "wiki", {"title": WIKI_PAGE, "oldid": oldid, "action": "raw"})
    return oldid, raw


def _section(raw):
    """Return text of the current-constituent list section of a revision."""
    lines = raw.splitlines()
    heads = [(i, l.strip("= ").strip()) for i, l in enumerate(lines) if re.match(r"^==[^=].*==\s*$", l)]
    def pick(pred):
        for k, (i, h) in enumerate(heads):
            if pred(h):
                end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
                return "\n".join(lines[i + 1:end])
        return None
    for pred in (
        lambda h: h in ("Current constituents", "List of constituents", "Constituents"),
        lambda h: h.startswith("Constituents in "),
        lambda h: h.startswith("Current constituents by market capitalisation"),
    ):
        s = pick(pred)
        if s:
            return s
    return None


LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")


def parse_members(raw):
    """List of (wiki_target, label, epic_or_None) for one revision."""
    sec = _section(raw)
    if not sec:
        return []
    sec = re.sub(r"<ref[^>]*/>", "", sec)
    sec = re.sub(r"<ref.*?</ref>", "", sec, flags=re.S)
    sec = re.sub(r"<!--.*?-->", "", sec, flags=re.S)
    out = []
    for line in sec.splitlines():
        if re.match(r"^\s*[#*]", line):
            m = LINK.search(line)
            if m:
                out.append((m.group(1).strip(), (m.group(2) or m.group(1)).strip(), None))
    if len(out) < 80 and "{|" in sec:
        out = []
        tbl = sec[sec.index("{|"):]
        for row in re.split(r"\n\|-[^\n]*", tbl)[1:]:
            cells = [c.strip() for c in re.split(r"\n\||\|\|", "\n" + row.strip()) if c.strip()]
            cells = [c for c in cells if not c.startswith("!") and not c.startswith("|}")]
            if not cells:
                continue
            m = LINK.search(cells[0])
            if not m:
                continue
            epic = None
            if len(cells) > 1:
                e = re.sub(r"\{\{[^}|]*\|([^}|]*)[^}]*\}\}", r"\1", cells[1])
                e = re.sub(r"\[\[[^\]|]*\|?([^\]]*)\]\]", r"\1", e)
                e = re.sub(r"<[^>]+>|'''?", "", e).strip()
                if re.fullmatch(r"[A-Z0-9]{1,5}\.?[A-Z]?", e):
                    epic = e
            out.append((m.group(1).strip(), (m.group(2) or m.group(1)).strip(), epic))
    # de-dup preserving order
    seen, res = set(), []
    for t in out:
        k = t[0].replace("_", " ")
        if k not in seen:
            seen.add(k)
            res.append((k, t[1], t[2]))
    return res


# ------------------------------------------------------------- company map
# Wikipedia link target (as found in the constituent lists) -> EPIC/TIDM under
# which Investegate files the company's announcements today. Every EPIC is
# checked against Investegate's company page in the `verify` stage; targets
# whose EPIC no longer resolves (delisted/acquired companies) become gaps.
EPIC = {
    "3i": "III", "AMEC": "AMFW", "ARM Holdings": "ARM", "Aberdeen Asset Management": "ADN",
    "Abrdn": "ABDN", "Standard Life": "ABDN", "Standard Life Aberdeen": "ABDN",
    "Admiral Group": "ADM", "African Barrick Gold": "ACA", "Aggreko": "AGK", "Alliance Trust": "ALW",
    "Anglo American PLC": "AAL", "Anglo American plc": "AAL", "Antofagasta PLC": "ANTO",
    "Antofagasta plc": "ANTO", "Ashmore Group": "ASHM", "Ashtead Group": "AHT",
    "Associated British Foods": "ABF", "AstraZeneca": "AZN", "Auto Trader Group": "AUTO",
    "Autonomy Corporation": "AU.", "Avast": "AVST", "Aveva": "AVV", "Aviva": "AV.", "B&M": "BME",
    "BAE Systems": "BA.", "BG Group": "BG.", "BHP": "BHP", "BHP Billiton": "BHP", "BP": "BP.",
    "BT Group": "BT.A", "Babcock International": "BAB", "Babcock International Group": "BAB",
    "Barclays": "BARC", "Barratt Developments": "BTRW", "Berkeley Group Holdings": "BKG",
    "British Airways": "BAY", "British American Tobacco": "BATS", "British Land": "BLND",
    "British Sky Broadcasting": "SKY", "Sky plc": "SKY", "Bunzl": "BNZL", "Burberry": "BRBY",
    "Burberry Group": "BRBY", "CRH plc": "CRH", "Cable & Wireless": "CW.",
    "Cable & Wireless Worldwide": "CW.", "Cadbury plc": "CBRY", "Cairn Energy": "CNE",
    "Capita": "CPI", "Capita Group": "CPI", "Liberty International": "INTU",
    "Capital Shopping Centres": "INTU", "Intu Properties": "INTU",
    "Carnival Corporation & plc": "CCL", "Centrica": "CNA", "Cobham plc": "COB",
    "Coca-Cola HBC AG": "CCH", "Coca-Cola Hellenic": "CCH", "Coca-Cola Hellenic Bottling Company": "CCH",
    "Compass Group": "CPG", "ConvaTec": "CTEC", "Croda International": "CRDA", "DCC plc": "DCC",
    "DS Smith": "SMDS", "Diageo": "DGE", "Direct Line Group": "DLG", "Dixons Carphone": "CURY",
    "EasyJet": "EZJ", "easyJet": "EZJ", "Entain": "ENT", "GVC Holdings": "ENT", "Essar Energy": "ESSR",
    "Eurasian Natural Resources Corporation": "ENRC", "Evraz": "EVR", "Evraz Group": "EVR",
    "Experian": "EXPN", "Ferguson plc": "FERG", "Wolseley plc": "FERG",
    "Wolseley (builders' merchant)": "FERG", "Flutter Entertainment": "FLTR",
    "Paddy Power Betfair": "FLTR", "Fresnillo plc": "FRES", "Friends Life Group": "FLG",
    "G4S": "GFS", "GKN": "GKN", "GlaxoSmithKline": "GSK", "Glencore": "GLEN",
    "Glencore International": "GLEN", "Glencore Xstrata": "GLEN", "Xstrata": "XTA", "HSBC": "HSBA",
    "Halma plc": "HLMA", "Hammerson": "HMSO", "Hargreaves Lansdown": "HL.",
    "Hikma Pharmaceuticals": "HIK", "Hiscox": "HSX", "Home Retail Group": "HOME", "Homeserve": "HSV",
    "ICAP (company)": "IAP", "IHG Hotels & Resorts": "IHG", "InterContinental Hotels Group": "IHG",
    "IMI plc": "IMI", "ITV plc": "ITV", "Imperial Brands": "IMB", "Imperial Tobacco": "IMB",
    "Informa": "INF", "Inmarsat": "ISAT", "Intermediate Capital Group": "ICG",
    "International Airlines Group": "IAG", "International Consolidated Airlines Group": "IAG",
    "International Consolidated Airlines Group SA": "IAG", "International Power": "IPR",
    "Intertek": "ITRK", "Intertek Group": "ITRK", "Invensys": "ISYS", "Investec Bank": "INVP",
    "J Sainsbury plc": "SBRY", "Sainsbury's": "SBRY", "JD Sports": "JD.", "John Wood Group": "WG.",
    "Wood Group": "WG.", "Johnson Matthey": "JMAT", "Just Eat": "JE.", "Just Eat Takeaway": "JET",
    "Takeaway.com": "JET", "Kazakhmys": "KAZ", "Kingfisher plc": "KGF", "Land Securities": "LAND",
    "Land Securities Group": "LAND", "Landsec": "LAND", "Legal & General": "LGEN",
    "Lloyds Banking Group": "LLOY", "London Stock Exchange Group": "LSEG", "Lonmin": "LMI",
    "M&G": "MNG", "Man Group": "EMG", "Marks & Spencer": "MKS", "Marks & Spencer Group": "MKS",
    "Mediclinic International": "MDC", "Meggitt": "MGGT", "Meggitt PLC": "MGGT",
    "Melrose Industries": "MRO", "Melrose plc": "MRO", "Merlin Entertainments": "MERL",
    "Micro Focus": "MCRO", "Micro Focus International": "MCRO", "Mondi": "MNDI", "Mondi Group": "MNDI",
    "Morrisons": "MRW", "NMC Health": "NMC", "NatWest Group": "NWG",
    "Royal Bank of Scotland Group": "NWG", "The Royal Bank of Scotland Group": "NWG",
    "National Grid plc": "NG.", "Next (clothing)": "NXT", "Next plc": "NXT",
    "OJSC Polymetal": "POLY", "Polymetal International": "POLY", "Ocado": "OCDO", "Ocado Group": "OCDO",
    "Old Mutual": "OML", "Pearson PLC": "PSON", "Pearson plc": "PSON", "Pennon Group": "PNN",
    "Pershing Square Holdings": "PSH", "Persimmon plc": "PSN", "Petrofac": "PFC", "Phoenix Group": "SDLF",
    "Provident Financial": "VANQ", "Prudential plc": "PRU", "RELX": "REL", "RELX Group": "REL",
    "Reed Elsevier": "REL", "RSA Insurance Group": "RSA", "Randgold Resources": "RRS",
    "Reckitt": "RKT", "Reckitt Benckiser": "RKT", "Renishaw plc": "RSW", "Rentokil Initial": "RTO",
    "Resolution Limited": "RSL", "Rexam": "REX", "Rightmove": "RMV", "Rio Tinto (corporation)": "RIO",
    "Rio Tinto Group": "RIO", "Rolls-Royce Holdings": "RR.", "Rolls-Royce plc": "RR.",
    "Royal Dutch Shell": "SHEL", "Royal Mail": "IDS", "SABMiller": "SAB", "SEGRO": "SGRO", "Segro": "SGRO",
    "Sergo": "SGRO", "SSE plc": "SSE", "Scottish and Southern Energy": "SSE", "Sage Group": "SGE",
    "The Sage Group": "SGE", "Schroders": "SDR", "Scottish Mortgage Investment Trust": "SMT",
    "Serco Group": "SRP", "Severn Trent": "SVT", "Shire (pharmaceutical company)": "SHP",
    "Shire plc": "SHP", "Smith & Nephew": "SN.", "Smiths Group": "SMIN", "Smurfit Kappa": "SKG",
    "Smurfit Kappa Group": "SKG", "Spirax-Sarco Engineering": "SPX", "Sports Direct": "FRAS",
    "St. James's Place plc": "STJ", "Standard Chartered": "STAN", "Standard Chartered Bank": "STAN",
    "TUI AG": "TUI", "TUI Group": "TUI", "TUI Travel": "TT.", "Tate & Lyle": "TATE",
    "Taylor Wimpey": "TW.", "Tesco": "TSCO", "Thomas Cook Group": "TCG", "Travis Perkins": "TPK",
    "Tullow Oil": "TLW", "Unilever": "ULVR", "United Utilities": "UU.", "Vedanta Resources": "VED",
    "Vodafone": "VOD", "WPP Group": "WPP", "WPP plc": "WPP", "Weir Group": "WEIR",
    "Whitbread": "WTB", "William Hill (bookmaker)": "WMH", "Worldpay": "WPG",
}


# EPICs that Investegate has re-used for an unrelated company today (checked
# against the company name on the Investegate page): treated as unresolved.
REUSED = {"ADN": "Andina plc", "BAY": "Bay Capital", "HOME": "Home Reit"}

# ------------------------------------------------------------------ verify
IG = "https://www.investegate.co.uk"


def verify():
    """Check each EPIC resolves to an Investegate company page; return {epic: name|None}."""
    out = {}
    for epic in sorted(set(EPIC.values())):
        h = fetch(f"{IG}/company/{epic}", "ig_company", redirects=False)
        name = None
        if h and h != "__REDIRECT__":
            m = re.search(r"<title>\s*(.*?) RNS Announcements \|", h, re.S)
            name = m.group(1).strip() if m else None
        if epic in REUSED:
            name = None
        out[epic] = name
        print(epic, name, file=sys.stderr)
    return out


# --------------------------------------------------------------- membership
def membership():
    """Return (months, {epic: set(months)}, {(target, month): wiki_epic}, {target: [wiki_epics]}, snaps)."""
    months, mem, wepic, tepic, snaps = [], {}, {}, {}, {}
    for mo, off in month_ends():
        oid, raw = snapshot(mo, off)
        if not raw:
            continue
        members = parse_members(raw)
        if len(members) < 95:
            print("  suspicious snapshot", mo, len(members), file=sys.stderr)
            continue
        months.append(mo)
        snaps[mo] = oid
        for target, label, we in members:
            epic = EPIC[target]
            mem.setdefault(epic, set()).add(mo)
            if we:
                wepic[(target, mo)] = we
                tepic.setdefault(target, []).append((mo, we))
            mem.setdefault(("T", epic), set()).add(target)
    return months, mem, wepic, tepic, snaps


def shift(mo, k):
    y, m = int(mo[:4]), int(mo[5:7]) + k
    while m > 12:
        y, m = y + 1, m - 12
    while m < 1:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


def review_effective(y, m):
    """Monday after the third Friday of a review month (FTSE quarterly review)."""
    d = dt.date(y, m, 1)
    fridays = [d + dt.timedelta(days=i) for i in range(31)
               if (d + dt.timedelta(days=i)).month == m and (d + dt.timedelta(days=i)).weekday() == 4]
    return fridays[2] + dt.timedelta(days=3)


def as_of(epic_months, date):
    """'member' / 'not' / 'ambiguous' for an event date given month-end snapshots."""
    mo = date[:7]
    prev, cur = shift(mo, -1) in epic_months, mo in epic_months
    if prev and cur:
        return "member"
    if not prev and not cur:
        return "not"
    y, m = int(mo[:4]), int(mo[5:7])
    if m in (3, 6, 9, 12):
        eff = review_effective(y, m).isoformat()
        if prev and date < eff:
            return "member"
        if cur and date >= eff:
            return "member"
        return "not"
    return "ambiguous"


# ------------------------------------------------------------------ events
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ROW = re.compile(
    r"<tr>\s*<td>\s*(\d\d) (\w\w\w) (\d{4}) (\d\d):(\d\d) (AM|PM)\s*</td>.*?"
    r'href="https://www\.investegate\.co\.uk/company/([^"]+)">([^<]*)</a>.*?'
    r'<td>\s*<a href="(https://www\.investegate\.co\.uk/announcement/[^"]+)">([^<]*)</a>', re.S)


def fmt_date(d):
    return f"{d.day} {dt.date(2000, d.month, 1).strftime('%B')} {d.year}"


def listing(name, epic, d0, d1, mode="1"):
    """All announcements of `epic` between d0 and d1 (dates), via company-name
    search (mode 1) or, when the site rejects the name (HTTP 422 for names with
    punctuation), EPIC prefix search (mode 2). Rows are filtered to `epic`."""
    out, page, total = [], 1, None
    word = name if mode == "1" else epic.split(".")[0]
    while True:
        params = {"search_for": mode, "search_word": word, "date_from": fmt_date(d0),
                  "date_to": fmt_date(d1), "exclude_navs": "true", "page": str(page)}
        h = fetch(IG + "/advanced-search/draw", "ig_search", params,
                  headers={"X-Requested-With": "XMLHttpRequest"})
        if h is None:
            if page == 1 and mode == "1":
                return listing(name, epic, d0, d1, mode="2")
            return out, False
        rows = ROW.findall(h)
        for dd, mon, yy, hh, mi, ap, ep, cname, url, head in rows:
            if ep != epic:
                continue
            hh = int(hh) % 12 + (12 if ap == "PM" else 0)
            date = f"{yy}-{MONTHS.index(mon) + 1:02d}-{dd}"
            out.append({"date": date, "time": f"{hh:02d}:{mi}", "epic": ep,
                        "company": html_unescape(cname), "url": url, "headline": html_unescape(head)})
        m = re.search(r"of\s*(?:<[^>]+>\s*)*([\d,]+)\s*(?:<[^>]+>\s*)*results", h)
        if m:
            total = int(m.group(1).replace(",", ""))
        if not rows or total is None or page * 100 >= total:
            return out, True
        page += 1


def html_unescape(s):
    import html
    return html.unescape(html.unescape(s)).replace("\u00c2\u00a0", " ").replace("\u00a0", " ").strip()


def events():
    months, mem, wepic, tepic, snaps = membership()
    names = verify()
    res, failures = {}, []
    path = os.path.join(CACHE, "events_raw.json")
    for epic in sorted(k for k in mem if isinstance(k, str)):
        name = names.get(epic)
        if not name:
            continue
        ms = sorted(mem[epic])
        d0 = max(dt.date(2010, 1, 1), dt.date.fromisoformat(shift(ms[0], -1) + "-01"))
        last = shift(ms[-1], 2) + "-01"
        d1 = min(dt.date(2021, 9, 30), dt.date.fromisoformat(last) - dt.timedelta(days=1))
        print(epic, name, d0, d1, file=sys.stderr)
        rows, ok = listing(html_unescape(name), epic, d0, d1)
        if not ok:
            failures.append(epic)
        res[epic] = {"name": html_unescape(name), "from": d0.isoformat(), "to": d1.isoformat(),
                     "complete": ok, "rows": rows}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res, f)
    return res


# ------------------------------------------------------------------- build
NOT_EVENT = re.compile(
    r"notice of (results|agm|annual general|interim|final|prelim|half)|results? date|date of (results|agm)|"
    r"change of (results|agm)|of results date|results? (announcement )?timing|timing of|"
    r"result of (offer|tender|placing|rights|scheme|court|general meeting|gm|egm|bond|ballot|exchange|consent|bookbuild|meeting of)|"
    r"results? of (offer|tender|placing|rights|scheme|court|general meeting|gm|egm|bond|ballot|exchange|consent|bookbuild|the general)|"
    r"conference call|webcast|audio|dial[- ]in|presentation (details|arrangements|timing)|"
    r"\bdoc re\b.*(proxy|circular)|form of proxy|posting of|availability of|publication of (notice|circular|prospectus|proxy)|"
    r"notice of (meeting|general)|replacement|correction|amendment|addendum|cancellation|withdrawn|"
    r"director(ate)?\b|pdmr|dividend|holding\(s\)|holdings? in |voting rights|block ?listing|own shares|price monitoring", re.I)
AGM = re.compile(r"\bagm\b|annual general meeting|\bagms\b|annual meeting|result of a\.?g\.?m|ag\s?m statement|general meeting.*annual", re.I)
AGM_NOT = re.compile(r"information|arrangement|notice|posting|proxy|circular|doc re|documents?|convening|availability|publication|date|resolutions? passed at.*(egm|general meeting\b(?!.*annual))", re.I)
ANNUAL = re.compile(r"annual (financial )?report|annual review|report (and|&) accounts|annual report.*20-f|form 20-f|10-k", re.I)
ANNUAL_NOT = re.compile(r"subsidiary|^filing|\bfiles?\b|filed|\bsec\b|half|interim|sustainab|responsib|corporate responsibility|remuneration|esg|gender|payments to gov|slavery|tax|climate|\bmodern", re.I)
INVESTOR = re.compile(r"capital markets? (day|event|update|seminar|presentation)|investor (day|seminar|event|update day|briefing)|strategy (day|update|presentation|review day)|analyst (day|seminar|event|visit)|investor and analyst", re.I)
TRADING = re.compile(r"interim management|flows update|trading (statement|update|and|in |& |performance)|interim management statement|\bims\b|pre[- ]close|business update|sales update|q[1-4] (trading|update|ims|interim management)|(first|third) quarter (trading|update|ims|interim management)|christmas trading|trading$|trading update", re.I)
RESULTS = re.compile(
    r"\bresults?\b|half[- ]?year(ly)? (report|financial report|statement)|interim (report|statement|financial report)|"
    r"(first|second|third|fourth) quarter|\bq[1-4]\b.*(report|result|earn)|preliminary (statement|announcement)|"
    r"prelim(inary)?s?\b|full[- ]year|annual results|earnings|"
    r"^[1-4]q\s*'?\d\d\b|half[- ]?year(ly)?\s+(re|fin|stat)|^(hy|fy|h1|1h|h2)\s*'?\s*(20)?\d\d\b(?!.*(dividend|scrip|ims|trading))", re.I)
RESULTS_NOT = re.compile(r"production|operational review|operating (review|statement)|sales$|exploration|drilling|reserves|"
                         r"trial|study|phase|clinical|data|traffic|passenger|ratings|rating|stress test|election|vote|poll|pillar 3|"
                         r"capital (raising|return)|offer|acquisition|disposal|merger|tender|scheme|noteholder|bondholder|pricing|proposals|"
                         r"referendum|notice|\bdoc re|reconciliation|notification|kumba|de beers|anglo (american )?platinum|"
                         r"\bwell\b|thungela|meeting|change in time|statement re|top[- ]line|pipeline|\beba\b|cebs|horizon|presentation|webcast", re.I)
SUBSIDIARY = re.compile(r"kumba|de beers|anglo (american )?platinum|thungela|reconciliation|notification", re.I)


def classify(head):
    h = head.strip()
    if SUBSIDIARY.search(h) or re.search(r"postpone|cancel|statement re\b|update statement|update on .*(agm|annual general)", h, re.I):
        return None
    if INVESTOR.search(h) and not re.search(r"notice|date|timing|webcast|announcement$|details|^circ", h, re.I):
        return "Investor day"
    if AGM.search(h):
        if ANNUAL.search(h) and not ANNUAL_NOT.search(h):
            return "Annual report"
        if AGM_NOT.search(h) and not re.search(r"result|statement|trading|update|ims|interim management", h, re.I):
            return None
        return "AGM"
    if NOT_EVENT.search(h):
        return None
    if ANNUAL.search(h) and not ANNUAL_NOT.search(h):
        return "Annual report"
    if TRADING.search(h):
        return "Trading statement"
    if RESULTS.search(h) and not RESULTS_NOT.search(h):
        return "Results"
    return None


def period_of(head, date):
    """Fiscal period from the headline, only when the headline names the year."""
    h = head.lower()
    y = re.search(r"\b(20\d\d)(?:/(\d\d))?\b", h)
    if not y:
        m = (re.search(r"\bq[1-4]\s*(?:fy)?\s*'?\s*(\d\d)\b", h) or re.search(r"\b[1-4]q\s*'?(\d\d)\b", h)
             or re.search(r"\b(?:hy|fy|h1|1h|h2)\s*'?\s*(\d\d)\b", h))
        yr = "20" + m.group(1) if m else None
    else:
        yr = y.group(0)
    if not yr:
        return None
    m = re.search(r"\bq([1-4])\b", h) or re.search(r"\b([1-4])q\s*'?\d\d\b", h) or re.search(r"\b(first|second|third|fourth) quarter", h)
    if m:
        q = {"first": "1", "second": "2", "third": "3", "fourth": "4"}.get(m.group(1), m.group(1))
        return f"Q{q} {yr}"
    if re.search(r"\b(half[- ]?year(?:ly)?|interim|h1|1h|hy|six months)\b|\b(hy|h1|1h)\s*'?\d", h) and "management statement" not in h:
        return f"H1 {yr}"
    if re.search(r"\b(final|full[- ]year|preliminary|prelim|annual|year[- ]end|fy)\b|\bfy\s*'?\d", h):
        return f"FY {yr}"
    return None


def build():
    months, mem, wepic, tepic, snaps = membership()
    with open(os.path.join(CACHE, "events_raw.json"), encoding="utf-8") as f:
        raw = json.load(f)
    names = verify()
    rows, gaps, seen = [], [], set()
    ambiguous = []
    for epic, blob in sorted(raw.items()):
        ems = mem[epic]
        targets = sorted(mem[("T", epic)])
        for a in sorted(blob["rows"], key=lambda r: (r["date"], r["time"])):
            a["headline"], a["company"] = html_unescape(a["headline"]), html_unescape(a["company"])
            typ = classify(a["headline"])
            if not typ:
                continue
            if not ("2010-01-01" <= a["date"] <= "2021-09-30"):
                continue
            st = as_of(ems, a["date"])
            if st == "not":
                continue
            # symbol: Wikipedia's EPIC for this company in that month if listed,
            # else the nearest EPIC Wikipedia gives for the company, else Investegate's.
            mo = a["date"][:7]
            sym = None
            cands = [(t, wepic.get((t, mo)) or wepic.get((t, shift(mo, -1)))) for t in targets]
            cands = [c for c in cands if c[1]]
            if cands:
                sym = cands[0][1]
            else:
                near = []
                for t in targets:
                    if any(m2 in ems for m2 in (mo, shift(mo, -1))) and tepic.get(t):
                        near += [(abs(int(m2[:4]) * 12 + int(m2[5:]) - int(mo[:4]) * 12 - int(mo[5:])), e)
                                 for m2, e in tepic[t]]
                if near:
                    sym = sorted(near)[0][1]
            if not sym or sym.rstrip(".") == epic.rstrip("."):
                sym = epic
            company = re.sub(r"\s*\([^()]*\)\s*$", "", a["company"]).strip()
            per = period_of(a["headline"], a["date"]) if typ in ("Results", "Trading statement", "Annual report") else None
            key = ("UK", sym, typ, a["date"], per or "")
            if key in seen:
                continue
            seen.add(key)
            row = {"date": a["date"], "time": a["time"], "market": "UK", "symbol": sym, "company": company,
                   "type": typ}
            if st == "member":
                row["index"] = "FTSE 100"
            else:
                ambiguous.append(f"{sym} {a['date']}")
            if per:
                row["period"] = per
            row["_epic"] = epic
            row.update({"title": re.sub(r"\s*-?\s*\(?part \w+ of \w+\)?$", "", a["headline"], flags=re.I),
                        "doc_url": a["url"], "doc_source": "exchange",
                        "source": "Investegate (RNS archive)"})
            rows.append(row)
    rows.sort(key=lambda r: (r["date"], r.get("time", ""), r["symbol"], r["type"]))
    # one annual report per company per publication season: drop follow-up
    # "Doc re. annual report" style rows within 60 days of the first one
    kept, lastar = [], {}
    for r in rows:
        if r["type"] == "Annual report":
            prev = lastar.get(r["_epic"])
            if prev and (dt.date.fromisoformat(r["date"]) - dt.date.fromisoformat(prev)).days <= 60:
                continue
            lastar[r["_epic"]] = r["date"]
        kept.append(r)
    rows = sorted(kept, key=lambda r: (r["date"], r["symbol"], r["type"]))
    return rows, ambiguous, raw, names, mem, snaps


def write():
    rows, ambiguous, raw, names, mem, snaps = build()
    gaps = []
    for epic in sorted(k for k in mem if isinstance(k, str)):
        ms = sorted(mem[epic])
        targets = ", ".join(sorted(mem[("T", epic)]))
        if not names.get(epic):
            why = (f"EPIC {epic} now belongs to an unrelated company on Investegate ({REUSED[epic]})"
                   if epic in REUSED else
                   "company delisted/acquired since; Investegate no longer resolves its EPIC, so its RNS history is not listed")
            gaps.append({"scope": f"{targets} [{epic}] FTSE 100 member {ms[0]}..{ms[-1]}: all event types",
                         "reason": why + "; LSE RNS listing API refuses automated reads (HTTP 403); company IR archive not collected"})
        elif epic in raw and raw[epic]["rows"] and epic != "SHEL":
            ds = sorted(a["date"] for a in raw[epic]["rows"])
            if ds[0][:7] > shift(raw[epic]["from"][:7], 3):
                gaps.append({"scope": f"{targets} [{epic}] {raw[epic]['from']}..{ds[0]}: all event types",
                             "reason": f"Investegate lists this company's RNS history under {epic} only from {ds[0]}; "
                                       "earlier announcements (filed under a former name/listing line) are not listed; "
                                       "LSE RNS listing refuses automated reads"})
        if epic in raw and not raw[epic]["complete"]:
            gaps.append({"scope": f"{targets} [{epic}]: all event types",
                         "reason": "Investegate listing could not be fully paged (fetch error); rows are partial"})
    # member-years (full calendar year in the index) without a Results / AGM / Annual report row
    found = {}
    for r in rows:
        found.setdefault((r["_epic"], r["date"][:4]), set()).add(r["type"])
    missing = {}
    for epic in sorted(k for k in mem if isinstance(k, str)):
        if not names.get(epic):
            continue
        for y in range(2010, 2022):
            last = 9 if y == 2021 else 12
            if all(f"{y}-{m:02d}" in mem[epic] for m in range(1, last + 1)):
                for t in ("Results", "AGM", "Annual report"):
                    if t not in found.get((epic, str(y)), set()):
                        missing.setdefault(t, []).append(f"{epic} {y}")
    for t, lst in missing.items():
        gaps.append({"scope": f"{t}: {len(lst)} company-years with full-year FTSE 100 membership: " + ", ".join(lst),
                     "reason": f"no RNS headline on Investegate for that company/year was recognised as {t} "
                               "(headline not identifiable by keyword, published under another name, or not announced via RNS)"})
    if ambiguous:
        gaps.append({"scope": f"index field omitted on {len(ambiguous)} rows: " + ", ".join(ambiguous),
                     "reason": "company entered/left the FTSE 100 intra-quarter in that month (month-end snapshots only); "
                               "membership on the event date could not be established"})
    shel = [r for r in rows if r["_epic"] == "SHEL"]
    gaps.append({"scope": f"Royal Dutch Shell [RDSA/RDSB] 2010-01..2021-09 ({len(shel)} rows found)",
                 "reason": "Investegate files Shell under its 2022 EPIC SHEL and the RDSA/RDSB-era announcements "
                           "(2005-Jan 2022) are not listed there; LSE RNS listing refuses automated reads"})
    gaps += [
        {"scope": "UK all rows: company-hosted document links",
         "reason": "company IR result-centre URLs were not collected/verified for ~170 companies; doc_url is the Investegate copy "
                   "of the RNS announcement (exchange regulatory news)"},
        {"scope": "UK all rows: index membership source",
         "reason": "FTSE Russell's index-notice listing returns HTTP 403 to automated reads, so membership is taken from month-end "
                   "revisions of Wikipedia's FTSE 100 constituent list (secondary source; may lag reviews by days); "
                   "quarterly-review months use the Monday after the third Friday as the effective date"},
        {"scope": "UK Investor day / Trading statement",
         "reason": "only events announced through an RNS headline are captured; capital markets days announced only on company "
                   "websites are missing. Pre-2013 RNS headlines are truncated to ~30 characters, so some are unclassifiable"},
    ]
    for r in rows:
        r.pop("_epic", None)
    doc = {"market": "UK", "range": {"from": "2010-01", "to": "2021-09"},
           "sources": [
               "Investegate advanced search / RNS archive (https://www.investegate.co.uk/advanced-search) - event listings and doc_url",
               "Wikipedia 'FTSE 100 Index' revision history, month-end revisions Dec 2009 - Sep 2021 (constituent lists, EPICs) - "
               "membership as of event date; revision ids: " + ", ".join(f"{k}:{v}" for k, v in sorted(snaps.items())),
           ],
           "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           "rows": rows, "gaps": gaps}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"wrote {len(rows)} rows, {len(gaps)} gaps -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage in ("membership", "all"):
        m = membership()
        print(f"{len(m[0])} month-end snapshots, {sum(1 for k in m[1] if isinstance(k, str))} EPICs", file=sys.stderr)
    if stage in ("verify", "all"):
        verify()
    if stage in ("events", "all"):
        events()
    if stage in ("build", "all"):
        write()
