#!/usr/bin/env python3
"""Collect Japan (Nikkei 225) corporate events, Jan 2010 - Sep 2021.

Sources
  * Nikkei Indexes weight PDF (indexes.nikkei.co.jp) - dated constituent
    snapshot (closing 2019-10-01) used as the membership anchor.
  * Japanese Wikipedia "日経平均株価" - year-level history of constituent
    removals/additions, used to roll membership backwards/forwards from the
    anchor.  Changes are only known to the year, so in a year where a code was
    added or removed its membership is "unknown" and `index` is left out.
  * IRBANK (irbank.net) archive of TDnet disclosures: Kessan Tanshin
    (Results, with release time), AGM convocation notices (AGM), results
    briefing materials (Earnings call) and IR-day / business briefings
    (Investor day).  IRBANK's TDnet archive starts in 2013.
  * IRBANK archive of EDINET filings: 有価証券報告書 (Annual report),
    available from 2009/2010.

Usage: python3 -I scripts/collect_japan_2010.py [--limit N] [--no-tanshin-time]

Every HTTP response is cached (gzip) under the scratchpad dir so the collector
is resumable; re-running only fetches what is missing.
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import requests
from bs4 import BeautifulSoup

UA = "MarketEventsTracker/1.0 historical-backfill (github.com/manoharkakdeai/claude)"
SCRATCH = "/tmp/claude-0/-home-user-claude/acd3cf6e-52bc-5be0-ba82-933351f9e351/scratchpad/japan"
CACHE = os.path.join(SCRATCH, "cache")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "data", "history", "history_japan_2010.json")
IRBANK = "https://irbank.net"
NIKKEI_PDF = "https://indexes.nikkei.co.jp/nkave/archives/file/nikkei_stock_average_weight_jp.pdf"
WIKI = "https://ja.wikipedia.org/wiki/%E6%97%A5%E7%B5%8C%E5%B9%B3%E5%9D%87%E6%A0%AA%E4%BE%A1"
MIN_DATE, MAX_DATE = "2010-01-01", "2021-09-30"
ANCHOR_DATE = "2019-10-01"
INDEX = "Nikkei 225"
THROTTLE = 1.1

# Constituent changes 2010-2021 from the Wikipedia table (names as printed
# there), mapped to TSE codes.  Codes present in the 2019-10-01 Nikkei PDF are
# cross-checked against the PDF name; the rest against the IRBANK company name
# (see verify step, results written to the scratchpad).
CHANGES = [
    # (year, "out"/"in", code, wikipedia name)
    (2010, "out", "9205", "日本航空"), (2010, "out", "5001", "新日本石油"),
    (2010, "out", "5016", "新日鉱ホールディングス"), (2010, "out", "8755", "損害保険ジャパン"),
    (2010, "out", "3404", "三菱レイヨン"), (2010, "out", "6796", "クラリオン"),
    (2010, "in", "9022", "東海旅客鉄道"), (2010, "in", "5407", "日新製鋼"),
    (2010, "in", "5020", "JXホールディングス"), (2010, "in", "8630", "NKSJホールディングス"),
    (2010, "in", "5214", "日本電気硝子"), (2010, "in", "8804", "東京建物"),
    (2011, "out", "6764", "三洋電機"), (2011, "out", "6991", "パナソニック電工"),
    (2011, "out", "8403", "住友信託銀行"), (2011, "out", "8404", "みずほ信託銀行"),
    (2011, "out", "8606", "みずほ証券"), (2011, "out", "9737", "CSK"),
    (2011, "in", "6506", "安川電機"), (2011, "in", "7735", "大日本スクリーン製造"),
    (2011, "in", "8750", "第一生命保険"), (2011, "in", "8304", "あおぞら銀行"),
    (2011, "in", "8729", "ソニーフィナンシャルホールディングス"), (2011, "in", "6113", "アマダ"),
    (2012, "out", "5405", "住友金属工業"), (2012, "out", "5407", "日新製鋼"),
    (2012, "out", "5701", "日本軽金属"),
    (2012, "in", "4043", "トクヤマ"), (2012, "in", "5458", "日新製鋼ホールディングス"),
    (2012, "in", "5703", "日本軽金属ホールディングス"),
    (2013, "out", "8815", "東急不動産"), (2013, "out", "3864", "三菱製紙"),
    (2013, "in", "6988", "日東電工"), (2013, "in", "3289", "東急不動産ホールディングス"),
    (2014, "out", "1334", "マルハニチロホールディングス"), (2014, "in", "1333", "マルハニチロ"),
    (2015, "out", "8803", "平和不動産"), (2015, "out", "3110", "日東紡績"),
    (2015, "in", "2432", "ディー・エヌ・エー"), (2015, "in", "1808", "長谷工コーポレーション"),
    (2016, "out", "8332", "横浜銀行"), (2016, "out", "6753", "シャープ"),
    (2016, "out", "8270", "ユニーグループ・ホールディングス"), (2016, "out", "4041", "日本曹達"),
    (2016, "in", "7186", "コンコルディア・フィナンシャルグループ"), (2016, "in", "7272", "ヤマハ発動機"),
    (2016, "in", "8028", "ファミリーマート"), (2016, "in", "4755", "楽天"),
    (2017, "out", "6767", "ミツミ電機"), (2017, "out", "6502", "東芝"),
    (2017, "out", "3865", "北越紀州製紙"), (2017, "out", "6508", "明電舎"),
    (2017, "in", "4578", "大塚ホールディングス"), (2017, "in", "6724", "セイコーエプソン"),
    (2017, "in", "6098", "リクルートホールディングス"), (2017, "in", "6178", "日本郵政"),
    (2018, "out", "5715", "古河機械金属"), (2018, "out", "5413", "日新製鋼"),
    (2018, "in", "4751", "サイバーエージェント"), (2018, "in", "4631", "DIC"),
    (2019, "out", "6773", "パイオニア"), (2019, "out", "5002", "昭和シェル石油"),
    (2019, "out", "6366", "千代田化工建設"), (2019, "out", "9681", "東京ドーム"),
    (2019, "in", "6645", "オムロン"), (2019, "in", "5019", "出光興産"),
    (2019, "in", "7832", "バンダイナムコホールディングス"), (2019, "in", "2413", "エムスリー"),
    (2020, "out", "8729", "ソニーフィナンシャルホールディングス"), (2020, "out", "4272", "日本化薬"),
    (2020, "out", "8028", "ファミリーマート"), (2020, "out", "9437", "NTTドコモ"),
    (2020, "in", "8697", "日本取引所グループ"), (2020, "in", "9434", "ソフトバンク"),
    (2020, "in", "3659", "ネクソン"), (2020, "in", "6753", "シャープ"),
    (2021, "out", "3105", "日清紡ホールディングス"), (2021, "out", "5901", "東洋製缶グループホールディングス"),
    (2021, "out", "9412", "スカパーJSATホールディングス"), (2021, "out", "9062", "日本通運"),
]
# 日新製鋼: 5407 -> 日新製鋼HD 5458 (2012) -> 日新製鋼 5413 (2014 re-merger, no
# change listed in the table).  Exact membership of 5458/5413 cannot be
# established from the sources, so their index is left unknown throughout.
UNKNOWN_ALWAYS = {"5458", "5413"}


# --------------------------------------------------------------------- fetch
_session = requests.Session()
_session.headers["User-Agent"] = UA
_session.verify = "/root/.ccr/ca-bundle.crt" if os.path.exists("/root/.ccr/ca-bundle.crt") else True
_last = [0.0]
STATS = {"fetched": 0, "cached": 0, "failed": 0}


def _cache_path(url):
    h = hashlib.sha1(url.encode()).hexdigest()
    return os.path.join(CACHE, h[:2], h + ".gz")


def fetch(url, binary=False, retries=6):
    """GET with cache, throttle and retry. Returns bytes/str, or None on 404/failure."""
    p = _cache_path(url)
    if os.path.exists(p):
        STATS["cached"] += 1
        with gzip.open(p, "rb") as f:
            data = f.read()
        if data == b"__404__":
            return None
        return data if binary else data.decode("utf-8", "replace")
    for attempt in range(retries):
        wait = THROTTLE - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = _session.get(url, timeout=90)
        except requests.RequestException as e:
            print(f"  ! {url} {type(e).__name__}; retry {attempt + 1}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with gzip.open(p, "wb") as f:
                f.write(r.content)
            STATS["fetched"] += 1
            return r.content if binary else r.content.decode("utf-8", "replace")
        if r.status_code == 404:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with gzip.open(p, "wb") as f:
                f.write(b"__404__")
            return None
        if r.status_code in (403, 401):
            print(f"  ! {url} HTTP {r.status_code} (access refused, not retrying)", file=sys.stderr)
            STATS["failed"] += 1
            return None
        print(f"  ! {url} HTTP {r.status_code}; retry {attempt + 1}", file=sys.stderr)
        time.sleep(8 * (attempt + 1))
    STATS["failed"] += 1
    return None


# ---------------------------------------------------------------- membership
def load_anchor():
    pdf = fetch(NIKKEI_PDF, binary=True)
    if not pdf:
        raise SystemExit("cannot fetch Nikkei weight PDF")
    tmp = os.path.join(SCRATCH, "nikkei_weight.pdf")
    with open(tmp, "wb") as f:
        f.write(pdf)
    txt = subprocess.run(["pdftotext", "-layout", tmp, "-"], capture_output=True, text=True).stdout
    m = re.search(r"\((\d{4})/(\d{1,2})/(\d{1,2})終値ベース\)", txt)
    assert m and (m.group(1), int(m.group(2)), int(m.group(3))) == ("2019", 10, 1), "unexpected PDF date"
    anchor = {}
    for line in txt.splitlines():
        mm = re.match(r"\s+(\d{4})\s+(.+?)\s{2,}[\d.]+\s*$", line)
        if mm:
            anchor[mm.group(1)] = mm.group(2).strip()
    assert len(anchor) == 225, len(anchor)
    return anchor


def check_wikipedia():
    """Fetch the Wikipedia change table and confirm every CHANGES name is listed for its year."""
    html = fetch(WIKI)
    soup = BeautifulSoup(html, "html.parser")
    tbl = soup.find(id="構成銘柄除外および採用の歴史").find_next("table")
    rows = {}
    for tr in tbl.find_all("tr"):
        cells = [c.get_text("", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) >= 3 and cells[0][:4].isdigit():
            rows[int(cells[0][:4])] = (cells[1], cells[2])
    problems = []
    for year, d, code, name in CHANGES:
        col = rows.get(year, ("", ""))[0 if d == "out" else 1]
        if name not in col:
            problems.append(f"{year} {d} {code} {name} not found in Wikipedia row")
    return problems


def membership(anchor):
    """Return {code: {year: 'member'|'non'|'unknown'}} for 2010..2021."""
    ch = {}
    for year, d, code, _ in CHANGES:
        ch.setdefault(code, {}).setdefault(year, []).append(d)
    codes = set(anchor) | set(ch)
    res = {}
    for code in codes:
        st = {}
        changes = ch.get(code, {})
        # status at start of 2019 is derived from the 2019-10-01 anchor
        anchor_status = code in anchor
        # forward from anchor: 2019 (rest), 2020, 2021
        s = anchor_status
        for y in (2020, 2021):
            if y in changes:
                st[y] = "unknown"
                for d in changes[y]:
                    s = (d == "in")
            else:
                st[y] = "member" if s else "non"
        # backward from anchor
        s = anchor_status
        for y in range(2019, 2009, -1):
            if y in changes:
                st[y] = "unknown"
                ds = changes[y]
                if len(ds) > 1:
                    s = None  # added and removed in the same year
                else:
                    s = (ds[0] == "out")  # removed in y -> member before y
            else:
                st[y] = "unknown" if s is None else ("member" if s else "non")
        if code in UNKNOWN_ALWAYS:
            st = {y: "unknown" for y in st}
        res[code] = st
    return res


# ------------------------------------------------------------------- parsing
def parse_period(name):
    """'平成28年3月期第1四半期決算短信' -> 'Q1 FY2016/03'; annual -> 'FY2016/03'."""
    m = re.search(r"(?:平成\s*(\d+|元)|(20\d\d))\s*年\s*(\d{1,2})\s*月期", name)
    if not m:
        return None
    if m.group(1):
        y = 1989 if m.group(1) == "元" else 1988 + int(m.group(1))
    else:
        y = int(m.group(2))
    fy = f"FY{y}/{int(m.group(3)):02d}"
    rest = name[m.end():]
    q = re.search(r"第\s*([1-4１-４一二三四])\s*四半期", rest)
    if q:
        n = {"１": 1, "２": 2, "３": 3, "４": 4, "一": 1, "二": 2, "三": 3, "四": 4}.get(q.group(1)) or int(q.group(1))
        return f"Q{n} {fy}"
    if "中間" in rest[:12]:
        return f"H1 {fy}"
    return fy


TITLE_RE = re.compile(r"\|\s*(.*?)\s+-\s+(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})提出")


def parse_other_entries(html):
    """Entries in the 'その他資料' list of an IRBANK tdnet page."""
    out = []
    for m in re.finditer(r'<dd class="gf"><a title="([^"]*)" href="(/[0-9A-Z]{4}/\d{18})">', html):
        t = BeautifulSoup(m.group(1), "html.parser").get_text()
        tm = TITLE_RE.search(t)
        if not tm:
            continue
        name = tm.group(1)
        date = f"{tm.group(2)}-{int(tm.group(3)):02d}-{int(tm.group(4)):02d}"
        time_ = f"{int(tm.group(5)):02d}:{tm.group(6)}"
        out.append({"name": name, "date": date, "time": time_, "href": m.group(2)})
    nxt = re.search(r'href="(/E\d{5}/tdnet\?y=\d+)">More</a>', html)
    return out, (nxt.group(1) if nxt else None)


EXCLUDE = ("訂正", "修正", "お知らせ", "開催", "変更", "延期", "中止")


def classify(name):
    if any(x in name for x in EXCLUDE):
        return None
    if "招集" in name and "株主総会" in name and "臨時" not in name:
        if "インターネット開示" in name or "ウェブ開示" in name or "WEB開示" in name.upper():
            return None
        return "AGM"
    low = name.lower()
    if any(x in low for x in ("ir day", "investor day", "インベスターデー", "事業説明会", "経営説明会",
                               "中期経営計画説明会", "経営計画説明会", "ir説明会資料", "戦略説明会")):
        return "Investor day"
    if (("決算" in name or "四半期" in name or "results" in low or "earnings" in low)
            and any(x in low for x in ("説明会", "プレゼンテーション", "説明資料", "presentation", "質疑応答", "決算説明"))):
        return "Earnings call"
    return None


def tanshin_links(html):
    """(title, href, link text) for Kessan Tanshin links on the IRBANK tdnet page."""
    out, seen = [], set()
    for m in re.finditer(r'<a title="([^"]*決算短信[^"]*)" href="(/[0-9A-Z]{4}/(\d{18}))">([^<]*)<', html):
        title, href, docid, text = m.groups()
        title = BeautifulSoup(title, "html.parser").get_text()
        if any(x in title for x in ("訂正", "修正", "損益計算書", "補足", "説明")):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append((title, href, docid, text))
    return out


DOC_TITLE_RE = re.compile(r"<title>[^<]*（(\d{4})/(\d{2})/(\d{2}) (\d{1,2}):(\d{2})提出）")


def yuho_links(html):
    """Annual reports (有価証券報告書) on the IRBANK reports page."""
    out = []
    for m in re.finditer(r'<a title="(有価証券報告書-[^"]*)" href="notes\?f=([A-Z0-9]{8})">([^<]*)</a>', html):
        title, fid, text = m.groups()
        if "損益計算書" in title:
            continue
        out.append((BeautifulSoup(title, "html.parser").get_text(), fid, text))
    return out


def period_end_from_yuho_title(title):
    m = re.search(r"-(\d{4})/(\d{2})/(\d{2})\)", title)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"-(?:平成)?(\d+|元)年(\d{1,2})月(\d{1,2})日\)", title)
    if m:
        y = m.group(1)
        y = 1989 if y == "元" else (1988 + int(y) if int(y) < 100 else int(y))
        return dt.date(y, int(m.group(2)), int(m.group(3)))
    return None


# ------------------------------------------------------------------- collect
def company_info(code):
    html = fetch(f"{IRBANK}/{code}")
    if not html:
        return None
    t = re.search(r"<title>\s*[0-9A-Z]{4}\s+(.*?)\s*\|", html)
    e = re.search(r'href="/(E\d{5})/(?:tdnet|reports|edinet)"', html)
    return {"name": t.group(1).strip() if t else None, "ecode": e.group(1) if e else None}


def in_range(d):
    return MIN_DATE <= d <= MAX_DATE


def collect_company(code, info, mem, rows, gaps, tanshin_time=True):
    name, ecode = info["name"], info["ecode"]
    if not ecode:
        gaps.append({"scope": f"{code} {name or ''} all events",
                     "reason": "IRBANK has no disclosure archive (no EDINET/TDnet code) for this delisted company"})
        return

    def status(date):
        return mem.get(int(date[:4]), "non")

    def add(row):
        st = status(row["date"])
        if st == "non" or not in_range(row["date"]):
            return
        r = {"date": row["date"]}
        if row.get("time"):
            r["time"] = row["time"]
        r.update({"market": "Japan"})
        if st == "member":
            r["index"] = INDEX
        r.update({"symbol": code, "company": name, "type": row["type"]})
        if row.get("period"):
            r["period"] = row["period"]
        r["title"] = row["title"]
        r["doc_url"] = row["doc_url"]
        r["doc_source"] = row["doc_source"]
        r["source"] = row["source"]
        rows.append(r)

    # ---- TDnet (IRBANK archive): first page + More chain
    tdurl = f"{IRBANK}/{ecode}/tdnet"
    html = fetch(tdurl)
    if not html:
        gaps.append({"scope": f"{code} {name} TDnet events", "reason": "IRBANK TDnet page could not be fetched"})
    else:
        tans = tanshin_links(html)
        page, n = html, 0
        oldest_td = None
        while page:
            entries, nxt = parse_other_entries(page)
            for e in entries:
                oldest_td = e["date"] if (oldest_td is None or e["date"] < oldest_td) else oldest_td
                typ = classify(e["name"])
                if typ:
                    add({"date": e["date"], "time": e["time"], "type": typ, "period": parse_period(e["name"]),
                         "title": e["name"], "doc_url": IRBANK + e["href"], "doc_source": "exchange",
                         "source": "TDnet (IRBANK archive)"})
            if not nxt or (entries and min(x["date"] for x in entries) < MIN_DATE):
                break
            n += 1
            page = fetch(IRBANK + nxt)
            if page is None:
                gaps.append({"scope": f"{code} {name} TDnet other disclosures before {oldest_td}",
                             "reason": "IRBANK paging request failed after retries"})
        # Kessan Tanshin: exact date + time from each document page
        for title, href, docid, text in tans:
            created = docid[4:12]  # TDnet id embeds the registration date (<= release date)
            if created > MAX_DATE.replace("-", ""):
                continue
            date = time_ = None
            if tanshin_time:
                doc = fetch(IRBANK + href)
                m = DOC_TITLE_RE.search(doc or "")
                if m:
                    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                    time_ = f"{int(m.group(4)):02d}:{m.group(5)}"
            if not date:
                gaps.append({"scope": f"{code} {name} Results {title}",
                             "reason": "release date not readable from IRBANK document page"})
                continue
            add({"date": date, "time": time_, "type": "Results", "period": parse_period(title),
                 "title": title, "doc_url": IRBANK + href, "doc_source": "exchange",
                 "source": "TDnet (IRBANK archive)"})

    # ---- EDINET annual reports (IRBANK archive)
    rep = fetch(f"{IRBANK}/{ecode}/reports")
    if not rep:
        gaps.append({"scope": f"{code} {name} Annual report", "reason": "IRBANK reports page could not be fetched"})
        return
    for title, fid, text in yuho_links(rep):
        if "訂正" in title:
            continue
        pend = period_end_from_yuho_title(title)
        date = time_ = None
        md = re.fullmatch(r"(\d{1,2})/(\d{1,2})", text.strip())
        if md and pend:
            # filing date shown as M/D; it is the first such day after period end
            mo, d = int(md.group(1)), int(md.group(2))
            for y in (pend.year, pend.year + 1):
                try:
                    cand = dt.date(y, mo, d)
                except ValueError:
                    continue
                if pend < cand <= pend + dt.timedelta(days=366):
                    date = cand.isoformat()
                    break
            if date and not (MIN_DATE <= date <= "2021-12-31"):
                continue
        else:
            if pend and not (dt.date(2009, 9, 30) <= pend <= dt.date(2021, 9, 30)):
                continue
            notes = fetch(f"{IRBANK}/{ecode}/notes?f={fid}")
            m = re.search(r"(\d{4})/(\d{2})/(\d{2}) (\d{1,2}):(\d{2})提出", notes or "")
            if m:
                date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                time_ = f"{int(m.group(4)):02d}:{m.group(5)}"
        if not date:
            gaps.append({"scope": f"{code} {name} Annual report {title}", "reason": "filing date not determinable"})
            continue
        period = f"FY{pend.year}/{pend.month:02d}" if pend else None
        add({"date": date, "time": time_, "type": "Annual report", "period": period, "title": title,
             "doc_url": f"{IRBANK}/{ecode}/notes?f={fid}", "doc_source": "regulator",
             "source": "EDINET (IRBANK archive)"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-tanshin-time", action="store_true")
    ap.add_argument("--codes", default="")
    args = ap.parse_args()
    os.makedirs(CACHE, exist_ok=True)

    anchor = load_anchor()
    wiki_problems = check_wikipedia()
    mem = membership(anchor)
    universe = sorted(c for c, st in mem.items() if any(v != "non" for v in st.values()))
    if args.codes:
        universe = [c for c in universe if c in args.codes.split(",")]
    if args.limit:
        universe = universe[: args.limit]
    print(f"universe: {len(universe)} codes", file=sys.stderr)

    rows, gaps, verify = [], [], []
    for p in wiki_problems:
        gaps.append({"scope": "index membership", "reason": p})
    for i, code in enumerate(universe, 1):
        info = company_info(code)
        print(f"[{i}/{len(universe)}] {code} {info and info['name']} {info and info['ecode']} "
              f"(fetched {STATS['fetched']}, cached {STATS['cached']})", file=sys.stderr)
        wname = next((n for _, _, c, n in CHANGES if c == code), None)
        verify.append({"code": code, "anchor_name_2019": anchor.get(code), "wikipedia_name": wname,
                       "irbank_name": info and info["name"], "ecode": info and info["ecode"],
                       "membership": {str(k): v for k, v in sorted(mem[code].items())}})
        if not info:
            gaps.append({"scope": f"{code} ({wname or anchor.get(code)}) all events",
                         "reason": "no IRBANK page for this code"})
            continue
        collect_company(code, info, mem[code], rows, gaps, tanshin_time=not args.no_tanshin_time)

    # dedupe on merge key
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda r: (r["date"], r["symbol"], r["type"], r.get("time", ""))):
        k = (r["market"], r["symbol"], r["type"], r["date"], r.get("period", ""))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    with open(os.path.join(SCRATCH, "membership_verify.json"), "w", encoding="utf-8") as f:
        json.dump(verify, f, ensure_ascii=False, indent=1)

    out = {
        "market": "Japan",
        "range": {"from": "2010-01", "to": "2021-09"},
        "sources": [
            {"name": "IRBANK TDnet archive", "url": "https://irbank.net/", "used_for": "Results (Kessan Tanshin, with release time), AGM convocation notices, results briefing / investor day materials; archive begins 2013"},
            {"name": "IRBANK EDINET archive", "url": "https://irbank.net/", "used_for": "Annual reports (有価証券報告書) 2010-2021"},
            {"name": "Nikkei Indexes weight list (closing 2019-10-01)", "url": NIKKEI_PDF, "used_for": "Nikkei 225 membership anchor"},
            {"name": "Wikipedia ja: 日経平均株価 (構成銘柄除外および採用の歴史)", "url": WIKI, "used_for": "year-level constituent changes 2010-2021"},
        ],
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "rows": uniq,
        "gaps": gaps,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"{len(uniq)} rows, {len(gaps)} gaps -> {OUT}; {STATS}", file=sys.stderr)


if __name__ == "__main__":
    main()
