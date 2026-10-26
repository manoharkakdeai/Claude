"""Collect India (Nifty 50 / Sensex) corporate events, Jan 2010 - Sep 2021.

Sources (all fetched, nothing inferred):
  * Membership: current official Nifty 50 list (archives.nseindia.com
    ind_nifty50list.csv) rolled back through the Nifty 50 replacement table on
    Wikipedia ("Index changes", which cites NSE Indices' IndexInclExcl.xls and
    the NSE/IISL press releases). niftyindices.com, nseindia.com and the
    Wayback copy of IndexInclExcl.xls were not reachable from this network.
  * Events: Screener company page "Documents" section
      - Annual reports  -> type "Annual report"; date = Last-Modified of the
        exchange-hosted file (BSE bseplus / NSE archives upload date).
      - Concalls        -> type "Earnings call"; Screener gives month only.
Raw responses are cached in the scratchpad (resumable); requests are throttled.

Usage: python3 -I scripts/collect_india_2010.py [--out data/history/history_india_2010.json]
"""
import argparse
import datetime as dt
import email.utils
import hashlib
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

UA = "MarketEventsTracker/1.0 historical-backfill (github.com/manoharkakdeai/claude)"
CACHE = os.environ.get(
    "INDIA_CACHE",
    "/tmp/claude-0/-home-user-claude/acd3cf6e-52bc-5be0-ba82-933351f9e351/scratchpad/india/raw",
)
MARKET = "India"
START, END = dt.date(2010, 1, 1), dt.date(2021, 9, 30)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

NIFTY_CSV = "https://archives.nseindia.com/content/indices/ind_nifty50list.csv"
NIFTY_WIKI = "https://en.wikipedia.org/wiki/NIFTY_50"
SCREENER = "https://www.screener.in/company/{}/"

# Names used in the Wikipedia replacement table -> NSE symbol (as used on Screener).
NAME2SYM = {
    "Kotak Mahindra Bank": "KOTAKBANK", "Grasim Industries": "GRASIM", "ABB India": "ABB",
    "Bajaj Auto": "BAJAJ-AUTO", "Idea Cellular": "IDEA", "Dr. Reddy's Laboratories": "DRREDDY",
    "Unitech": "UNITECH", "Sesa Goa": "VEDL", "Vedanta": "VEDL", "Suzlon": "SUZLON",
    "Reliance Capital": "RELCAPITAL", "Coal India": "COALINDIA",
    "Reliance Communications": "RCOM", "Asian Paints": "ASIANPAINT", "Reliance Power": "RPOWER",
    "Bank of Baroda": "BANKBARODA", "Steel Authority of India": "SAIL", "Lupin": "LUPIN",
    "Sterlite Industries": "STER", "UltraTech Cement": "ULTRACEMCO", "Siemens India": "SIEMENS",
    "IndusInd Bank": "INDUSINDBK", "Wipro": "WIPRO", "NMDC": "NMDC",
    "Reliance Infrastructure": "RELINFRA", "JP Associates": "JPASSOCIAT",
    "Jaiprakash Associates": "JPASSOCIAT", "Tech Mahindra": "TECHM",
    "Ranbaxy Laboratories": "RANBAXY", "United Spirits": "MCDOWELL-N",
    "Zee Entertainment Enterprises": "ZEEL", "DLF": "DLF", "Jindal Steel & Power": "JINDALSTEL",
    "Yes Bank": "YESBANK", "IDFC": "IDFC", "Bosch India": "BOSCHLTD",
    "Adani Ports & SEZ": "ADANIPORTS", "Cairn India": "CAIRN", "Aurobindo Pharma": "AUROPHARMA",
    "Punjab National Bank": "PNB", "Bharti Infratel": "INFRATEL", "Eicher Motors": "EICHERMOT",
    "BHEL": "BHEL", "Indiabulls Housing Finance": "IBULHSGFIN", "Indian Oil Corporation": "IOC",
    "ACC": "ACC", "Bajaj Finance": "BAJFINANCE", "Hindustan Petroleum": "HINDPETRO",
    "Tata Power": "TATAPOWER", "UPL": "UPL", "Ambuja Cements": "AMBUJACEM",
    "Bajaj Finserv": "BAJAJFINSV", "Titan Company": "TITAN", "JSW Steel": "JSWSTEEL",
    "Britannia Industries": "BRITANNIA", "Nestlé India": "NESTLEIND", "Shree Cement": "SHREECEM",
    "HDFC Life": "HDFCLIFE", "SBI Life Insurance Company": "SBILIFE",
    "Divi's Laboratories": "DIVISLAB", "GAIL": "GAIL", "Tata Consumer Products": "TATACONSUM",
    "Apollo Hospitals": "APOLLOHOSP", "Adani Enterprises": "ADANIENT", "HDFC": "HDFC",
    "LTIMindtree": "LTIM", "Shriram Finance": "SHRIRAMFIN", "Bharat Electronics": "BEL",
    "Trent": "TRENT", "Bharat Petroleum": "BPCL", "Jio Financial Services": "JIOFIN",
    "Eternal": "ETERNAL", "Hero MotoCorp": "HEROMOTOCO", "IndiGo": "INDIGO",
    "Max Healthcare": "MAXHEALTH", "BSE": "BSE",
}
# Same listed entity, symbol changed after the backfill window.
SYMBOL_ALIAS = {"TMPV": "TATAMOTORS"}
# Historical NSE symbol -> current symbol Screener files the company under (renamed after the window).
SCREENER_SYMBOL = {"TATAMOTORS": "TMPV", "IBULHSGFIN": "SAMMAANCAP", "INFRATEL": "INDUSTOWER", "MCDOWELL-N": "UNITDSPR"}

_last = [0.0]
session = requests.Session()
session.headers["User-Agent"] = UA


def _cache_path(key):
    return os.path.join(CACHE, "http", hashlib.sha1(key.encode()).hexdigest())


def fetch(url, head=False, allow_redirects=True):
    """GET/HEAD with on-disk cache. Returns dict(status, headers, text, url)."""
    key = ("HEAD " if head else "GET ") + url + ("" if allow_redirects else " noredir")
    p = _cache_path(key)
    if os.path.exists(p + ".json"):
        with open(p + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        if not head and os.path.exists(p + ".body"):
            with open(p + ".body", encoding="utf-8", errors="replace") as f:
                meta["text"] = f.read()
        return meta
    wait = 1.0 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    meta = {"status": 0, "headers": {}, "url": url, "text": ""}
    for attempt in range(3):
        try:
            if head:
                r = session.head(url, timeout=45, allow_redirects=allow_redirects)
            else:
                r = session.get(url, timeout=60, allow_redirects=allow_redirects)
            meta = {"status": r.status_code, "headers": dict(r.headers), "url": r.url, "text": "" if head else r.text}
            break
        except requests.RequestException as e:
            meta["error"] = str(e)
            time.sleep(3 * (attempt + 1))
    _last[0] = time.time()
    if meta["status"] in (200, 301, 302, 404):  # cache definitive answers only
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".json", "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in meta.items() if k != "text"} | {"fetched": url}, f)
        if not head:
            with open(p + ".body", "w", encoding="utf-8") as f:
                f.write(meta["text"])
    return meta


# ---------------------------------------------------------------- membership
def parse_wiki_changes(html):
    """Return [(date, excluded_name, included_name)] from the Nifty replacement table."""
    soup = BeautifulSoup(html, "html.parser")
    table = None
    for t in soup.find_all("table"):
        head = " ".join(th.get_text(" ", strip=True) for th in t.find_all("th"))
        if "Constituent excluded" in head and "Date of replacement" in head:
            table = t
            break
    if table is None:
        raise RuntimeError("Nifty replacement table not found")
    grid, spans = [], {}
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not tr.find("td"):
            continue
        row, ci, it = [], 0, iter(cells)
        while True:
            if ci in spans and spans[ci][0] > 0:
                row.append(spans[ci][1])
                spans[ci][0] -= 1
                ci += 1
                continue
            c = next(it, None)
            if c is None:
                break
            for sup in c.find_all("sup"):
                sup.decompose()
            txt = c.get_text(" ", strip=True)
            rs = int(c.get("rowspan", 1) or 1)
            if rs > 1:
                spans[ci] = [rs - 1, txt]
            row.append(txt)
            ci += 1
        while ci in spans and spans[ci][0] > 0:
            row.append(spans[ci][1]); spans[ci][0] -= 1; ci += 1
        grid.append(row)
    out = []
    for r in grid:
        if len(r) < 3:
            continue
        d = dt.datetime.strptime(r[2], "%d %B %Y").date()
        out.append((d, r[0], r[1]))
    return out


def build_membership():
    csv = fetch(NIFTY_CSV)
    if csv["status"] != 200:
        raise RuntimeError("Nifty CSV unavailable")
    current = set()
    for line in csv["text"].splitlines()[1:]:
        parts = line.split(",")
        if len(parts) >= 3 and parts[2].strip():
            current.add(SYMBOL_ALIAS.get(parts[2].strip(), parts[2].strip()))
    wiki = fetch(NIFTY_WIKI)
    changes = parse_wiki_changes(wiki["text"])
    names = {}
    for d, ex, inc in changes:
        if d <= dt.date(2009, 12, 31):
            continue
        for n in (ex, inc):
            if n not in NAME2SYM:
                raise RuntimeError("unmapped name " + n)
    # roll back from today to 2009-12-31; record intervals [start, end) per symbol
    members = set(current)
    if len(members) != 50:
        raise RuntimeError(f"current list has {len(members)}")
    intervals = {s: [[None, None]] for s in members}  # end None = still member
    problems = []
    for d, ex, inc in sorted(changes, key=lambda x: x[0], reverse=True):
        if d <= dt.date(2009, 12, 31):
            break
        es, isym = NAME2SYM[ex], NAME2SYM[inc]
        if isym not in members:
            problems.append(f"{d}: included {inc} not in set when rolling back")
            continue
        if es in members:
            problems.append(f"{d}: excluded {ex} already in set when rolling back")
            continue
        members.remove(isym)
        intervals[isym][-1][0] = d          # isym joined on d
        members.add(es)
        intervals.setdefault(es, []).append([None, d])  # es left on d (start unknown yet)
        if len(members) != 50:
            problems.append(f"{d}: size {len(members)}")
    for s in members:
        intervals[s][-1][0] = dt.date(2009, 12, 31)  # member at start of window
    # intervals with start None: joined before 2010 but not a member on 2009-12-31 -> left again
    clean = {}
    for s, ivs in intervals.items():
        for a, b in ivs:
            if a is None:
                continue
            clean.setdefault(s, []).append((a, b))
    return clean, members, changes, problems


def nifty_member(intervals, sym, day_from, day_to):
    for a, b in intervals.get(sym, []):
        if a <= day_from and (b is None or day_to < b):
            return True
    return False


# ---------------------------------------------------------------- Screener
MON = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def screener_docs(sym):
    page = fetch(SCREENER.format(SCREENER_SYMBOL.get(sym, sym)))
    if page["status"] != 200:
        return None, page
    soup = BeautifulSoup(page["text"], "html.parser")
    title = soup.find("h1")
    company = title.get_text(" ", strip=True) if title else sym
    docs = soup.find(id="documents")
    ars, calls = [], []
    if docs:
        for blk in docs.select("div.documents"):
            h = blk.find("h3").get_text(strip=True) if blk.find("h3") else ""
            if h.startswith("Annual reports"):
                for li in blk.select("li"):
                    a = li.find("a", href=True)
                    if not a:
                        continue
                    m = re.search(r"Annual Report (\d{4})", a.get_text(" ", strip=True))
                    src = li.find("span")
                    ars.append({"fy": int(m.group(1)) if m else None, "url": a["href"],
                                "label": a.get_text(" ", strip=True),
                                "from": src.get_text(strip=True) if src else ""})
            elif h.startswith("Concalls"):
                for li in blk.select("li"):
                    d = li.find("div")
                    if not d:
                        continue
                    m = re.match(r"([A-Z][a-z]{2}) (\d{4})", d.get_text(strip=True))
                    if not m:
                        continue
                    links = {a.get_text(strip=True): a["href"] for a in li.find_all("a", href=True)}
                    calls.append({"month": f"{m.group(2)}-{MON[m.group(1)]:02d}", "links": links})
    return {"company": company, "url": page["url"], "ars": ars, "calls": calls}, page


def upload_date(url):
    h = fetch(url, head=True)
    if h["status"] != 200:
        return None, h["status"]
    lm = {k.lower(): v for k, v in h["headers"].items()}.get("last-modified")
    if not lm:
        m = re.search(r"_(\d{2})(\d{2})(\d{4})\d{6}\.zip$", url)
        if m:
            return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))), 200
        return None, "no-last-modified"
    return email.utils.parsedate_to_datetime(lm).astimezone(IST).date(), 200


# ---------------------------------------------------------------- AGM date from the annual report's AGM notice
PDF_DIR = os.path.join(CACHE, "..", "pdf")
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
_MON_RE = "(" + "|".join(m.capitalize() for m in MONTHS) + "|" + "|".join(MONTHS) + "|" + "|".join(m.upper() for m in MONTHS) + ")"
_WD = r"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+)?(?:the\s+)?"
_DATE_A = re.compile(_WD + _MON_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I)
_DATE_B = re.compile(_WD + r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?" + _MON_RE + r",?\s+(\d{4})", re.I)
_HELD = re.compile(r"(?:annual\s+general\s+meeting|\bAGM\b)[^.]{0,250}?(?:will|shall|is\s+scheduled\s+to|scheduled\s+to|is\s+to|proposed\s+to)\s+be\s+(?:held|convened)\s+(?:on\s+)?", re.I)


def _parse_date_at(txt):
    best = None
    for rx, order in ((_DATE_A, "mdy"), (_DATE_B, "dmy")):
        m = rx.match(txt)
        if m:
            g = m.groups()
            try:
                if order == "mdy":
                    d = dt.date(int(g[2]), MONTHS.index(g[0].lower()) + 1, int(g[1]))
                else:
                    d = dt.date(int(g[2]), MONTHS.index(g[1].lower()) + 1, int(g[0]))
            except ValueError:
                continue
            best = d
            break
    return best


def pdf_text(url):
    """Download an annual report (PDF, or NSE zip holding PDFs) once and return its text."""
    os.makedirs(PDF_DIR, exist_ok=True)
    base = os.path.join(PDF_DIR, hashlib.sha1(url.encode()).hexdigest())
    if os.path.exists(base + ".txt"):
        with open(base + ".txt", encoding="utf-8", errors="replace") as f:
            return f.read()
    wait = 1.0 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    try:
        r = session.get(url, timeout=180)
    except requests.RequestException:
        return None
    finally:
        _last[0] = time.time()
    if r.status_code != 200:
        return None
    blobs = []
    if url.lower().endswith(".zip"):
        import io, zipfile
        try:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            for n in z.namelist():
                if n.lower().endswith(".pdf") and z.getinfo(n).file_size < 300_000_000:
                    blobs.append(z.read(n))
        except zipfile.BadZipFile:
            return None
    else:
        blobs.append(r.content)
    import subprocess
    texts = []
    for i, b in enumerate(blobs):
        tmp = f"{base}.{i}.pdf"
        with open(tmp, "wb") as f:
            f.write(b)
        try:
            out = subprocess.run(["pdftotext", "-q", tmp, "-"], capture_output=True, timeout=300)
            texts.append(out.stdout.decode("utf-8", "replace"))
        except subprocess.TimeoutExpired:
            pass
        os.remove(tmp)
    txt = "\n".join(texts)
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(txt)
    return txt


def agm_date_from_report(txt, upload):
    """AGM date announced in the report's notice: '...Annual General Meeting ... will be held on <date>'.
    Only dates within 150 days of the report's exchange file date are accepted."""
    flat = re.sub(r"\s+", " ", txt)
    votes = {}
    for m in _HELD.finditer(flat):
        win = flat[m.end():m.end() + 260]
        d = _parse_date_at(win[:80])
        if d is None:  # '... will be held at <venue>, on <date>'
            on = re.search(r"\bon\s+", win)
            d = _parse_date_at(win[on.end():on.end() + 80]) if on else None
        if d and dt.timedelta(days=-150) <= d - upload <= dt.timedelta(days=150):
            votes[d] = votes.get(d, 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: (kv[1], -kv[0].toordinal()))[0]


# ---------------------------------------------------------------- company-website results archives
# Each entry: archive page on the company's own site, and a function mapping a PDF link to
# (fiscal-year-end year, quarter, kind) where kind is "results" or "call"; None to skip.
def _wipro(href, text=""):
    m = re.search(r"/quarterly-results/(\d{4})-(\d{4})/[qQ](\d)", href)
    if not m:
        return None
    name = href.rsplit("/", 1)[-1].lower()
    if "indian-gaap" in name or "us-gaap" in name or "igaap" in name:
        return None
    if re.search(r"press-?release|earnings-release", name):
        return int(m.group(2)), int(m.group(3)), "results"
    if re.search(r"(earnings|conference)-(conference-)?call|call-transcript|earnings-transcript", name) and "us-" not in name:
        return int(m.group(2)), int(m.group(3)), "call"
    return None


def _divis(href, text=""):
    m = re.search(r"Press-Release-Qtr-?(\d)-(\d{4})-(\d{2})\.pdf$", href)
    if not m:
        return None
    return int(m.group(2)) + 1, int(m.group(1)), "results"


def _yy(x):
    x = int(x)
    return x if x > 1000 else 2000 + x


def _qfy(href, text=""):
    """Generic 'Q1FY11', 'Q1FY10-11', 'q1fy2018-19', 'q1-2019-20' file names (Aditya Birla group sites etc.)."""
    name = requests.utils.unquote(href.rsplit("/", 1)[-1])
    m = (re.search(r"q([1-4])[\s_-]*fy[\s_-]*(\d{4}|\d{2})(?:[-_](\d{4}|\d{2}))?(?!\d)", name, re.I)
         or re.search(r"q([1-4])[\s_-]+(\d{4})[-_](\d{2})(?!\d)", name, re.I))
    if not m:
        return None
    fy = _yy(m.group(3)) if m.group(3) else _yy(m.group(2))
    low = name.lower()
    if re.search(r"revised|invite|newspaper|news-paper|intimation|msat|satyam", low):
        return None
    if "transcript" in low:
        return fy, int(m.group(1)), "call"
    if re.search(r"result|press[-_ ]?release|sebi[-_]release", low) and "presentation" not in low:
        return fy, int(m.group(1)), "results"
    return None


_QEND_TXT = re.compile(r"results?\b[^|]{0,80}?(?:quarter|period|year|months)[^|]{0,60}?ended(?:\s+on)?\s+(?:"
                       + _MON_RE + r"\s+(\d{1,2}),?\s+(\d{4})|(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON_RE + r",?\s+(\d{4}))", re.I)
_QMAP = {6: 1, 9: 2, 12: 3, 3: 4}


def _generic(href, text=""):
    """File name with Q/FY tokens, else link text 'Financial Results for the quarter ended <date>'."""
    k = _qfy(href, text)
    if k:
        return k
    low = text.lower()
    if re.search(r"newspaper|advertis|auditor|notice|presentation|intimation|outcome|investor|limited review|clarification", low):
        return None
    m = _QEND_TXT.search(text)
    if not m:
        return None
    g = m.groups()
    mon, yr = (g[0], int(g[2])) if g[0] else (g[4], int(g[5]))
    mo = MONTHS.index(mon.lower()) + 1
    if mo not in _QMAP:
        return None
    return (yr if mo == 3 else yr + 1), _QMAP[mo], "results"


COMPANY_SITES = {
    "PNB": {"pages": ["https://pnb.bank.in/financials-archives.html"], "map": _generic},
    "ITC": {"pages": ["https://itcportal.com/investors/quarterly-results.html"], "map": _generic},
    "BAJAJ-AUTO": {"pages": ["https://www.bajajauto.com/investors/financial-and-operational-performance"], "map": _generic},
    "TECHM": {"pages": ["https://www.techmahindra.com/investors/quarterly-earnings/"], "map": _generic},
    "TATASTEEL": {"pages": ["https://www.tatasteel.com/investors/financial-performance/financial-results/"], "map": _generic},
    "GRASIM": {"pages": ["https://www.grasim.com/investors/results-reports-and-presentations"], "map": _qfy},
    "HINDALCO": {"pages": ["https://www.hindalco.com/investors/financial-performance/results-reports-presentations"], "map": _qfy},
    "WIPRO": {"pages": ["https://www.wipro.com/investors/quarterly-results/"], "map": _wipro},
    "DIVISLAB": {"pages": ["https://www.divislabs.com/investor-relations/press-releases/"], "map": _divis},
}
_DATE_ANY = re.compile(
    r"(?:" + _MON_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4}))|(?:(\d{1,2})\s*(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?"
    + _MON_RE + r",?\s+(\d{4}))|(?:\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b)", re.I)


def quarter_end(fy_end, q):
    return {1: dt.date(fy_end - 1, 6, 30), 2: dt.date(fy_end - 1, 9, 30),
            3: dt.date(fy_end - 1, 12, 31), 4: dt.date(fy_end, 3, 31)}[q]


def first_release_date(txt, qend, max_days=100):
    """Release date read from the document: a date introduced by 'Date:' / 'held on' wins, else the
    first date that falls 1..max_days after the quarter end."""
    flat = re.sub(r"\s+", " ", txt)
    found = []
    for m in _DATE_ANY.finditer(flat):
        g = m.groups()
        try:
            if g[0]:
                d = dt.date(int(g[2]), MONTHS.index(g[0].lower()) + 1, int(g[1]))
            elif g[4]:
                d = dt.date(int(g[5]), MONTHS.index(g[4].lower()) + 1, int(g[3]))
            else:
                d = dt.date(int(g[8]), int(g[7]), int(g[6]))
        except ValueError:
            continue
        if dt.timedelta(days=1) <= d - qend <= dt.timedelta(days=max_days):
            lead = flat[max(0, m.start() - 25):m.start()].lower()
            found.append((0 if re.search(r"(date|dated|held on)\s*[:\-]?\s*$", lead) else 1, len(found), d))
    return min(found)[2] if found else None


def pdf_first_pages(url):
    os.makedirs(PDF_DIR, exist_ok=True)
    base = os.path.join(PDF_DIR, hashlib.sha1(("p6 " + url).encode()).hexdigest())
    if os.path.exists(base + ".txt"):
        with open(base + ".txt", encoding="utf-8", errors="replace") as f:
            return f.read()
    wait = 1.0 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    try:
        r = session.get(url, timeout=120)
    except requests.RequestException:
        return None
    finally:
        _last[0] = time.time()
    if r.status_code != 200 or not r.content.startswith(b"%PDF"):
        return None
    import subprocess
    tmp = base + ".pdf"
    with open(tmp, "wb") as f:
        f.write(r.content)
    try:
        out = subprocess.run(["pdftotext", "-q", "-l", "6", tmp, "-"], capture_output=True, timeout=120)
        txt = out.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        txt = ""
    os.remove(tmp)
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(txt)
    return txt


def company_site_rows(sym, company, intervals, gaps):
    cfg = COMPANY_SITES[sym]
    picks = {}
    for page in cfg["pages"]:
        r = fetch(page)
        if r["status"] != 200:
            gaps.append({"scope": f"{sym} / Results (company site)", "reason": f"archive page HTTP {r['status']}"})
            continue
        soup = BeautifulSoup(r["text"], "html.parser")
        for a in soup.find_all("a", href=True):
            href = requests.compat.urljoin(r["url"], a["href"].strip())
            if not href.lower().endswith(".pdf"):
                continue
            k = cfg["map"](href, a.get_text(" ", strip=True))
            if not k:
                continue
            picks.setdefault(k, [])
            if href not in picks[k]:
                picks[k].append(href)
    rows = []
    for (fy, q, kind), urls in sorted(picks.items()):
        qend = quarter_end(fy, q)
        if qend < dt.date(2009, 9, 30) or qend > END:
            continue
        date, used = None, None
        for u in urls:
            txt = pdf_first_pages(u)
            if txt:
                date = first_release_date(txt, qend)
                if date:
                    used = u
                    break
        label = f"Q{q} FY{str(fy)[2:]}"
        typ = "Results" if kind == "results" else "Earnings call"
        if not date:
            gaps.append({"scope": f"{sym} / {typ} {label}", "reason": "company-site document found but no release date could be read from it"})
            continue
        if not (START <= date <= END):
            continue
        row = {"date": date.isoformat(), "market": MARKET, "symbol": sym, "company": company, "type": typ,
               "period": label, "title": ("Quarterly results press release " if kind == "results" else "Earnings call transcript ") + label,
               "doc_url": used, "doc_source": "company",
               "source": "Company IR archive; date read from the document"}
        if nifty_member(intervals, sym, date, date):
            row["index"] = "Nifty 50"
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/history/history_india_2010.json")
    args = ap.parse_args()

    intervals, start_set, changes, problems = build_membership()
    gaps = []
    for p in problems:
        gaps.append({"scope": "membership", "reason": "Rollback inconsistency: " + p})
    universe = sorted(s for s, ivs in intervals.items()
                      if any(a <= END and (b is None or b > START) for a, b in ivs))
    print(f"universe {len(universe)} symbols; start-of-2010 set {len(start_set)}", file=sys.stderr)

    rows, companies = [], set()
    for sym in universe:
        info, page = screener_docs(sym)
        if info is None:
            gaps.append({"scope": f"{sym} / all event types / 2010-2021",
                         "reason": f"Screener company page not available (HTTP {page['status']}); company not resolvable without NSE/BSE APIs"})
            continue
        company = re.sub(r"\s+", " ", info["company"])
        got = 0
        seen_fy = set()
        # Annual reports: prefer BSE PDF over NSE zip for the same fiscal year
        ars = sorted(info["ars"], key=lambda a: (a["fy"] or 0, 0 if "bse" in a["from"] else 1))
        for a in ars:
            if not a["fy"] or a["fy"] in seen_fy or a["fy"] < 2009 or a["fy"] > 2021:
                continue
            d, st = upload_date(a["url"])
            if d is None:
                gaps.append({"scope": f"{sym} / Annual report FY{a['fy']}",
                             "reason": f"Listed on Screener but exchange file date unavailable ({st})"})
                continue
            # plausibility: upload must fall in the FY year or the following year
            if not (a["fy"] <= d.year <= a["fy"] + 1):
                gaps.append({"scope": f"{sym} / Annual report FY{a['fy']}",
                             "reason": f"Exchange file Last-Modified {d} implausible for FY{a['fy']} (likely re-uploaded); date not established"})
                continue
            if not (START <= d <= END):
                continue
            seen_fy.add(a["fy"])
            row = {"date": d.isoformat(), "market": MARKET, "symbol": sym, "company": company,
                   "type": "Annual report", "period": f"FY{a['fy']}",
                   "title": f"{a['label']} (date = exchange file date)",
                   "doc_url": a["url"], "doc_source": "exchange",
                   "source": "Screener documents list; date from " + ("BSE" if "bse" in a["from"] else "NSE archives") + " file Last-Modified"}
            if nifty_member(intervals, sym, d, d):
                row["index"] = "Nifty 50"
            rows.append(row); got += 1
            txt = pdf_text(a["url"])
            agm = agm_date_from_report(txt, d) if txt else None
            if agm is None:
                gaps.append({"scope": f"{sym} / AGM FY{a['fy']}",
                             "reason": "AGM date not found in annual report text (notice not included, scanned PDF or unparseable)" if txt is not None
                             else "Annual report download failed; AGM date not extracted"})
            elif START <= agm <= END:
                arow = {"date": agm.isoformat(), "market": MARKET, "symbol": sym, "company": company,
                        "type": "AGM", "period": f"FY{a['fy']}",
                        "title": f"Annual General Meeting (date from AGM notice in {a['label']})",
                        "doc_url": a["url"], "doc_source": "exchange",
                        "source": "AGM notice text in exchange-hosted annual report (via Screener documents list)"}
                if nifty_member(intervals, sym, agm, agm):
                    arow["index"] = "Nifty 50"
                rows.append(arow); got += 1
        # Concalls: month only
        by_month = {}
        for c in info["calls"]:
            by_month.setdefault(c["month"], []).append(c)
        for month, items in sorted(by_month.items()):
            y, m = map(int, month.split("-"))
            first = dt.date(y, m, 1)
            last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
            if first < START or first > END:
                continue
            links = {}
            for it in items:
                for k, v in it["links"].items():
                    links.setdefault(k, v)
            url = links.get("Transcript") or links.get("PPT") or links.get("REC") or (next(iter(links.values())) if links else None)
            kinds = [k for k in ("Transcript", "PPT", "REC") if k in links]
            row = {"date": month, "market": MARKET, "symbol": sym, "company": company,
                   "type": "Earnings call",
                   "title": "Concall" + (f" ({', '.join(kinds)})" if kinds else "") + (f"; {len(items)} entries listed this month" if len(items) > 1 else ""),
                   "source": "Screener concalls list (month only)"}
            if url:
                row["doc_url"] = url
                row["doc_source"] = "exchange" if ("bseindia" in url or "nseindia" in url) else "company"
            if nifty_member(intervals, sym, first, last):
                row["index"] = "Nifty 50"
            rows.append(row); got += 1
        if sym in COMPANY_SITES:
            site_rows = company_site_rows(sym, company, intervals, gaps)
            call_months = {r["date"][:7] for r in site_rows if r["type"] == "Earnings call"}
            if call_months:  # exact-dated company transcripts supersede Screener month-only rows
                before = len(rows)
                rows[:] = [r for r in rows if not (r["symbol"] == sym and r["type"] == "Earnings call" and len(r["date"]) == 7 and r["date"] in call_months)]
                got -= before - len(rows)
            rows.extend(site_rows); got += len(site_rows)
        if got:
            companies.add(sym)
        print(f"{sym}: {got} rows", file=sys.stderr)

    rows.sort(key=lambda r: (r["date"], r["symbol"], r["type"]))
    out = {
        "market": MARKET,
        "range": {"from": "2010-01", "to": "2021-09"},
        "sources": [
            {"name": "NSE archives - current Nifty 50 constituents", "url": NIFTY_CSV},
            {"name": "Wikipedia NIFTY 50 'Index changes' table (cites NSE Indices IndexInclExcl.xls and NSE/IISL press releases)", "url": NIFTY_WIKI},
            {"name": "Screener company Documents (annual reports, concalls)", "url": "https://www.screener.in/company/<SYMBOL>/"},
            {"name": "BSE / NSE archive file headers (Last-Modified) for annual report upload dates", "url": "https://www.bseindia.com/bseplus/AnnualReport/"},
        ],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "membership": {"method": "Nifty 50 rolled back from official current list via dated replacement table", "universe": universe,
                       "members_2010_01_01": sorted(start_set)},
        "rows": rows,
        "gaps": gaps,
    }
    return out, args.out


if __name__ == "__main__":
    out, path = main()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{len(out['rows'])} rows, {len(out['gaps'])} gaps", file=sys.stderr)
