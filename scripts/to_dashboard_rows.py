"""Convert a history_<market>_2010.json file (schema rows) into the row shape the dashboard tables read.

Usage: python3 scripts/to_dashboard_rows.py data/history/history_korea_2010.json -o out/history_korea_2010.json [--years 2010-2015]

The dashboard groups event types more coarsely than the schema: AGMs and investor days
share "Investor day / meet", annual reports and prospectuses are "Filing", and trading
statements count as "Results". Gaps stay in the repo file; only rows are converted.
"""
import argparse
import json
import re
import sys

PREFIX = {"India": "in", "US": "us", "UK": "uk", "Japan": "jp", "South Korea": "kr", "Australia": "au"}
TYPE_MAP = {
    "Results": ("Results", "Reported"),
    "Trading statement": ("Results", "Reported"),
    "Earnings call": ("Earnings call", "Held"),
    "Investor day": ("Investor day / meet", "Held"),
    "AGM": ("Investor day / meet", "Held"),
    "Annual report": ("Filing", "Filed"),
    "Filing": ("Filing", "Filed"),
    "Prospectus": ("Filing", "Filed"),
    "IPO": ("IPO", "Listed"),
}
TYPE_CODE = {"Results": "r", "Earnings call": "c", "Investor day / meet": "m", "Filing": "f", "IPO": "ipo"}


def slug(text, n=60):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:n].strip("-")


def convert(row, seen):
    dtype, status = TYPE_MAP[row["type"]]
    sym = row["symbol"]
    company = row["company"] if dtype == "IPO" or f"({sym})" in row["company"] else f"{row['company']} ({sym})"
    detail = row.get("title") or row["type"]
    if row.get("period") and row["period"] not in detail:
        detail = f"{detail} ({row['period']})"
    base = "-".join(p for p in (PREFIX[row["market"]], slug(sym, 20), row["date"], TYPE_CODE[dtype], slug(detail, 24)) if p)
    rid, i = base, 2
    while rid in seen:
        rid, i = f"{base}-{i}", i + 1
    seen.add(rid)
    out = {
        "id": rid, "date": row["date"], "market": row["market"], "type": dtype, "company": company, "symbol": sym,
        "indices": row.get("index", ""), "detail": detail, "status": status,
        "doc_url": row.get("doc_url", ""), "doc_source": row.get("doc_source", ""),
    }
    # Optional fields are left out when empty or a repeat of doc_url, to keep files under the dashboard's 8 MB cap.
    if row.get("time"):
        out["time"] = row["time"]
    if row.get("source", "").startswith("http") and row["source"] != out["doc_url"]:
        out["source"] = row["source"]
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--years", help="keep only rows in this year range, e.g. 2010-2015 (to split large markets)")
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    rows = data["rows"] if isinstance(data, dict) else data
    if args.years:
        lo, hi = args.years.split("-")
        rows = [r for r in rows if lo <= r["date"][:4] <= hi]
    seen = set()
    out = sorted((convert(r, seen) for r in rows), key=lambda r: (r["date"], r["company"]), reverse=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out)} dashboard rows written", file=sys.stderr)


if __name__ == "__main__":
    main()
