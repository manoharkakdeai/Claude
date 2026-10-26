#!/usr/bin/env python3
"""Collect IPO / prospectus events, Jan 2010 - Sep 2021, for India, UK, Japan,
South Korea and Australia, into data/history/history_ipos_2010.json.

Sources (probed for reachability before use; unreachable ones become gaps):
  India      SEBI "Public Issues" filings list (Draft offer documents filed with
             SEBI, Red Herring documents and final offer documents filed with
             ROC) -> type "Prospectus".  BSE API / NSE are blocked -> gap.
  UK         London Stock Exchange monthly issuer-list archives
             (docs.londonstockexchange.com, "Issuer list archive YYYY.zip"),
             admission dates of Main Market and AIM companies -> type "IPO".
  Japan      JPX Monthly Statistics Report, table "Changes in Listed Companies
             and Issues" (17_idou YYMM.pdf), "New Listings" block -> type "IPO".
  S. Korea   DART detailed search, 증권신고서(지분증권) originals by filers that
             had no KRX exchange disclosures in the prior 12 months and are now
             KOSPI/KOSDAQ listed (or are SPACs) -> type "Prospectus".
             KIND / KRX sites answer Access Denied, data.krx.co.kr needs login.
  Australia  ASX listed-companies directory (listing date of companies still
             listed today) -> type "IPO".

Usage:  python3 -I scripts/collect_ipos_2010.py [--markets india,uk,japan,korea,australia]
Raw responses are cached under the scratchpad dir (SCRATCH env var or default),
so re-running resumes without refetching.
"""
import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile

import requests
from bs4 import BeautifulSoup

UA = "MarketEventsTracker/1.0 historical-backfill (github.com/manoharkakdeai/claude)"
SCRATCH = os.environ.get(
    "SCRATCH",
    "/tmp/claude-0/-home-user-claude/acd3cf6e-52bc-5be0-ba82-933351f9e351/scratchpad/ipos",
)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "data", "history", "history_ipos_2010.json")
START, END = dt.date(2010, 1, 1), dt.date(2021, 9, 30)
DELAY = 1.0

SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA
_last = [0.0]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(market, url, data=None, headers=None, binary=False, ext="html", retries=3):
    """GET/POST with on-disk cache (resumable) and polite throttling."""
    d = os.path.join(SCRATCH, market)
    os.makedirs(d, exist_ok=True)
    key = url + ("|" + json.dumps(data, sort_keys=True, ensure_ascii=False) if data else "")
    path = os.path.join(d, hashlib.sha1(key.encode()).hexdigest()[:20] + "." + ext)
    if os.path.exists(path):
        with open(path, "rb") as f:
            b = f.read()
        return b if binary else b.decode("utf-8", "replace")
    for attempt in range(retries):
        wait = DELAY - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            if data is None:
                r = SESSION.get(url, headers=headers, timeout=120)
            else:
                r = SESSION.post(url, data=data, headers=headers, timeout=120)
            _last[0] = time.time()
            if r.status_code == 200:
                with open(path, "wb") as f:
                    f.write(r.content)
                return r.content if binary else r.content.decode("utf-8", "replace")
            log(f"  HTTP {r.status_code} {url}")
            if r.status_code in (403, 404):
                return None
        except requests.RequestException as e:
            _last[0] = time.time()
            log(f"  error {e} {url}")
        time.sleep(3 * (attempt + 1))
    return None


def in_range(d):
    return START <= d <= END


def slug_symbol(name):
    s = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")
    return s


# ---------------------------------------------------------------- India (SEBI)
SEBI_LIST = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"
SEBI_REF = "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&ssid=15&smid=10"
SEBI_SMID = {
    "10": ("Draft Offer Documents filed with SEBI", "DRHP filed with SEBI"),
    "11": ("Red Herring Documents filed with ROC", "Red herring prospectus filed with ROC"),
    "12": ("Final Offer Documents filed with ROC", "Final offer document (prospectus) filed with ROC"),
}
SEBI_SKIP = re.compile(r"addendum|corrigend|clarification|errat|withdraw|public announcement|advertisement|notice", re.I)
SEBI_SUFFIX = re.compile(
    r"\s*[-–—:]+\s*(draft red herring prospectus|drhp|red herring prospectus|rhp|prospectus|"
    r"draft letter of offer|draft prospectus|final offer document|offer document|letter of offer|"
    r"updated draft red herring prospectus.*|udrhp.*|draft abridged prospectus|abridged prospectus).*$",
    re.I,
)


def sebi_page(smid, frm, to, page):
    data = {
        "nextValue": "1", "next": "n" if page else "s", "search": "", "fromDate": frm, "toDate": to,
        "fromYear": "", "toYear": "", "deptId": "-1", "sid": "3", "ssid": "15", "smid": smid,
        "ssidhidden": "15", "intmid": "-1", "sText": "Filings", "ssText": "Public Issues",
        "smText": SEBI_SMID[smid][0], "doDirect": str(page) if page else "-1",
    }
    return fetch("india", SEBI_LIST, data=data, headers={"Referer": SEBI_REF})


def collect_india(gaps):
    rows = []
    for smid, (label, title) in SEBI_SMID.items():
        for year in range(2010, 2022):
            frm, to = f"01-01-{year}", ("30-09-2021" if year == 2021 else f"31-12-{year}")
            page, seen_pages = 0, None
            while True:
                html = sebi_page(smid, frm, to, page)
                if html is None:
                    gaps.append({"scope": f"India SEBI {label} {year} page {page + 1}", "reason": "SEBI list request failed"})
                    break
                soup = BeautifulSoup(html.split("#@#")[0], "html.parser")
                if seen_pages is None:
                    m = re.search(r"(\d+) to (\d+) of (\d+)", soup.get_text(" "))
                    total = int(m.group(3)) if m else 0
                    seen_pages = (total + 24) // 25
                for tr in soup.select("tr"):
                    tds = tr.find_all("td")
                    if len(tds) < 2:
                        continue
                    try:
                        d = dt.datetime.strptime(tds[0].get_text(strip=True), "%b %d, %Y").date()
                    except ValueError:
                        continue
                    a = tds[1].find("a")
                    t = a.get_text(" ", strip=True) if a else tds[1].get_text(" ", strip=True)
                    href = a.get("href") if a else None
                    if not in_range(d) or SEBI_SKIP.search(t):
                        continue
                    company = SEBI_SUFFIX.sub("", t).strip(" -–")
                    company = re.sub(r"\s+", " ", company)
                    if not company:
                        continue
                    row = {
                        "date": d.isoformat(), "market": "India", "symbol": slug_symbol(company),
                        "company": company, "type": "Prospectus",
                        "title": f"{title}: {t}", "doc_source": "regulator",
                        "source": f"SEBI Filings > Public Issues > {label}",
                    }
                    if href and href.startswith("http"):
                        row["doc_url"] = href
                    rows.append(row)
                page += 1
                if page >= (seen_pages or 0):
                    break
        log(f"India smid {smid}: {len(rows)} rows so far")
    return rows


# ------------------------------------------------------------- UK (LSE lists)
LSE_HIST = "https://docs.londonstockexchange.com/reports/issuers/historic.json"
LSE_INST = "https://docs.londonstockexchange.com/reports/instruments/Instrument%20list.xlsx"
LSE_MARKETS = {"MAIN MARKET": "Main Market", "AIM": "AIM"}


def norm_name(s):
    s = (s or "").upper().replace("&", " AND ")
    s = re.sub(r"\b(PUBLIC LIMITED COMPANY|PLC|LIMITED|LTD|HLDGS|HOLDINGS|GROUP|GRP|THE|INC|CORP|CORPORATION|SE|NV|SA|AG)\b", " ", s)
    return re.sub(r"[^A-Z0-9]+", "", s)


def lse_sheet_rows(blob, fname):
    """Yield (sheet, admission_date, company, market) from one archive workbook."""
    if fname.lower().endswith(".xls"):
        import xlrd
        wb = xlrd.open_workbook(file_contents=blob, on_demand=True)
        for sn in wb.sheet_names():
            sh = wb.sheet_by_name(sn)
            hdr = None
            for r in range(sh.nrows):
                vals = sh.row_values(r)
                low = [str(v).strip().lower() for v in vals]
                if hdr is None:
                    if any(v in ("list date", "admission date") for v in low) and "company" in " ".join(low):
                        hdr = low
                        ci = next(i for i, v in enumerate(hdr) if v.startswith("company"))
                        di = next(i for i, v in enumerate(hdr) if v in ("list date", "admission date"))
                        mi = next((i for i, v in enumerate(hdr) if v == "market"), None)
                        si = [i for i, v in enumerate(hdr) if v in ("sector", "sub sector", "icb industry", "icb super-sector")]
                    continue
                dv = vals[di]
                if isinstance(dv, float) and dv > 0:
                    d = xlrd.xldate.xldate_as_datetime(dv, wb.datemode).date()
                elif isinstance(dv, str) and re.match(r"\d{2}/\d{2}/\d{4}", dv.strip()):
                    d = dt.datetime.strptime(dv.strip()[:10], "%d/%m/%Y").date()
                else:
                    continue
                yield sn, d, str(vals[ci]).strip(), (str(vals[mi]).strip().upper() if mi is not None else ""), " ".join(str(vals[i]) for i in si if i < len(vals))
            wb.unload_sheet(sn)
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True)
        for sn in wb.sheetnames:
            hdr = None
            for vals in wb[sn].iter_rows(values_only=True):
                low = [str(v).strip().lower() if v is not None else "" for v in vals]
                if hdr is None:
                    if any(v in ("list date", "admission date") for v in low) and any(v.startswith("company") for v in low):
                        hdr = low
                        ci = next(i for i, v in enumerate(hdr) if v.startswith("company"))
                        di = next(i for i, v in enumerate(hdr) if v in ("list date", "admission date"))
                        mi = next((i for i, v in enumerate(hdr) if v == "market"), None)
                        si = [i for i, v in enumerate(hdr) if v in ("sector", "sub sector", "icb industry", "icb super-sector")]
                    continue
                if len(vals) <= max(ci, di):
                    continue
                dv = vals[di]
                if isinstance(dv, dt.datetime):
                    d = dv.date()
                elif isinstance(dv, dt.date):
                    d = dv
                elif isinstance(dv, str) and re.match(r"\d{2}/\d{2}/\d{4}", dv.strip()):
                    d = dt.datetime.strptime(dv.strip()[:10], "%d/%m/%Y").date()
                else:
                    continue
                yield sn, d, str(vals[ci] or "").strip(), (str(vals[mi] or "").strip().upper() if mi is not None else ""), " ".join(str(vals[i] or "") for i in si if i < len(vals))


NON_EQUITY = re.compile(r"preference|debenture|loans", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def collect_uk(gaps):
    meta = fetch("uk", LSE_HIST, ext="json")
    if not meta:
        gaps.append({"scope": "UK all", "reason": "LSE issuer-list archive index not reachable"})
        return []
    files = {}
    for x in json.loads(meta)["Data"]:
        m = re.search(r"(\d{4})", x["fileName"])
        if m and 2009 <= int(m.group(1)) <= 2021:
            files[int(m.group(1))] = x["downloadLink"]
    # first_seen[norm] = (snapshot month index, admission date, company, market)
    first_seen = {}
    snapshots = {}  # norm -> set of (snapshot_ym)
    nonequity = set()
    entries = {}  # (norm, admission date) -> (company, market, first snapshot ym, year, link)
    for year in sorted(files):
        link = files[year]
        blob = fetch("uk", link, binary=True, ext="zip")
        if not blob:
            gaps.append({"scope": f"UK {year}", "reason": "LSE issuer-list archive download failed"})
            continue
        z = zipfile.ZipFile(io.BytesIO(blob))
        for fn in z.namelist():
            if not fn.lower().endswith((".xls", ".xlsx")):
                continue
            for sn, d, company, mkt, sector in lse_sheet_rows(z.read(fn), fn):
                mon = MONTHS.get(sn.strip().lower()[:3])
                if not mon or not company:
                    continue
                if NON_EQUITY.search(sector):
                    nonequity.add(norm_name(company))
                    continue
                ym = year * 100 + mon
                n = norm_name(company)
                if n not in first_seen or ym < first_seen[n]:
                    first_seen[n] = ym
                snapshots.setdefault(n, set()).add(ym)
                k = (n, d)
                if k not in entries or ym < entries[k][2]:
                    entries[k] = (company, mkt, ym, year, link)
        log(f"UK {year}: {len(entries)} issuer/admission pairs")
    # current TIDMs (only for companies still listed today)
    tidm = {}
    inst = fetch("uk", LSE_INST, binary=True, ext="xlsx")
    if inst:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(inst), read_only=True)
        ws = wb["1.1 Shares"]
        hdr = None
        for vals in ws.iter_rows(values_only=True):
            if hdr is None:
                if vals and vals[0] == "TIDM":
                    hdr = list(vals)
                continue
            if vals[0] and vals[1]:
                tidm.setdefault(norm_name(vals[1]), str(vals[0]).strip())
    rows, transfers, late = [], 0, 0
    for (n, d), (company, mkt, ym, year, link) in entries.items():
        if not in_range(d):
            continue
        label = None
        for k, v in LSE_MARKETS.items():
            if mkt.startswith(k):
                label = v
        if not label:
            continue
        adm_ym = d.year * 100 + d.month
        # company already on an earlier monthly snapshot before this admission month
        # => transfer between markets / re-admission, not a new listing.
        if any(s < adm_ym for s in snapshots.get(n, ())):
            transfers += 1
            continue
        fy, fm = divmod(first_seen.get(n, ym), 100)
        if (fy * 12 + fm) - (d.year * 12 + d.month) > 2:
            late += 1  # first appears in the issuer list long after its admission date
            continue
        name = re.sub(r"\s+", " ", company).strip()
        rows.append({
            "date": d.isoformat(), "market": "UK", "symbol": tidm.get(n) or slug_symbol(name),
            "company": name, "type": "IPO",
            "title": f"Admission to trading, London Stock Exchange {label}" + (" (AIM growth market)" if label == "AIM" else ""),
            "doc_url": link, "doc_source": "exchange",
            "source": f"LSE issuer list archive {year} (monthly snapshot)",
        })
    gaps.append({"scope": "UK issuers first listed in the archive long after their admission date", "reason": f"{late} admissions dropped: the issuer only appears in a monthly snapshot more than two months after its stated admission date (renamed issuers whose earlier name already carries the admission, or archive scope changes such as debt vehicles added later) (archive scope changes, e.g. debt/funding vehicles added to the list later), so the date cannot be confirmed as a new equity listing"})
    gaps.append({"scope": "UK preference-share / debenture issuers", "reason": f"{len(nonequity)} issuers classed in LSE sectors 'Preference' or 'Debentures & Loans' excluded as non-equity listings"})
    gaps.append({"scope": "UK admissions that were moves between markets",
                 "reason": f"{transfers} admissions dropped because the issuer already appeared in an earlier monthly LSE snapshot (AIM<->Main Market moves, re-admissions)"})
    return rows


# --------------------------------------------------------------- Japan (JPX)
JPX_ARCH = "https://www.jpx.co.jp/english/markets/statistics-equities/monthly/00-archives-{:02d}.html"
JPX_EXTRA = ["https://www.jpx.co.jp/english/markets/statistics-equities/monthly/b5b4pj000000qc9y-att/17_idou1508.pdf"]
JPX_SECT = [
    ("市場第一部", "TSE 1st Section"), ("市場第二部", "TSE 2nd Section"), ("マザーズ", "TSE Mothers"),
    ("ＪＡＳＤＡＱスタンダード", "JASDAQ Standard"), ("ＪＡＳＤＡＱグロース", "JASDAQ Growth"),
    ("TOKYO PRO Market", "TOKYO PRO Market"),
]
JPX_ROW = re.compile(r"(\d{4}\.\d{2}\.\d{2})\s+([0-9A-Z]{4,8})\s+(\S.*?)\s{2,}(\S.*)$")


def collect_japan(gaps):
    pdfs = []
    for i in range(5, 11):  # 2021 .. 2016 archive pages
        html = fetch("japan", JPX_ARCH.format(i))
        if not html:
            gaps.append({"scope": f"Japan archive page {i}", "reason": "JPX monthly archive page not reachable"})
            continue
        for h in re.findall(r'href="([^"]*_idou\d{4}\.pdf)"', html):
            pdfs.append("https://www.jpx.co.jp" + h if h.startswith("/") else h)
    pdfs = sorted(set(pdfs + JPX_EXTRA))
    rows = []
    tmpdir = os.path.join(SCRATCH, "japan")
    for url in pdfs:
        yymm = re.search(r"idou(\d{4})", url).group(1)
        if not ("1001" <= yymm <= "2109"):
            continue
        blob = fetch("japan", url, binary=True, ext="pdf")
        if not blob:
            gaps.append({"scope": f"Japan 20{yymm[:2]}-{yymm[2:]}", "reason": "JPX idou PDF download failed"})
            continue
        p = os.path.join(tmpdir, f"idou{yymm}.pdf")
        with open(p, "wb") as f:
            f.write(blob)
        txt = subprocess.run(["pdftotext", "-layout", p, "-"], capture_output=True, text=True).stdout
        a = txt.find("New Listings")
        b = txt.find("Delisting", a)
        block = txt[a:b] if a >= 0 else ""
        sect = None
        for line in block.splitlines():
            m = JPX_ROW.search(line)
            head = line[: m.start()].strip() if m else line.strip()
            if head:
                matched = [v for k, v in JPX_SECT if head.startswith(k)]
                if matched:
                    sect = matched[0]
                elif re.search(r"[぀-ヿ一-鿿]", head):  # another Japanese section label
                    sect = None
            if not m or not sect:
                continue
            d = dt.datetime.strptime(m.group(1), "%Y.%m.%d").date()
            code = m.group(2)
            if len(code) != 4 or not in_range(d):  # 5+ digit codes are warrants / preferred
                continue
            jp, en = m.group(3).strip(), re.sub(r"\s+", " ", m.group(4)).strip()
            title = f"New listing, {sect}" + (" (professional market)" if sect == "TOKYO PRO Market" else "")
            rows.append({
                "date": d.isoformat(), "market": "Japan", "symbol": code, "company": en or jp,
                "type": "IPO", "title": f"{title}: {jp}", "doc_url": url, "doc_source": "exchange",
                "source": f"JPX Monthly Statistics Report, Changes in Listed Companies (20{yymm[:2]}-{yymm[2:]})",
            })
    return rows


# ------------------------------------------------------------- Korea (DART)
DART = "https://dart.fss.or.kr"
DART_FORM = None


def dart_form():
    global DART_FORM
    if DART_FORM is None:
        html = fetch("korea", DART + "/dsab007/main.do")
        soup = BeautifulSoup(html, "html.parser")
        f = soup.find("form", id="searchForm")
        data = {}
        for i in f.find_all(["input", "select"]):
            n = i.get("name")
            if not n or (i.get("type") in ("radio", "checkbox") and not i.has_attr("checked")):
                continue
            if i.name == "select":
                o = i.find("option", selected=True) or i.find("option")
                data[n] = o.get("value") if o else ""
            else:
                data[n] = i.get("value") or ""
        data.pop("finalReport", None)  # all versions, so originals keep their own date
        DART_FORM = data
    return dict(DART_FORM)


def dart_search(**kw):
    out, page = [], 1
    while True:
        data = dart_form()
        data.update({k: v for k, v in kw.items()})
        data.update({"currentPage": str(page), "maxResults": "100"})
        html = fetch("korea", DART + "/dsab007/detailSearch.ax", data=data,
                     headers={"Referer": DART + "/dsab007/main.do"})
        if html is None:
            return None
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) < 6:
                continue
            ca = tds[1].find("a")
            cm = re.search(r"openCorpInfoNew\('(\d+)'", ca.get("href", "")) if ca else None
            tag = tds[1].find("span", title=True)
            ra = tds[2].find("a")
            rc = re.search(r"rcpNo=(\d+)", ra.get("href", "")) if ra else None
            out.append({
                "corp": cm.group(1) if cm else None,
                "name": ca.get_text(" ", strip=True) if ca else tds[1].get_text(" ", strip=True),
                "tag": tag.get("title") if tag else "",
                "report": re.sub(r"\s+", " ", tds[2].get_text(" ", strip=True)),
                "date": tds[4].get_text(strip=True), "remark": tds[5].get_text(" ", strip=True),
                "rcp": rc.group(1) if rc else None,
            })
        m = re.search(r"\[(\d+)/(\d+)\]", soup.get_text(" "))
        if not m or int(m.group(1)) >= int(m.group(2)):
            return out
        page += 1


def dart_ticker(corp):
    html = fetch("korea", DART + "/dsae001/select.ax", data={"selectKey": corp},
                 headers={"Referer": DART + "/dsae001/main.do"})
    if not html:
        return None, None
    t = BeautifulSoup(html, "html.parser").get_text("|", strip=True).split("|")
    def after(label):
        return t[t.index(label) + 1] if label in t and t.index(label) + 1 < len(t) else None
    code = after("종목코드")
    eng = after("영문명")
    return (code if code and re.fullmatch(r"[0-9A-Z]{6}", code) else None), eng


def collect_korea(gaps):
    originals = []
    d = dt.date(2010, 1, 1)
    while d <= END:
        nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        last = min(nxt - dt.timedelta(days=1), END)
        res = dart_search(startDate=d.strftime("%Y%m%d"), endDate=last.strftime("%Y%m%d"),
                          publicType="C001", option="report")
        if res is None:
            gaps.append({"scope": f"South Korea {d:%Y-%m}", "reason": "DART search request failed"})
        else:
            for r in res:
                if r["report"].strip() == "증권신고서(지분증권)" and r["corp"]:
                    originals.append(r)
        d = nxt
    log(f"Korea: {len(originals)} original equity registration statements")
    # keep first original filing per company per 180-day window (re-filings after withdrawal kept separately)
    rows, followons, skipped_other = [], 0, 0
    for r in originals:
        fd = dt.datetime.strptime(r["date"], "%Y.%m.%d").date()
        listed_now = r["tag"] in ("유가증권시장", "코스닥시장")
        spac = "기업인수목적" in r["name"]
        if not (listed_now or spac):
            skipped_other += 1
            continue
        prior = dart_search(startDate=(fd - dt.timedelta(days=365)).strftime("%Y%m%d"),
                            endDate=(fd - dt.timedelta(days=1)).strftime("%Y%m%d"),
                            option="corp", textCrpCik=r["corp"], textCrpNm=r["name"])
        if prior is None:
            gaps.append({"scope": f"South Korea {r['name']} {r['date']}", "reason": "DART prior-disclosure check failed"})
            continue
        if any(re.search(r"(^|\s)(유|코|넥)(\s|$)", p["remark"]) for p in prior):
            followons += 1  # filer was already exchange-listed (KOSPI/KOSDAQ/KONEX) -> not an IPO
            continue
        ticker, eng = dart_ticker(r["corp"])
        rows.append({
            "date": fd.isoformat(), "market": "South Korea", "symbol": ticker or f"DART{r['corp']}",
            "company": r["name"], "type": "Prospectus",
            "title": "IPO securities registration statement (증권신고서(지분증권))"
                     + (f" – {eng}" if eng else "") + (" – SPAC" if spac else ""),
            "doc_url": f"{DART}/dsaf001/main.do?rcpNo={r['rcp']}", "doc_source": "regulator",
            "source": "DART (FSS) detailed search, 증권신고서(지분증권)",
        })
    gaps.append({"scope": "South Korea equity registration statements by filers now unlisted (non-SPAC)",
                 "reason": f"{skipped_other} filings excluded: filer is not KOSPI/KOSDAQ listed today (withdrawn IPOs, since-delisted IPOs, foreign employee-share offerings and unlisted public offerings cannot be told apart without opening documents, and DART robots.txt disallows the document viewer)"})
    gaps.append({"scope": "South Korea follow-on offerings", "reason": f"{followons} filings excluded as follow-ons: filer had KRX exchange disclosures (remark 유/코/넥) in the 12 months before filing"})
    return rows


# --------------------------------------------------------- Australia (ASX)
ASX_DIR = "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file"


def collect_australia(gaps):
    txt = fetch("australia", ASX_DIR, ext="csv")
    if not txt:
        gaps.append({"scope": "Australia all", "reason": "ASX directory not reachable"})
        return []
    import csv
    rows = []
    for r in csv.DictReader(io.StringIO(txt)):
        try:
            d = dt.datetime.strptime(r["Listing date"].strip(), "%d/%m/%Y").date()
        except (ValueError, KeyError):
            continue
        if not in_range(d):
            continue
        code = r["ASX code"].strip()
        rows.append({
            "date": d.isoformat(), "market": "Australia", "symbol": code,
            "company": r["Company name"].strip(),
            "type": "IPO", "title": "Admission to the ASX official list (listing date)",
            "doc_url": f"https://www.asx.com.au/markets/company/{code}", "doc_source": "exchange",
            "source": "ASX listed companies directory",
        })
    return rows


# ---------------------------------------------------------------------- main
STATIC_GAPS = [
    {"scope": "US IPOs and S-1 filings (all years)", "reason": "Out of scope for now: sec.gov blocks requests whose User-Agent lacks a contact email, and no email may be used"},
    {"scope": "India IPO listing dates (BSE and NSE, all years)", "reason": "api.bseindia.com answers Akamai 'Access Denied' to the tracker User-Agent (public-issue archive is served only via that API); www.nseindia.com not reachable from this environment"},
    {"scope": "India SME IPOs", "reason": "SME offer documents are filed with BSE SME / NSE Emerge, not SEBI; both sources unreachable"},
    {"scope": "India addenda / corrigenda to offer documents", "reason": "Deliberately skipped; only the DRHP, RHP and final offer document entries are kept. SEBI 'Public Issues' also lists the occasional FPO, which the list does not label separately"},
    {"scope": "South Korea IPO listing dates (KOSPI/KOSDAQ/KONEX, all years)", "reason": "kind.krx.co.kr, www.krx.co.kr, global.krx.co.kr and open.krx.co.kr answer Akamai 'Access Denied'; data.krx.co.kr requires a login; OpenDART needs an API key (none available)"},
    {"scope": "Japan new listings 2010-01 to 2015-12 (except 2015-08)", "reason": "JPX new-listing archive pages only reach back to 2022 and the monthly statistics archive to 2016; older monthly PDFs have unguessable URLs (only the Aug 2015 PDF was found via search)"},
    {"scope": "Japan technical listings", "reason": "The JPX 'New Listings' table includes holding-company formations and other technical listings alongside IPOs without a flag; rows are titled 'New listing' and not filtered"},
    {"scope": "Japan Nagoya / Fukuoka / Sapporo exchanges, ETFs, REITs, infrastructure funds, warrants", "reason": "Not in the TSE table or deliberately excluded (non-company or non-TSE listings)"},
    {"scope": "Japan prospectuses (有価証券届出書)", "reason": "EDINET not probed/collected in this pass"},
    {"scope": "Australia companies delisted since listing (2010-2021 floats no longer on ASX)", "reason": "Only the current ASX listed-companies directory carries listing dates; ASX has no reachable historical new-listings archive, so delisted/merged floats are missing (survivorship)"},
    {"scope": "Australia prospectuses", "reason": "ASX announcements search is disallowed by robots.txt (/search*) and no other prospectus archive is reachable"},
    {"scope": "UK prospectuses (FCA National Storage Mechanism) and RNS intention-to-float notices", "reason": "Not collected: docs.londonstockexchange.com new-issues report files return 403 and RNS/NSM were not reachable in automated form"},
    {"scope": "UK symbols", "reason": "LSE issuer archives carry no TIDM; TIDM is taken from today's LSE instrument list where the issuer name matches, else a name-derived symbol is used"},
    {"scope": "UK companies listed and delisted within the same month", "reason": "LSE archives are month-end snapshots, so such admissions are invisible"},
    {"scope": "India symbols", "reason": "SEBI filings carry no ticker; symbol is the upper-case company name slug"},
    {"scope": "index membership (all markets)", "reason": "Not set: new listings are not index members on the listing date in these sources; no membership was inferred"},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default="india,uk,japan,korea,australia")
    ap.add_argument("-o", "--output", default=OUT)
    args = ap.parse_args()
    fns = {"india": collect_india, "uk": collect_uk, "japan": collect_japan,
           "korea": collect_korea, "australia": collect_australia}
    rows, gaps = [], list(STATIC_GAPS)
    for m in args.markets.split(","):
        log(f"== {m}")
        got = fns[m](gaps)
        log(f"   {m}: {len(got)} rows")
        rows.extend(got)
    # dedupe on (market, symbol, type, date, period)
    seen, uniq = set(), []
    for r in rows:
        k = (r["market"], r["symbol"], r["type"], r["date"], r.get("period", ""))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    uniq.sort(key=lambda r: (r["date"], r["market"], r["symbol"]))
    out = {
        "market": "India, UK, Japan, South Korea, Australia (IPOs and prospectuses)",
        "range": {"from": "2010-01", "to": "2021-09"},
        "sources": [
            "SEBI Filings > Public Issues (www.sebi.gov.in)",
            "London Stock Exchange issuer-list archives (docs.londonstockexchange.com)",
            "JPX Monthly Statistics Report, Changes in Listed Companies (www.jpx.co.jp)",
            "DART detailed search (dart.fss.or.kr)",
            "ASX listed companies directory (asx.api.markitdigital.com / www.asx.com.au)",
        ],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "rows": uniq,
        "gaps": gaps,
    }
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    log(f"wrote {len(uniq)} rows to {args.output}")


if __name__ == "__main__":
    main()
