"""Merge history_<market>.json files into one deduplicated, sorted events file.

Usage: python3 scripts/merge_events.py data/history/*.json -o data/events.json

Rows that share (market, symbol, type, date, period) are treated as one event;
the row with a company-hosted doc_url wins over an exchange or regulator copy.
"""
import argparse
import json
import sys

REQUIRED = ("date", "market", "symbol", "company", "type")
SOURCE_RANK = {"company": 0, "exchange": 1, "regulator": 2}


def load_rows(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("rows") or data.get("data") or data.get("results") or []
    return data


def rank(row):
    if not row.get("doc_url"):
        return 9
    return SOURCE_RANK.get(row.get("doc_source"), 5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args()

    merged, skipped = {}, 0
    for path in args.inputs:
        for row in load_rows(path):
            if any(not row.get(k) for k in REQUIRED):
                skipped += 1
                continue
            key = (row["market"], row["symbol"], row["type"], row["date"], row.get("period", ""))
            if key not in merged or rank(row) < rank(merged[key]):
                merged[key] = row

    rows = sorted(merged.values(), key=lambda r: (r["date"], r["market"], r["symbol"]))
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(rows)} events written, {skipped} incomplete rows skipped", file=sys.stderr)


if __name__ == "__main__":
    main()
