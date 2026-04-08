"""
RotaktWeb — autonomous daily auditor for rotakt.ro & micromobilitate.ro

Pulls full product catalog via WooCommerce Store API (public, no auth),
counts active products and flags products with missing/empty description.

Outputs:
  data/snapshot_<site>_YYYY-MM-DD.json   (raw catalog snapshot per site)
  data/history.json                       (rolling time series)
  reports/report_YYYY-MM-DD.md            (daily markdown report)

Run: python rotakt_web_agent.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
HISTORY_FILE = DATA_DIR / "history.json"

SITES = {
    "rotakt.ro": "https://www.rotakt.ro",
    "micromobilitate.ro": "https://www.micromobilitate.ro",
}

# Threshold (chars after stripping HTML) below which a description is "missing"
MIN_DESCRIPTION_CHARS = 20

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


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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
            import time
            time.sleep(wait)
    raise last_exc  # type: ignore[misc]


def fetch_all_products(base_url: str) -> list[dict]:
    """Paginates through WooCommerce Store API and returns all products."""
    products: list[dict] = []
    page = 1
    with httpx.Client(
        timeout=TIMEOUT,
        headers=DEFAULT_HEADERS,
        http2=False,
        follow_redirects=True,
    ) as client:
        while True:
            url = f"{base_url}/wp-json/wc/store/v1/products"
            params = {"per_page": PER_PAGE, "page": page}
            r = _get_with_retries(client, url, params)
            if r.status_code == 400 and page > 1:
                # Woo returns 400 "rest_product_invalid_page_number" past the last page
                break
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            products.extend(batch)
            total_pages = int(r.headers.get("X-WP-TotalPages", "0") or 0)
            if total_pages and page >= total_pages:
                break
            page += 1
            if page > 400:  # safety stop
                break
    return products


def analyze(products: Iterable[dict]) -> dict:
    products = list(products)
    missing: list[dict] = []
    in_stock = 0
    out_of_stock = 0
    for p in products:
        desc_text = strip_html(p.get("description"))
        short_text = strip_html(p.get("short_description"))
        if len(desc_text) < MIN_DESCRIPTION_CHARS:
            missing.append(
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "sku": p.get("sku"),
                    "permalink": p.get("permalink"),
                    "description_chars": len(desc_text),
                    "short_description_chars": len(short_text),
                }
            )
        if p.get("is_in_stock"):
            in_stock += 1
        else:
            out_of_stock += 1
    return {
        "total": len(products),
        "in_stock": in_stock,
        "out_of_stock": out_of_stock,
        "missing_description_count": len(missing),
        "missing_description": missing,
    }


def save_snapshot(site: str, date_str: str, products: list[dict], analysis: dict) -> Path:
    path = DATA_DIR / f"snapshot_{site}_{date_str}.json"
    payload = {
        "site": site,
        "date": date_str,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "analysis": analysis,
        "product_ids": [p.get("id") for p in products],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update_history(date_str: str, results: dict[str, dict]) -> None:
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = []
    entry = {
        "date": date_str,
        "sites": {
            site: {
                "total": a["total"],
                "in_stock": a["in_stock"],
                "out_of_stock": a["out_of_stock"],
                "missing_description_count": a["missing_description_count"],
            }
            for site, a in results.items()
        },
    }
    # Replace any existing entry for the same date
    history = [h for h in history if h.get("date") != date_str]
    history.append(entry)
    history.sort(key=lambda h: h["date"])
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def previous_totals(date_str: str) -> dict[str, dict]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    prior = [h for h in history if h.get("date") < date_str]
    if not prior:
        return {}
    return prior[-1].get("sites", {})


def render_report(date_str: str, results: dict[str, dict]) -> Path:
    prev = previous_totals(date_str)
    lines = [
        f"# RotaktWeb — Raport zilnic {date_str}",
        "",
        f"_Generat: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
        "## Sumar",
        "",
        "| Site | Produse | Stoc | Fără stoc | Fără descriere | Δ vs. ultimul rulaj |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for site, a in results.items():
        delta = ""
        if site in prev:
            d = a["total"] - prev[site].get("total", 0)
            delta = f"{d:+d}" if d else "0"
        lines.append(
            f"| {site} | {a['total']} | {a['in_stock']} | {a['out_of_stock']} "
            f"| {a['missing_description_count']} | {delta} |"
        )
    lines.append("")

    for site, a in results.items():
        lines.append(f"## {site} — produse fără descriere ({a['missing_description_count']})")
        lines.append("")
        if not a["missing_description"]:
            lines.append("_Toate produsele au descriere._")
            lines.append("")
            continue
        lines.append("| ID | SKU | Nume | Chars desc | Chars short |")
        lines.append("|---:|---|---|---:|---:|")
        for m in a["missing_description"][:200]:
            name = (m.get("name") or "").replace("|", "\\|")
            link = m.get("permalink") or ""
            sku = m.get("sku") or ""
            lines.append(
                f"| {m['id']} | {sku} | [{name}]({link}) "
                f"| {m['description_chars']} | {m['short_description_chars']} |"
            )
        if len(a["missing_description"]) > 200:
            lines.append("")
            lines.append(f"_…{len(a['missing_description']) - 200} produse suplimentare omise._")
        lines.append("")

    path = REPORTS_DIR / f"report_{date_str}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


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
            print(f"[{site}] ERROR: {e}", file=sys.stderr)
            continue
        analysis = analyze(products)
        save_snapshot(site, date_str, products, analysis)
        results[site] = analysis
        print(
            f"[{site}] total={analysis['total']} "
            f"in_stock={analysis['in_stock']} "
            f"missing_desc={analysis['missing_description_count']}",
            flush=True,
        )

    if not results:
        print("No results — aborting.", file=sys.stderr)
        return 1

    update_history(date_str, results)
    report_path = render_report(date_str, results)
    write_latest(date_str, results)
    print(f"Report: {report_path}")
    return 0


def write_latest(date_str: str, results: dict[str, dict]) -> Path:
    """Single consolidated JSON consumed by the marketing dashboard."""
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date": date_str,
        "sites": {
            site: {
                "total": a["total"],
                "in_stock": a["in_stock"],
                "out_of_stock": a["out_of_stock"],
                "missing_description_count": a["missing_description_count"],
                "missing_description": a["missing_description"],
            }
            for site, a in results.items()
        },
    }
    path = DATA_DIR / "latest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
