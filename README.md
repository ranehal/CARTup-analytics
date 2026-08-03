# CartUp Price Tracker

Price history & deals tracker for [CartUp](https://www.cartup.com) (api.cartup.com). A scraper snapshots product prices daily, and a static GitHub Pages dashboard renders categories, subcategories, flash-sale and campaign sections, price-history graphs, compare, analytics and price-target alerts.

## Features

- **Independent realtime scraper** — no reliance on the captured HAR; talks directly to the CartUp API. Pure-Python (`scraper.py`, standard library only).
- **Daily price history** — `data/history.json` records `YYYY-MM-DD:price` entries per product; each run writes a delta snapshot to `data/daily/YYYY-MM-DD.json`.
- **Categories & subcategories** — full 3-level category tree (16 top, 169 mid, 1232 leaf).
- **Sections** — Flash Sale + mega/offer/pop builder pages, shown as a card strip.
- **Dashboard** — search, filters (category, subcategory, sort, discount, stock, all-time lows, price cap), infinite-scroll grid, per-card sparklines, history chart with 30D/90D/1Y/All ranges, compare, analytics, CSV export.
- **Alerts** — set a price target per product, persisted in `localStorage`; notified when the price crosses it.

## Data model

Product records in `data/products.json`:

| key | meaning |
| --- | --- |
| `i` | product id |
| `n` | name |
| `img` | thumbnail path (relative to `https://sl-dev-s3.s3.amazonaws.com`) |
| `p` | discounted price |
| `m` | MRP |
| `d` | discount % |
| `st` | in stock |
| `rt` / `rc` | rating / rating count |
| `fs` | free shipping |
| `sl` | product slug |
| `c` / `t` / `sc` | category path / top category / subcategory |
| `sec` | section ids (e.g. `flash-563`, `mega-…`) |

`data/meta.json` holds stats, channels, sections (with `productIds` and `count`) and the category tree. `data/history.json` maps product id → comma-joined `date:price` pairs (the same price on consecutive days is not re-appended; `null`-free, stock changes are captured via the daily files).

## Running

Requirements: Python 3.9+ (standard library only — no pip dependencies needed).

```bash
python scraper.py --scope=featured     # featured scope (14 channels, flash, mega pages, personalize)
python scraper.py --scope=full         # all 1232 leaf categories (~600k SKUs, slow)
```

Or use the launcher (double-click):

```bat
runall.bat scraper     # scrape only
runall.bat dashbrd     # start dashboard only (serves on http://localhost:3000)
runall.bat both        # scrape, then open the dashboard  (default with no argument)
```

Options:

```bash
python scraper.py --scope=featured --quiet --concurrency=6 --pages=5
```

- `--scope=featured|full` — featured (fast daily) or full catalog.
- `--pages=N` — page cap per category (0 = unlimited; featured defaults to 5).
- `--concurrency=N` — parallel requests (default 6).
- `--quiet` — suppress per-request logging.

Output lands in `data/` and is committed by the GitHub Actions workflow (`daily.yml`, scheduled 23:15 UTC).

## API notes

The mobile API at `api.cartup.com` uses a per-request token flow:

1. Unauthenticated request → `401` with header `cf-ray-status-id-tn` containing base64 JSON `{"expires","sign","random"}`.
2. Double base64-encode that token (`btoa(btoa(token))`) and send it back as the `sxsrf` header.
3. Subsequent requests succeed. Required headers: `user-agent: Dart/3.11 (dart:io)`, `origin: cartup-prod`, `isapp: 1`, `accept-encoding: gzip`.

The same handshake is used by the cartup.com web app (`sxsrf = btoa(btoa(localStorage['cf-ray-status-id-tn']))`). `scraper.py` implements it transparently.

## Development

```bash
runall.bat dashbrd    # serve on http://localhost:3000
```

Open the served `index.html`. `app.js` is dependency-free (vanilla JS + SVG charts).

## Disclaimer

Unofficial project for personal price tracking. Data and API are property of CartUp; use responsibly.
