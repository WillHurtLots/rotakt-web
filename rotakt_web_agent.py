"""
RotaktWeb — autonomous daily auditor for rotakt.ro & micromobilitate.ro

Pulls full product catalog via WooCommerce Store API (public, no auth),
runs comprehensive health checks, and emits a Marketing dashboard payload.

Checks per product:
  - missing description (HTML stripped < 20 chars)
  - missing / placeholder image
  - missing or zero price
  - missing brand assignment
  - low stock (low_stock_remaining > 0)
  - SEO issues (ugly slug or short name)

Catalog-level checks:
  - empty categories (count == 0)
  - daily diff vs previous snapshot (added / removed products)

Outputs:
  data/snapshot_<site>_YYYY-MM-DD.json   raw catalog snapshot per site
  data/history.json                       rolling KPI time series
  data/latest.json                        consolidated payload for dashboard
  data/alerts.json                        alert summary for notifications
  reports/report_YYYY-MM-DD.md            human-readable markdown report
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
HISTORY_FILE = DATA_DIR / "history.json"
LATEST_FILE = DATA_DIR / "latest.json"
ALERTS_FILE = DATA_DIR / "alerts.json"

SITES = {
    "rotakt.ro": "https://www.rotakt.ro",
    "micromobilitate.ro": "https://www.micromobilitate.ro",
}

# Health thresholds
MIN_DESCRIPTION_CHARS = 20
SHORT_NAME_CHARS = 8
MAX_SLUG_CHARS = 80
PLACEHOLDER_HINTS = ("placeholder", "woocommerce-placeholder")

# HTTP
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
}
PER_PAGE = 50
TIMEOUT = 90.0
MAX_RETRIES = 4
RETRY_BACKOFF = 3.0


# ─────────────────────────────  helpers  ────────────────────────────────


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def lite(p: dict, **extra) -> dict:
    base = {
        "id": p.get("id"),
        "name": p.get("name"),
        "sku": p.get("sku") or "",
        "permalink": p.get("permalink"),
    }
    base.update(extra)
    return base


# ─────────────────────────────  HTTP  ───────────────────────────────────


def _get_with_retries(client: httpx.Client, url: str, params: dict) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.get(url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_exc = e
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF * attempt
            print(f"  retry {attempt}/{MAX_RETRIES - 1} after {wait}s ({type(e).__name__})", flush=True)
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT,
        headers=DEFAULT_HEADERS,
        http2=False,
        follow_redirects=True,
    )


def fetch_all_products(base_url: str) -> list[dict]:
    products: list[dict] = []
    page = 1
    with _client() as client:
        while True:
            url = f"{base_url}/wp-json/wc/store/v1/products"
            r = _get_with_retries(client, url, {"per_page": PER_PAGE, "page": page})
            if r.status_code == 400 and page > 1:
                break  # past last page
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            products.extend(batch)
            total_pages = int(r.headers.get("X-WP-TotalPages", "0") or 0)
            if total_pages and page >= total_pages:
                break
            page += 1
            if page > 400:
                break
    return products


def fetch_categories(base_url: str) -> list[dict]:
    cats: list[dict] = []
    page = 1
    with _client() as client:
        while True:
            url = f"{base_url}/wp-json/wc/store/v1/products/categories"
            r = _get_with_retries(
                client, url, {"per_page": 100, "page": page, "hide_empty": "false"}
            )
            if r.status_code == 400 and page > 1:
                break
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            cats.extend(batch)
            total_pages = int(r.headers.get("X-WP-TotalPages", "0") or 0)
            if total_pages and page >= total_pages:
                break
            page += 1
            if page > 50:
                break
    return cats


# ─────────────────────────────  checks  ─────────────────────────────────


def check_description(p: dict) -> dict | None:
    desc = strip_html(p.get("description"))
    if len(desc) < MIN_DESCRIPTION_CHARS:
        return lite(
            p,
            description_chars=len(desc),
            short_description_chars=len(strip_html(p.get("short_description"))),
        )
    return None


def check_image(p: dict) -> dict | None:
    images = p.get("images") or []
    if not images:
        return lite(p, reason="no_images")
    src = (images[0].get("src") or "").lower()
    if any(h in src for h in PLACEHOLDER_HINTS):
        return lite(p, reason="placeholder", image_url=images[0].get("src"))
    return None


def check_price(p: dict) -> dict | None:
    prices = p.get("prices") or {}
    raw = (prices.get("price") or "").strip()
    try:
        val = int(raw) if raw else 0
    except ValueError:
        val = 0
    if val == 0:
        currency = prices.get("currency_code") or ""
        return lite(p, raw_price=raw, currency=currency)
    return None


def check_brand(p: dict) -> dict | None:
    brands = p.get("brands") or []
    if not brands:
        return lite(p)
    return None


def check_low_stock(p: dict) -> dict | None:
    remaining = p.get("low_stock_remaining")
    if remaining is not None and isinstance(remaining, (int, float)) and remaining > 0:
        return lite(p, remaining=remaining)
    return None


SLUG_OK = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SLUG_NUMERIC = re.compile(r"^[\d-]+$")


def check_seo(p: dict) -> dict | None:
    issues = []
    name = (p.get("name") or "").strip()
    slug = (p.get("slug") or "").strip()
    if len(name) < SHORT_NAME_CHARS:
        issues.append(f"name<{SHORT_NAME_CHARS}")
    if not slug:
        issues.append("slug-missing")
    else:
        if len(slug) > MAX_SLUG_CHARS:
            issues.append(f"slug>{MAX_SLUG_CHARS}")
        if SLUG_NUMERIC.match(slug):
            issues.append("slug-numeric-only")
        elif not SLUG_OK.match(slug):
            issues.append("slug-non-canonical")
    if issues:
        return lite(p, issues=issues, slug=slug, name_chars=len(name))
    return None


CHECKS = [
    ("missing_description", check_description),
    ("missing_image", check_image),
    ("missing_price", check_price),
    ("missing_brand", check_brand),
    ("low_stock", check_low_stock),
    ("seo_issues", check_seo),
]


# ─────────────────────────────  analysis  ───────────────────────────────


def analyze(products: Iterable[dict]) -> dict:
    products = list(products)
    in_stock = sum(1 for p in products if p.get("is_in_stock"))
    out_of_stock = len(products) - in_stock

    checks: dict[str, list[dict]] = {name: [] for name, _ in CHECKS}
    products_with_any_issue: set = set()
    for p in products:
        for name, fn in CHECKS:
            issue = fn(p)
            if issue is not None:
                checks[name].append(issue)
                products_with_any_issue.add(p.get("id"))

    health_score = 0
    if products:
        clean = len(products) - len(products_with_any_issue)
        health_score = round((clean / len(products)) * 100)

    return {
        "total": len(products),
        "in_stock": in_stock,
        "out_of_stock": out_of_stock,
        "products_with_issues": len(products_with_any_issue),
        "health_score": health_score,
        "checks": checks,
    }


def analyze_categories(categories: list[dict]) -> dict:
    sorted_cats = sorted(categories, key=lambda c: c.get("count", 0) or 0, reverse=True)
    empty = [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "slug": c.get("slug"),
            "permalink": c.get("permalink"),
        }
        for c in categories
        if (c.get("count") or 0) == 0
    ]
    return {
        "total": len(categories),
        "empty_count": len(empty),
        "empty": empty,
        "top": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "count": c.get("count") or 0,
                "permalink": c.get("permalink"),
            }
            for c in sorted_cats[:15]
        ],
    }


# ─────────────────────────────  diff  ──────────────────────────────────


def load_previous_lite(site: str, today_str: str) -> list[dict] | None:
    files = sorted(DATA_DIR.glob(f"snapshot_{site}_*.json"))
    for f in reversed(files):
        if f.stem.endswith(today_str):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if "products_lite" in data:
            return data["products_lite"]
        if "product_ids" in data:
            # legacy snapshots — only IDs available
            return [{"id": pid, "name": None, "permalink": None} for pid in data["product_ids"]]
    return None


def compute_diff(current: list[dict], previous: list[dict] | None) -> dict:
    if previous is None:
        return {"baseline": True, "added": [], "removed": []}
    cur_ids = {p.get("id") for p in current}
    prev_by_id = {p.get("id"): p for p in previous}
    prev_ids = set(prev_by_id.keys())
    added_ids = cur_ids - prev_ids
    removed_ids = prev_ids - cur_ids
    cur_by_id = {p.get("id"): p for p in current}
    added = [
        {
            "id": pid,
            "name": cur_by_id[pid].get("name"),
            "permalink": cur_by_id[pid].get("permalink"),
            "sku": cur_by_id[pid].get("sku") or "",
        }
        for pid in added_ids
    ]
    removed = [
        {
            "id": pid,
            "name": prev_by_id[pid].get("name"),
            "permalink": prev_by_id[pid].get("permalink"),
            "sku": prev_by_id[pid].get("sku", "") if isinstance(prev_by_id[pid], dict) else "",
        }
        for pid in removed_ids
    ]
    return {"baseline": False, "added": added, "removed": removed}


# ─────────────────────────────  persistence  ────────────────────────────


def save_snapshot(
    site: str,
    date_str: str,
    products: list[dict],
    analysis: dict,
    categories: dict,
    diff: dict,
) -> Path:
    path = DATA_DIR / f"snapshot_{site}_{date_str}.json"
    payload = {
        "site": site,
        "date": date_str,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "analysis": analysis,
        "categories": categories,
        "diff": diff,
        "products_lite": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "sku": p.get("sku") or "",
                "permalink": p.get("permalink"),
            }
            for p in products
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update_history(date_str: str, results: dict[str, dict]) -> None:
    history: list[dict] = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = []
    entry = {
        "date": date_str,
        "sites": {
            site: {
                "total": r["analysis"]["total"],
                "in_stock": r["analysis"]["in_stock"],
                "out_of_stock": r["analysis"]["out_of_stock"],
                "health_score": r["analysis"]["health_score"],
                "products_with_issues": r["analysis"]["products_with_issues"],
                "missing_description_count": len(r["analysis"]["checks"]["missing_description"]),
                "missing_image_count": len(r["analysis"]["checks"]["missing_image"]),
                "missing_price_count": len(r["analysis"]["checks"]["missing_price"]),
                "missing_brand_count": len(r["analysis"]["checks"]["missing_brand"]),
                "low_stock_count": len(r["analysis"]["checks"]["low_stock"]),
                "seo_issues_count": len(r["analysis"]["checks"]["seo_issues"]),
                "categories_total": r["categories"]["total"],
                "categories_empty": r["categories"]["empty_count"],
            }
            for site, r in results.items()
        },
    }
    history = [h for h in history if h.get("date") != date_str]
    history.append(entry)
    history.sort(key=lambda h: h["date"])
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def write_latest(date_str: str, results: dict[str, dict]) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date": date_str,
        "sites": {
            site: {
                "total": r["analysis"]["total"],
                "in_stock": r["analysis"]["in_stock"],
                "out_of_stock": r["analysis"]["out_of_stock"],
                "health_score": r["analysis"]["health_score"],
                "products_with_issues": r["analysis"]["products_with_issues"],
                "checks": r["analysis"]["checks"],
                "categories": r["categories"],
                "diff": r["diff"],
            }
            for site, r in results.items()
        },
    }
    LATEST_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_alerts(date_str: str, results: dict[str, dict]) -> dict:
    summary = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sites": {},
        "total_alerts": 0,
    }
    for site, r in results.items():
        a = r["analysis"]
        site_alerts = {
            "missing_description": len(a["checks"]["missing_description"]),
            "missing_image": len(a["checks"]["missing_image"]),
            "missing_price": len(a["checks"]["missing_price"]),
            "missing_brand": len(a["checks"]["missing_brand"]),
            "low_stock": len(a["checks"]["low_stock"]),
            "seo_issues": len(a["checks"]["seo_issues"]),
            "empty_categories": r["categories"]["empty_count"],
            "added_today": 0 if r["diff"].get("baseline") else len(r["diff"]["added"]),
            "removed_today": 0 if r["diff"].get("baseline") else len(r["diff"]["removed"]),
        }
        summary["sites"][site] = site_alerts
        summary["total_alerts"] += sum(
            v for k, v in site_alerts.items() if k not in ("added_today", "removed_today")
        )
    ALERTS_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


# ─────────────────────────────  report  ────────────────────────────────


def render_report(date_str: str, results: dict[str, dict]) -> Path:
    lines = [
        f"# RotaktWeb — Raport zilnic {date_str}",
        "",
        f"_Generat: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        "## Sumar",
        "",
        "| Site | Total | În stoc | Health | Produse cu probleme | Categorii goale |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for site, r in results.items():
        a = r["analysis"]
        lines.append(
            f"| {site} | {a['total']} | {a['in_stock']} | {a['health_score']}% "
            f"| {a['products_with_issues']} | {r['categories']['empty_count']} |"
        )
    lines.append("")

    for site, r in results.items():
        a = r["analysis"]
        lines.append(f"## {site}")
        lines.append("")
        lines.append("### Probleme detectate")
        lines.append("")
        lines.append("| Verificare | Număr |")
        lines.append("|---|---:|")
        for check_name, items in a["checks"].items():
            lines.append(f"| {check_name} | {len(items)} |")
        lines.append(f"| empty_categories | {r['categories']['empty_count']} |")
        lines.append("")

        d = r["diff"]
        if d.get("baseline"):
            lines.append("_Diff zilnic: baseline (prima rulare)._")
        else:
            lines.append(f"### Modificări vs. ultimul rulaj")
            lines.append("")
            lines.append(f"- Produse noi: **{len(d['added'])}**")
            lines.append(f"- Produse dispărute: **{len(d['removed'])}**")
        lines.append("")

    path = REPORTS_DIR / f"report_{date_str}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ─────────────────────────────  main  ───────────────────────────────────


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results: dict[str, dict] = {}
    for site, base in SITES.items():
        print(f"[{site}] fetching products…", flush=True)
        try:
            products = fetch_all_products(base)
        except httpx.HTTPError as e:
            print(f"[{site}] ERROR products: {e}", file=sys.stderr)
            continue

        print(f"[{site}] fetching categories…", flush=True)
        try:
            cats_raw = fetch_categories(base)
        except httpx.HTTPError as e:
            print(f"[{site}] ERROR categories: {e}", file=sys.stderr)
            cats_raw = []

        analysis = analyze(products)
        categories = analyze_categories(cats_raw)
        previous = load_previous_lite(site, date_str)
        diff = compute_diff(products, previous)

        save_snapshot(site, date_str, products, analysis, categories, diff)
        results[site] = {"analysis": analysis, "categories": categories, "diff": diff}

        print(
            f"[{site}] total={analysis['total']} health={analysis['health_score']}% "
            f"issues={analysis['products_with_issues']} cats={categories['total']} "
            f"empty_cats={categories['empty_count']} "
            f"added={len(diff['added'])} removed={len(diff['removed'])}",
            flush=True,
        )

    if not results:
        print("No results — aborting.", file=sys.stderr)
        return 1

    update_history(date_str, results)
    write_latest(date_str, results)
    summary = write_alerts(date_str, results)
    report_path = render_report(date_str, results)
    print(f"Report: {report_path}")
    print(f"Total alerts across all sites: {summary['total_alerts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
