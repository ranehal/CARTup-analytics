#!/usr/bin/env python3
"""CartUp price tracker scraper.

Snapshots products (prices/stock) from the CartUp mobile API and appends
YYYY-MM-DD:price entries to data/history.json, writes a delta to
data/daily/YYYY-MM-DD.json and meta.json.

Usage:
  python scraper.py --scope=featured [--quiet] [--concurrency=6] [--pages=5]
  python scraper.py --scope=full
"""
import argparse
import base64
import gzip
import json
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib import error, request

BASE = "https://api.cartup.com"
IMG = "https://sl-dev-s3.s3.amazonaws.com"

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
DAILY = DATA / "daily"
STATE = DATA / "state"


def cat(v):
    return str(v if v is not None else "").strip()


def num(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def wrap(s):
    return base64.b64encode(base64.b64encode(s.encode())).decode()


def img_of(f):
    f = cat(f)
    if not f:
        return ""
    if f.startswith("http") or f.startswith("/"):
        return f
    if "/" in f:
        return f"{IMG}/{f}"
    return f"{IMG}/product/{f}"


def slug_of(u):
    parts = cat(u).split("/")
    return parts[-1] if parts else ""


class Api:
    def __init__(self, concurrency=6, delay=0.05, quiet=False):
        self.concurrency = concurrency
        self.delay = delay
        self.quiet = quiet
        self.token = None
        self.calls = 0
        self.fails = 0
        self._lock = threading.Lock()

    def log(self, label, value):
        if not self.quiet:
            print(f"  {label}: {value}")

    def _open(self, url):
        headers = {
            "user-agent": "Dart/3.11 (dart:io)",
            "origin": "cartup-prod",
            "isapp": "1",
            "accept-encoding": "gzip",
        }
        if self.token:
            headers["sxsrf"] = self.token

        def do():
            return request.urlopen(request.Request(url, headers=headers), timeout=30)

        try:
            resp = do()
        except error.HTTPError as e:
            if e.code != 401:
                raise
            t = e.headers.get("cf-ray-status-id-tn")
            if not t:
                raise
            with self._lock:
                self.token = wrap(t)
            headers["sxsrf"] = self.token
            resp = do()

        data = resp.read()
        if resp.headers.get("Content-Encoding", "") == "gzip":
            data = gzip.decompress(data)
        return data

    def get_json(self, path):
        url = path if path.startswith("http") else BASE + path
        data = self._open(url)
        with self._lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        j = json.loads(data)
        if isinstance(j, dict) and "success" in j and not j["success"]:
            raise RuntimeError(f"API error {j.get('code', '')} {url}")
        return j

    def paginate(self, url_for, page_info, items, max_pages=100000, concurrency=3, label=""):
        first = self.get_json(url_for(1))
        pi = page_info(first) or {}
        total = int(pi.get("totalPageCount", 1) or 1)
        pages = max(1, min(total, max_pages))
        all_items = list(items(first) or [])
        if pages > 1:
            n = min(concurrency, pages - 1)

            def fetch(p):
                return self.get_json(url_for(p))

            with ThreadPoolExecutor(max_workers=n) as ex:
                for p in range(2, pages + 1):
                    j = ex.submit(fetch, p).result()
                    arr = items(j)
                    if arr:
                        all_items.extend(arr)
        if label:
            self.log(label, f"{len(all_items)} items / {pages} pages")
        return all_items


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------
def normalize_category(it, path_name, leaf_name):
    p = num(it.get("discountedPrice"))
    m = num(it.get("price"))
    pid = it.get("id") or it.get("productId")
    if not pid:
        return None
    if it.get("isVariantAvailable") is False:
        st = False
    else:
        st = num(it.get("currentStockQty")) > 0
    return {
        "i": pid,
        "n": cat(it.get("name")),
        "c": path_name,
        "t": path_name.split(" > ")[0],
        "sc": leaf_name,
        "img": cat(it.get("thumbnail")),
        "p": p or m,
        "m": m,
        "d": num(it.get("discountPercentage")),
        "st": st,
        "rt": float(it.get("ratings") or 0),
        "rc": num(it.get("totalRating")),
        "sl": cat(it.get("slug")),
        "fs": bool(it.get("isFreeShippingApplied")),
        "_cat": True,
    }


def normalize_flash(it):
    if not it.get("productId"):
        return None
    p = num(it.get("discountedPrice"))
    m = num(it.get("price"))
    return {
        "i": it.get("productId"),
        "n": cat(it.get("name")),
        "img": cat(it.get("productVariantThumbnail") or it.get("thumbnail")),
        "p": p or m,
        "m": m,
        "d": num(it.get("discountPercentage")),
        "st": num(it.get("remainingSlotStock")) > 0,
        "rt": 0.0,
        "rc": 0,
        "sl": cat(it.get("slug")),
        "fs": bool(it.get("isFreeShippingApplied")),
    }


def normalize_builder(it):
    if not it.get("productId"):
        return None
    p = num(it.get("productDiscountedPrice"))
    m = num(it.get("productPrice"))
    if it.get("productIsVariantAvailable") is False:
        st = False
    else:
        st = num(it.get("productCurrentStockQty")) > 0
    return {
        "i": it.get("productId"),
        "n": cat(it.get("productName")),
        "img": cat(it.get("productThumbnail") or it.get("productVariantThumbnail")),
        "p": p or m,
        "m": m,
        "d": num(it.get("productDiscountPercentage")),
        "st": st,
        "rt": float(it.get("productAvgRating") or 0),
        "rc": num(it.get("productTotalRating")),
        "sl": cat(it.get("productSlug")),
        "fs": bool(it.get("isFreeShippingApplied")),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def read_json(f, fallback=None):
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def write_json(f, obj):
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(obj), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------
def fetch_tree(api):
    j = api.get_json("/product/api/v1/category/tree-v2")
    tree = j.get("data") or []
    by_link = {}
    leaves = []
    meta_cats = []

    def walk(arr, parent):
        for n in arr:
            node = {
                "id": n.get("id"),
                "name": cat(n.get("name")),
                "slug": cat(n.get("link")),
                "icon": cat(n.get("icon")),
                "parent": parent,
                "children": [],
            }
            by_link[str(node["slug"])] = node
            meta_cats.append(node)
            kids = n.get("children") or []
            if kids:
                walk(kids, n.get("id"))
            else:
                leaves.append(node)

    walk(tree, 0)
    for n in meta_cats:
        if n["parent"]:
            parent_node = by_link.get(str(n["parent"]))
            if parent_node:
                parent_node["children"].append(n["id"])
    return tree, leaves, by_link


def fetch_home(api):
    j = api.get_json("/product/api/v1/homepage-layouts/get-home-page-temp-data")
    init = (j.get("data") or {}).get("init") or {}
    channels = []
    for c in init.get("categories_channels") or []:
        s = slug_of(c.get("target"))
        if s and s != "home":
            channels.append({"name": cat(c.get("name")), "slug": s, "image": cat(c.get("image_url"))})

    def collect(arr):
        out = []
        for o in arr or []:
            s = slug_of(o.get("target"))
            if s:
                out.append({"slug": s, "image": cat(o.get("image_url")), "text": cat(o.get("text"))})
        return out

    return {
        "channels": channels,
        "mega": collect(init.get("mega_deals")),
        "offers": collect(init.get("offers_channels")),
        "pop": collect(init.get("pop_layers")),
    }


def fetch_flash(api, products, sections, add_product):
    cfg = api.get_json("/product/api/v1/homepage-layouts/get-flash-sale-layouts-config")
    for c in cfg.get("data") or []:
        campaign_id = c.get("campaignId")
        if not campaign_id:
            continue
        all_items = api.paginate(
            url_for=lambda p, _id=campaign_id: (
                f"/product/api/v1/flash-sales/get-all-product-list?currentPage={p}&rowsPerPage=20&sorting=0&sns_seed_data="
            ),
            page_info=lambda j: (j.get("data") or {}).get("pageInfo"),
            items=lambda j: (j.get("data") or {}).get("items") or [],
            label=f"flash products ({cat(c.get('title'))}#{campaign_id})",
        )
        home = (api.get_json(f"/product/api/v1/flash-sales/get-homepage-product-list/{campaign_id}").get("data") or [])
        sec = {
            "id": f"flash-{campaign_id}",
            "type": "flash",
            "name": cat(c.get("title")) or "Flash Sale",
            "campaignId": campaign_id,
            "image": img_of(c.get("bannerFilePath")),
            "productIds": [],
        }
        for it in list(all_items) + list(home):
            rec = normalize_flash(it)
            if not rec:
                continue
            sec["productIds"].append(rec["i"])
            add_product({**rec, "c": "Flash Sale", "t": "Flash Sale", "sc": "Flash Sale"}, sec["id"])
        sec["productIds"] = sorted(set(sec["productIds"]))
        sec["count"] = len(sec["productIds"])
        sections.append(sec)


def fetch_builder_page(api, slug, label, products, sections, add_product):
    try:
        j = api.get_json(
            f"/product/api/v1/static-builder-pages/sbp/seg/page-builder-details/get-builder-page-details-v2/{slug}"
        )
    except Exception:
        if not api.quiet:
            print(f"  skip builder page (not found): {slug}")
        return None
    d = j.get("data")
    if not d:
        return None
    sec = {
        "id": f"mega-{slug}",
        "type": "mega",
        "name": cat(d.get("name")) or label or cat(d.get("slug")),
        "slug": slug,
        "image": "",
        "productIds": [],
    }
    cols = [c for r in d.get("rows") or [] for c in r.get("columns") or []]
    tb = next((c for c in cols if c.get("topBanners")), None)
    top_banner = (tb.get("topBanners") or [{}])[0] if tb else {}
    bcol = next((c for c in cols if c.get("banners")), None)
    banner = (bcol.get("banners") or [{}])[0] if bcol else {}
    sec["image"] = img_of(top_banner.get("appBannerUrl") or top_banner.get("desktopBannerUrl") or banner.get("bannerFile") or d.get("thumbnail"))
    prod_cols = [c.get("id") for c in cols if c.get("contentType") == "Product"]
    for col_id in prod_cols:
        items = api.paginate(
            url_for=lambda p, _id=col_id: (
                f"/product/api/v1/static-builder-pages/sbp/seg/page-builder-details/get-static-builder-page-products-info-v2?builder_column_id={_id}&sns_seed_data=&current_page={p}&rowsPerPage=20"
            ),
            page_info=lambda j: (j.get("data") or {}).get("pageInfo"),
            items=lambda j: (j.get("data") or {}).get("items") or [],
            label=f"builder {slug} col {col_id}",
        )
        for it in items:
            rec = normalize_builder(it)
            if not rec:
                continue
            sec["productIds"].append(rec["i"])
            add_product({**rec, "c": sec["name"], "t": sec["name"], "sc": sec["name"]}, sec["id"])
    sec["productIds"] = sorted(set(sec["productIds"]))
    sec["count"] = len(sec["productIds"])
    sections.append(sec)
    return sec


def fetch_category(api, node, path_name, cat_pages):
    items = api.paginate(
        url_for=lambda p, s=node["slug"]: (
            f"/product/api/v1/product/product-stock/get-category-slug-wise/{s}?currentPage={p}&statusId=1&rowsPerPage=40"
        ),
        page_info=lambda j: (j.get("data") or {}).get("pageInfo"),
        items=lambda j: (j.get("data") or {}).get("items") or [],
        max_pages=cat_pages,
        label=f"category {node['slug']} ({path_name})",
    )
    return items


def fetch_personalize(api):
    return api.paginate(
        url_for=lambda p: f"/product/api/v1/personalize-product/get-products?currentPage={p}&rowsPerPage=50",
        page_info=lambda j: (j.get("data") or {}).get("pageInfo"),
        items=lambda j: (j.get("data") or {}).get("items") or [],
        label="personalize/recommended",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="CartUp price snapshot scraper")
    parser.add_argument("--scope", choices=["featured", "full"], default="featured")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--pages", type=int, default=None, help="page cap per category (0 = unlimited)")
    args = parser.parse_args()

    day = (datetime.now(timezone(timedelta(hours=6)))).strftime("%Y-%m-%d")
    cat_pages = args.pages if args.pages is not None else (0 if args.scope == "full" else 5)
    api = Api(concurrency=args.concurrency, quiet=args.quiet)

    print(
        f"CartUp scraper  ·  scope={args.scope}  ·  day={day}  ·  "
        f"concurrency={args.concurrency}  ·  pages/category={'unlimited' if cat_pages == 0 else cat_pages}"
    )

    products = {}
    sections = []

    def add_product(rec, sec_id=None):
        pid = rec["i"]
        cur = products.get(pid)
        if cur is None:
            rec = {**rec, "sec": []}
            products[pid] = rec
            cur = rec
        else:
            if rec.get("_cat"):
                cur["c"] = rec["c"]
                cur["t"] = rec["t"]
                cur["sc"] = rec["sc"]
            if not cur.get("n"):
                cur["n"] = rec["n"]
            if not cur.get("img"):
                cur["img"] = rec["img"]
            if not cur.get("p"):
                cur["p"] = rec["p"]
                cur["m"] = rec["m"]
                cur["d"] = rec["d"]
                cur["st"] = rec["st"]
            if rec.get("fs"):
                cur["fs"] = True
            if rec.get("rt", 0) > cur.get("rt", 0):
                cur["rt"] = rec["rt"]
                cur["rc"] = rec["rc"]
        if sec_id and sec_id not in cur["sec"]:
            cur["sec"].append(sec_id)

    tree, leaves, by_link = fetch_tree(api)
    api.log("category tree", f"{len(tree)} top-level, {len(leaves)} leaf")
    home = fetch_home(api)
    api.log("home channels", len(home["channels"]))
    api.log("mega/offer/pop targets", f"{len(home['mega'])}/{len(home['offers'])}/{len(home['pop'])}")

    fetch_flash(api, products, sections, add_product)
    section_slugs = []
    for o in home["mega"] + home["offers"] + home["pop"]:
        if o["slug"] not in section_slugs:
            section_slugs.append(o["slug"])
    for s in section_slugs:
        label = next((o["text"] for o in home["mega"] if o["slug"] == s), None)
        fetch_builder_page(api, s, label, products, sections, add_product)

    for it in fetch_personalize(api):
        rec = normalize_category(it, "For You", "For You")
        if rec:
            add_product({**rec, "c": "For You", "t": "For You", "sc": "For You"})

    if args.scope == "full":
        api.log("scope=full", f"scraping all {len(leaves)} leaf categories")

        def path_of(node):
            path = []
            cur = node
            while cur:
                path.insert(0, cur["name"])
                cur = by_link.get(str(cur.get("parent")))
            return " > ".join(path)

        jobs = [{"node": n, "path": path_of(n)} for n in leaves]
        idx = 0
        idx_lock = threading.Lock()

        def worker():
            nonlocal idx
            while True:
                with idx_lock:
                    if idx >= len(jobs):
                        return
                    job = jobs[idx]
                    idx += 1
                try:
                    items = fetch_category(api, job["node"], job["path"], cat_pages)
                    for it in items:
                        rec = normalize_category(it, job["path"], job["node"]["name"])
                        if rec:
                            add_product(rec)
                except Exception:
                    pass

        threads = [threading.Thread(target=worker) for _ in range(min(args.concurrency, 4))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        api.log("scope=featured", f"scraping {len(home['channels'])} curated channels")
        for ch in home["channels"]:
            node = by_link.get(ch["slug"]) or {"name": ch["name"], "slug": ch["slug"]}
            try:
                items = fetch_category(api, node, ch["name"], cat_pages)
                for it in items:
                    rec = normalize_category(it, ch["name"], node["name"])
                    if rec:
                        add_product(rec)
            except Exception:
                pass

    lst = []
    for r in products.values():
        rec = {k: v for k, v in r.items() if k != "_cat"}
        rec["sec"] = list(r["sec"])
        lst.append(rec)

    print(f"\nScraped {len(lst)} unique products, {len(sections)} sections, {api.calls} requests")

    DAILY.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    prev_state = read_json(STATE / "last.json", {})
    history = read_json(DATA / "history.json", {})
    state_map = {str(r["i"]): [r["p"], r["m"], 1 if r["st"] else 0] for r in lst}
    daily = {}
    new_count = 0
    changed_count = 0
    for pid, vals in state_map.items():
        was = prev_state.get(pid)
        if not was:
            daily[pid] = vals
            new_count += 1
        elif was[0] != vals[0] or was[1] != vals[1] or (1 if was[2] else 0) != vals[2]:
            daily[pid] = vals
            changed_count += 1

    for pid, (p, _, _) in state_map.items():
        prev = history.get(pid)
        if prev:
            parts = prev.split(",")
            ld, lp = parts[-1].split(":")
            if ld == day:
                if int(lp) != p:
                    parts[-1] = f"{day}:{p}"
            elif int(lp) != p:
                parts.append(f"{day}:{p}")
            history[pid] = ",".join(parts)
        else:
            history[pid] = f"{day}:{p}"

    if daily:
        write_json(DAILY / f"{day}.json", daily)
    write_json(STATE / "last.json", state_map)

    meta = {
        "source": BASE,
        "scope": args.scope,
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "day": day,
        "stats": {
            "products": len(lst),
            "sections": len(sections),
            "categories": len(tree),
            "newToday": new_count,
            "changedToday": changed_count,
            "requests": api.calls,
        },
        "channels": home["channels"],
        "sections": sections,
        "categories": tree,
    }

    write_json(DATA / "products.json", lst)
    write_json(DATA / "history.json", history)
    write_json(DATA / "meta.json", meta)
    size_mb = (DATA / "products.json").stat().st_size / 1e6
    print(f"Wrote data/products.json ({size_mb:.1f} MB), data/history.json, data/meta.json, data/daily/{day}.json")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
