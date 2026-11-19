"""FlexiSpot UK price tracker.

Loads a product page in headless Chromium (Playwright), reads the price the page
shows, appends it to a CSV history and reports whether it changed since the last
check. Sends Telegram / ntfy / macOS alerts on a price drop, when the --target
is reached, on stock changes and on promo changes (settings at the top of this file).

Usage:
    python3 tracker.py                          # one check
    python3 tracker.py --target 380             # alert when <= £380
    python3 tracker.py --every 30 --target 380  # keep checking every 30 min
    python3 tracker.py --test-alert             # check your alert setup

Price source, in order:
  1. The product JSON the page itself loads (/sapi/mall-item/item/detail):
     salePrc / originalPrc / outOfStock for each SKU.
  2. Fallback: the rendered price elements (.price-container / .original-save-prices).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# ALERT SETTINGS: paste your values between the quotes.
# Environment variables with the same names override these (used by GitHub Actions).
# Don't commit this file to a public repo with a real token in it.
TELEGRAM_BOT_TOKEN = ""   # from @BotFather, e.g. "123456789:AAH..."
TELEGRAM_CHAT_ID = ""     # your chat id, e.g. "987654321"
NTFY_TOPIC = ""           # optional: ntfy.sh topic name, e.g. "waffle-recliner-7f3k9q"
# ---------------------------------------------------------------------------

DEFAULT_URL = "https://flexispot.co.uk/power-swivel-rocker-recliner-with-waffle-back.html"
HERE = Path(__file__).resolve().parent
DEFAULT_HISTORY = HERE / "price_history.csv"
DETAIL_API = "/sapi/mall-item/item/detail"
FIELDS = ["checked_at", "url", "name", "sale_price", "original_price", "discount_pct",
          "in_stock", "promo_text", "source"]


@dataclass
class PriceSnapshot:
    checked_at: str
    url: str
    name: str
    sale_price: float | None
    original_price: float | None
    discount_pct: float | None
    in_stock: bool | None
    promo_text: str
    source: str  # "api" or "dom"


# ---------- parsing (pure functions, unit-tested) ----------

def parse_money(text: str | None) -> float | None:
    """'£1,419.99' -> 1419.99; returns None when no amount is present."""
    if not text:
        return None
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def discount_pct(sale: float | None, original: float | None) -> float | None:
    if sale is None or not original or original <= 0:
        return None
    return round((1 - sale / original) * 100, 1)


def parse_detail_json(payload: dict, url: str, now: str) -> PriceSnapshot | None:
    """Build a snapshot from the item/detail API response the page loads."""
    data = (payload or {}).get("data") or {}
    skus = data.get("shopSkuList") or []
    if not skus:
        return None
    sku = skus[0]  # this product has a single SKU; take the first if there are several
    sale = (sku.get("salePrc") or {}).get("value")
    orig = (sku.get("originalPrc") or {}).get("value")
    sale = round(float(sale), 2) if sale is not None else None
    orig = round(float(orig), 2) if orig is not None else None
    item = data.get("itemRenderTO") or {}
    promo = item.get("secKillFormalDescription") or "" if item.get("secKillFlag") else ""
    return PriceSnapshot(
        checked_at=now, url=url,
        name=item.get("itemName") or sku.get("name") or "",
        sale_price=sale, original_price=orig, discount_pct=discount_pct(sale, orig),
        in_stock=(not sku["outOfStock"]) if "outOfStock" in sku else None,
        promo_text=promo, source="api",
    )


def parse_dom_prices(sale_text: str, original_text: str, name: str, url: str,
                     now: str) -> PriceSnapshot | None:
    sale, orig = parse_money(sale_text), parse_money(original_text)
    if sale is None:
        return None
    return PriceSnapshot(checked_at=now, url=url, name=name, sale_price=sale,
                         original_price=orig, discount_pct=discount_pct(sale, orig),
                         in_stock=None, promo_text="", source="dom")


# ---------- history ----------

def last_snapshot(history: Path, url: str) -> dict | None:
    if not history.exists():
        return None
    last = None
    with history.open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("url") == url and row.get("sale_price"):
                last = row
    return last


def append_snapshot(history: Path, snap: PriceSnapshot) -> None:
    new = not history.exists()
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(asdict(snap))


def describe_change(prev: dict | None, snap: PriceSnapshot) -> tuple[str, float | None]:
    """Returns (message, delta). delta < 0 means the price dropped."""
    if prev is None:
        return "first check", None
    delta = round(snap.sale_price - float(prev["sale_price"]), 2)
    if delta == 0:
        return f"unchanged since {prev['checked_at']}", 0.0
    word = "DROPPED" if delta < 0 else "rose"
    return f"{word} by £{abs(delta):.2f} (was £{float(prev['sale_price']):.2f} at {prev['checked_at']})", delta


def alerts_for(prev: dict | None, snap: PriceSnapshot, delta: float | None,
               target: float | None) -> list[str]:
    """Reasons to alert. Each fires once per change, not on every check."""
    reasons = []
    if delta is not None and delta < 0:
        reasons.append(f"Price dropped £{abs(delta):.2f} to £{snap.sale_price:.2f}")
    if target is not None and snap.sale_price <= target:
        prev_price = float(prev["sale_price"]) if prev else None
        if prev_price is None or prev_price > target:
            reasons.append(f"At or below your target £{target:.2f}: now £{snap.sale_price:.2f}")
    if prev is not None and snap.in_stock is not None and prev.get("in_stock") in ("True", "False"):
        was = prev["in_stock"] == "True"
        if was != snap.in_stock:
            reasons.append("Back in stock" if snap.in_stock else "Went OUT OF STOCK")
    if prev is not None and snap.promo_text and snap.promo_text != (prev.get("promo_text") or ""):
        reasons.append(f"New promo: {snap.promo_text}")
    return reasons


def _post(url: str, data: bytes, headers: dict) -> None:
    import urllib.request
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def notify(title: str, message: str, url: str = "") -> list[str]:
    """Send to every configured channel. Returns the channels that succeeded.

    Configure with the ALERT SETTINGS at the top of this file, or environment
    variables of the same name (env wins):
      TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   -> Telegram bot message
      NTFY_TOPIC (+ optional NTFY_SERVER)     -> ntfy.sh push notification
    A macOS desktop notification is also shown when running on a Mac.
    """
    import os
    import urllib.parse
    sent = []
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or TELEGRAM_BOT_TOKEN
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID") or TELEGRAM_CHAT_ID
    if tg_token and tg_chat:
        try:
            body = urllib.parse.urlencode({
                "chat_id": tg_chat, "text": f"{title}\n{message}\n{url}".strip(),
                "disable_web_page_preview": "true"}).encode()
            _post(f"https://api.telegram.org/bot{tg_token}/sendMessage", body,
                  {"Content-Type": "application/x-www-form-urlencoded"})
            sent.append("telegram")
        except Exception as e:
            print(f"  telegram alert failed: {e}", file=sys.stderr)
    topic = os.environ.get("NTFY_TOPIC") or NTFY_TOPIC
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        headers = {"Title": title.encode("ascii", "ignore").decode(), "Tags": "moneybag"}
        if url:
            headers["Click"] = url
        try:
            _post(f"{server}/{topic}", message.encode(), headers)
            sent.append("ntfy")
        except Exception as e:
            print(f"  ntfy alert failed: {e}", file=sys.stderr)
    if sys.platform == "darwin":
        script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
        subprocess.run(["osascript", "-e", script], check=False)
        sent.append("mac")
    return sent


# ---------- fetching ----------

def fetch_snapshot(url: str, timeout_ms: int = 45000) -> PriceSnapshot:
    from playwright.sync_api import sync_playwright

    now = datetime.now().isoformat(timespec="seconds")

    def is_detail(resp) -> bool:
        return DETAIL_API in resp.url and resp.request.method == "POST"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="en-GB", user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"))
        # Preferred source: the product JSON the page requests after load.
        try:
            with page.expect_response(is_detail, timeout=timeout_ms) as resp_info:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            snap = parse_detail_json(resp_info.value.json(), url, now)
            if snap and snap.sale_price:
                browser.close()
                return snap
        except Exception as e:  # fall back to the rendered price
            print(f"  (product JSON not available: {e.__class__.__name__}; reading page instead)",
                  file=sys.stderr)
        try:
            page.wait_for_function(
                "() => { const e = document.querySelector('.price-container');"
                " return e && /\\d/.test(e.innerText) && !/^\\s*£?0(\\.00)?\\s*$/.test(e.innerText); }",
                timeout=timeout_ms)
        except Exception:
            pass
        sale_text = page.locator(".price-container").first.inner_text(timeout=5000)
        orig_loc = page.locator(".original-save-prices")
        orig_text = orig_loc.first.inner_text(timeout=2000) if orig_loc.count() else ""
        name = (page.title() or "").split("|")[0].strip()
        browser.close()

    snap = parse_dom_prices(sale_text, orig_text, name, url, now)
    if snap is None:
        raise RuntimeError(f"Could not find a price on {url}")
    return snap


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    ap.add_argument("--target", type=float, help="notify when the sale price is at or below this (GBP)")
    ap.add_argument("--no-notify", action="store_true", help="don't send any alerts")
    ap.add_argument("--every", type=float, metavar="MINUTES",
                    help="keep running and check every N minutes (e.g. --every 30)")
    ap.add_argument("--test-alert", action="store_true",
                    help="send a test alert to the configured channels and exit")
    args = ap.parse_args(argv)

    if args.test_alert:
        sent = notify("FlexiSpot tracker test", "Alerts are working.", args.url)
        print("sent via: " + (", ".join(sent) or "nothing configured"))
        return 0 if sent else 1

    if not args.every:
        return check_once(args)

    import random
    import time
    print(f"Checking every {args.every:g} min. Ctrl+C to stop.")
    while True:
        try:
            check_once(args)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # keep the loop alive through network blips / site hiccups
            print(f"[{datetime.now().isoformat(timespec='seconds')}] check failed: {e}", file=sys.stderr)
        # small jitter so requests don't land on an exact clock pattern
        time.sleep(args.every * 60 + random.uniform(0, 60))


def check_once(args) -> int:
    snap = fetch_snapshot(args.url)
    prev = last_snapshot(args.history, args.url)
    change, delta = describe_change(prev, snap)
    append_snapshot(args.history, snap)

    orig = f" (was £{snap.original_price:.2f}, -{snap.discount_pct}%)" if snap.original_price else ""
    stock = "" if snap.in_stock is None else (" in stock" if snap.in_stock else " OUT OF STOCK")
    print(f"[{snap.checked_at}] {snap.name}: £{snap.sale_price:.2f}{orig}{stock} — {change}", flush=True)
    if snap.promo_text:
        print(f"  promo: {snap.promo_text}", flush=True)

    reasons = alerts_for(prev, snap, delta, args.target)
    if reasons and not args.no_notify:
        sent = notify("FlexiSpot: " + reasons[0], "\n".join([snap.name, *reasons]), snap.url)
        print(f"  alert sent via: {', '.join(sent) or 'nothing configured'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
