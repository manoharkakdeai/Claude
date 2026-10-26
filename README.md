# Market Events Tracker

Collectors and data behind the Market Events Tracker dashboard: results, earnings calls, investor days, AGMs, IPOs and prospectus filings for India, the US, the UK, Japan, South Korea and Australia.

## Layout

| Path | What it holds |
|---|---|
| `data/events.json` | Live calendar, merged from the history files plus current refreshes |
| `data/history/history_<market>.json` | Per-market history (fixed once a period has passed) |
| `data/watchlist.json` | Index members: one row per market, index, symbol, company |
| `schema/event.schema.json` | Row format shared by every events file |
| `scripts/merge_events.py` | Merges and deduplicates history files into `events.json` |
| `docs/sources.md` | Where each market's events come from, and link priority |

## Merge

```sh
python3 scripts/merge_events.py data/history/*.json -o data/events.json
```
