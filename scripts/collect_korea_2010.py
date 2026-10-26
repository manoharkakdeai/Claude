"""Collect South Korea (KOSPI 200) corporate events, Jan 2010 - Sep 2021, from DART.

Sources
  * DART web disclosure search (dart.fss.or.kr/dsab007/detailSearch.ax), swept
    market-wide for KOSPI-listed companies (corporationType=P) by exact DART
    report name, in 3-month windows, then filtered to the member universe.
  * DART corp lookup (dart.fss.or.kr/corp/searchExistAll.ax) to map 6-digit KRX
    stock codes to DART corp codes.
  * Member universe: the KOSPI 200 constituent tables on en.wikipedia.org/wiki/KOSPI_200
    and ko.wikipedia.org/wiki/코스피_200 (CURRENT membership only - KRX/KIND
    historical constituent pages refuse automated reads or need a login).
    Because membership at the event date cannot be established, rows carry no
    `index` field; see the gaps list in the output file.

Every row links the DART regulator copy (dsaf001/main.do?rcpNo=...) built from the
receipt number in a fetched search listing. Company-website copies are not
collected (no verified URLs).

Usage: python3 -I scripts/collect_korea_2010.py [--cache DIR] [-o data/history/history_korea_2010.json]
Resumable: every HTTP response is cached under the cache dir and reused.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

UA = "MarketEventsTracker/1.0 historical-backfill (github.com/manoharkakdeai/claude)"
DART = "https://dart.fss.or.kr"
CA = "/root/.ccr/ca-bundle.crt"
DEFAULT_CACHE = "/tmp/claude-0/-home-user-claude/acd3cf6e-52bc-5be0-ba82-933351f9e351/scratchpad/korea/cache"
WIKI = {
    "en": "https://en.wikipedia.org/wiki/KOSPI_200",
    "ko": "https://ko.wikipedia.org/wiki/%EC%BD%94%EC%8A%A4%ED%94%BC_200",
}
START, END = dt.date(2010, 1, 1), dt.date(2021, 9, 30)

# DART exact report names -> schema type
REPORTS = [
    ("연결재무제표기준영업(잠정)실적(공정공시)", "Results"),
    ("영업(잠정)실적(공정공시)", "Results"),
    ("기업설명회(IR)개최", "Investor day"),
    ("기업설명회(IR)개최(안내공시)", "Investor day"),
    ("정기주주총회결과", "AGM"),
    ("사업보고서", "Annual report"),
    ("반기보고서", "Filing"),
    ("분기보고서", "Filing"),
]

session = requests.Session()
session.headers["User-Agent"] = UA
session.verify = CA if os.path.exists(CA) else True
_last = [0.0]


def fetch(cache_dir, method, url, data=None, referer=None):
    key = hashlib.sha1(json.dumps([method, url, data], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = os.path.join(cache_dir, key[:2], key + ".html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    for attempt in range(5):
        wait = 1.0 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            headers = {"Referer": referer} if referer else {}
            r = session.request(method, url, data=data, headers=headers, timeout=60)
            if r.status_code == 200:
                r.encoding = r.encoding or "utf-8"
                text = r.text
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                return text
            print(f"HTTP {r.status_code} {url} {data}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"error {e} {url}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"failed {url} {data}")


def load_members(cache_dir):
    """Return {stock_code: {"en": name, "ko": name}} from the current wiki constituent tables."""
    members = {}
    for lang, url in WIKI.items():
        soup = BeautifulSoup(fetch(cache_dir, "GET", url), "html.parser")
        for table in soup.find_all("table"):
            heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
            if not ({"Symbol", "종목코드"} & set(heads)):
                continue
            for tr in table.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                if len(cells) >= 2 and re.fullmatch(r"\d{6}", cells[1]):
                    members.setdefault(cells[1], {})[lang] = cells[0]
    return members


def corp_code(cache_dir, stock):
    txt = fetch(cache_dir, "POST", DART + "/corp/searchExistAll.ax", {"textCrpNm": stock},
                referer=DART + "/dsab007/main.do").strip()
    codes = [c for c in re.split(r"[,\s]+", txt) if re.fullmatch(r"\d{8}", c)]
    return codes[0] if len(codes) == 1 else None


def windows():
    d = START
    while d <= END:
        m = d.month + 3
        nxt = dt.date(d.year + (m - 1) // 12, (m - 1) % 12 + 1, 1)
        yield d, min(nxt - dt.timedelta(days=1), END)
        d = nxt


def parse_list(html):
    soup = BeautifulSoup(html, "html.parser")
    total = 0
    info = soup.select_one(".pageInfo")
    if info:
        m = re.search(r"총\s*([\d,]+)", info.get_text())
        total = int(m.group(1).replace(",", "")) if m else 0
    out = []
    for tr in soup.select("table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        cik = re.search(r"openCorpInfoNew\('(\d{8})'", str(tds[1]))
        rcp = re.search(r"rcpNo=(\d{14})", str(tds[2]))
        if not (cik and rcp):
            continue
        name = tds[1].get_text(" ", strip=True)
        name = re.sub(r"^(유|코|넥|기)\s+", "", name)
        out.append({
            "cik": cik.group(1), "corp": name,
            "title": " ".join(tds[2].get_text(" ", strip=True).split()),
            "date": tds[4].get_text(strip=True).replace(".", "-"),
            "rcpNo": rcp.group(1),
        })
    return total, out


def sweep(cache_dir, report_name):
    rows = []
    for a, b in windows():
        page, got = 1, []
        while True:
            data = {"currentPage": str(page), "maxResults": "100", "maxLinks": "10", "sort": "date",
                    "series": "desc", "corporationType": "P", "reportName": report_name,
                    "startDate": a.strftime("%Y%m%d"), "endDate": b.strftime("%Y%m%d")}
            total, items = parse_list(fetch(cache_dir, "POST", DART + "/dsab007/detailSearch.ax", data,
                                            referer=DART + "/dsab007/main.do"))
            got += items
            if not items or len(got) >= total:
                break
            page += 1
        if len(got) != total:
            print(f"WARN {report_name} {a}..{b}: got {len(got)} of {total}", file=sys.stderr)
        rows += got
        print(f"{report_name} {a}..{b}: {len(got)}", file=sys.stderr)
    return rows


def period_of(title, kind):
    m = re.search(r"\((\d{4})\.(\d{2})\)", title)
    if not m:
        return None
    ym = f"{m.group(1)}-{m.group(2)}"
    if kind == "사업보고서":
        return f"FY ended {ym}"
    if kind == "반기보고서":
        return f"Half-year ended {ym}"
    return f"Quarter ended {ym}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("-o", "--output", default="data/history/history_korea_2010.json")
    args = ap.parse_args()

    members = load_members(args.cache)
    print(f"{len(members)} current constituents", file=sys.stderr)
    cik2sym, unresolved = {}, []
    for sym in sorted(members):
        c = corp_code(args.cache, sym)
        if c:
            cik2sym[c] = sym
        else:
            unresolved.append(sym)

    raw = {}
    for name, typ in REPORTS:
        for it in sweep(args.cache, name):
            if it["cik"] in cik2sym:
                raw.setdefault(it["rcpNo"], (name, typ, it))

    keyed, seen_sym = {}, set()
    for rcp, (name, typ, it) in sorted(raw.items()):
        title = it["title"]
        if title.startswith("["):  # [기재정정]/[첨부정정]/... amendments: keep originals only
            continue
        if not (START.isoformat() <= it["date"] <= END.isoformat()):
            continue
        sym = it["cik"] and cik2sym[it["cik"]]
        period = period_of(title, name) if typ in ("Annual report", "Filing") else None
        row = {
            "date": it["date"], "market": "South Korea", "symbol": sym,
            "company": members[sym].get("en") or it["corp"], "type": typ,
        }
        if period:
            row["period"] = period
        row.update({
            "title": f"{title} ({it['corp']})",
            "doc_url": f"{DART}/dsaf001/main.do?rcpNo={rcp}",
            "doc_source": "regulator", "source": "DART",
        })
        key = (sym, typ, row["date"], period or "")
        prev = keyed.get(key)
        # same-day duplicates (e.g. consolidated + separate provisional results): prefer consolidated
        if prev is None or ("연결" in title and "연결" not in prev["title"]):
            keyed[key] = row
        seen_sym.add(sym)

    rows = sorted(keyed.values(), key=lambda r: (r["date"], r["symbol"], r["type"]))
    no_rows = sorted(set(members) - seen_sym)
    gaps = [
        {"scope": "index membership (all rows)",
         "reason": "Historical KOSPI 200 constituents could not be fetched: kind.krx.co.kr, www/index/open/global.krx.co.kr return 403 (Akamai) to automated reads, "
                   "data.krx.co.kr constituent data requires a login, and the Wayback Machine was unreachable through the proxy. Universe = current constituents from "
                   "the en/ko Wikipedia KOSPI 200 tables (fetched Oct 2026); rows therefore omit `index`, and companies that left the index before 2026 (or delisted) are not covered."},
        {"scope": "doc_url (all rows)",
         "reason": "Company-website copies not collected; only the DART regulator copy (receipt-number URL from the fetched listing) is linked."},
        {"scope": "Results / period",
         "reason": "Provisional-results report names on DART carry no fiscal period; period left empty. Only companies that filed 영업(잠정)실적 fair disclosures are covered; "
                   "where consolidated and separate notices were filed the same day only the consolidated one is kept."},
        {"scope": "Investor day (기업설명회(IR)개최)",
         "reason": "Row date is the DART notice filing date, not the IR session date (which is only inside each notice); notices are not split into earnings calls vs other IR events. "
                   "Multiple notices filed on the same day by one company are collapsed to one row."},
        {"scope": "AGM",
         "reason": "Date is the filing date of 정기주주총회결과 (normally the meeting day). AGM convocation notices not collected."},
        {"scope": "amendments",
         "reason": "Bracket-prefixed DART titles ([기재정정], [첨부정정], ...) are skipped; only original filings are kept."},
        {"scope": "time",
         "reason": "DART search listings give receipt date only; no release time collected."},
    ]
    if unresolved:
        gaps.append({"scope": "symbols " + ", ".join(unresolved),
                     "reason": "Stock code did not resolve to a single DART corp code via /corp/searchExistAll.ax."})
    if no_rows:
        gaps.append({"scope": "symbols " + ", ".join(no_rows),
                     "reason": "Current constituents with no matching DART filings in 2010-01..2021-09 (mostly listed or created after Sep 2021, or filings under a predecessor corp code)."})

    out = {
        "market": "South Korea",
        "range": {"from": "2010-01", "to": "2021-09"},
        "sources": [
            "DART detail search https://dart.fss.or.kr/dsab007/detailSearch.ax (corporationType=P, exact report names)",
            "DART corp lookup https://dart.fss.or.kr/corp/searchExistAll.ax",
            WIKI["en"], WIKI["ko"],
        ],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
        "gaps": gaps,
    }
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{len(rows)} rows, {len(seen_sym)} companies -> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
