"""FlexiSpot UK price tracker.

Loads a product page in headless Chromium (Playwright), reads the price the page
shows, appends it to a CSV history and reports whether it changed since the last
check. Also records product coupons (the "Discount £33"-style line under the price
and the coupon list the page loads). Sends Telegram / ntfy / macOS alerts on a price
drop, when the price after coupon reaches --target, when a coupon appears / changes /
disappears, on stock changes and on promo changes (settings at the top of this file).
It also scans the Black Friday campaign page for sofa/recliner coupon events
(--campaign-url, --keywords), which never appear on the product page.

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
from dataclasses import asdict, dataclass, field
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
# Campaign page with event-only coupons (e.g. the one-day "Sofa Flash Coupon Sale"
# tiers) that never show on the product page. "" turns this check off.
DEFAULT_CAMPAIGN_URL = "https://flexispot.co.uk/black-friday-2026"
DEFAULT_KEYWORDS = "sofa,recliner,waffle"
HERE = Path(__file__).resolve().parent
DEFAULT_HISTORY = HERE / "price_history.csv"
RAW_COUPON_DIR = HERE / "coupon_raw"
DETAIL_API = "/sapi/mall-item/item/detail"
COUPON_APIS = ("/sapi/mall-marketing/coupon/querySkuCoupon",
               "/sapi/mall-marketing/coupon/queryCouponDiscountFunc")
FIELDS = ["checked_at", "url", "name", "sale_price", "original_price", "discount_pct",
          "in_stock", "promo_text", "source", "coupons", "coupon_discount", "effective_price",
          "campaign"]


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
    coupons: str = ""                      # readable summary; "" = no coupon on the page
    coupon_discount: float | None = None   # £ off from the coupon, when the page states it
    effective_price: float | None = None   # sale price minus coupon discount
    campaign: str = ""                     # matching events/coupon tiers on the campaign page
    # not saved to the CSV:
    campaign_detail: str = field(default="", repr=False)        # with quantities left
    raw_coupon_json: list = field(default_factory=list, repr=False)


# ---------- campaign page (Black Friday coupon events) ----------

_DATE_RANGE = re.compile(r"^\d{1,2}\.[A-Z][a-z]{2}( \d{1,2}:\d{2})? - \d{1,2}\.[A-Z][a-z]{2}( \d{1,2}:\d{2})?$")
_SECTION_WORDS = re.compile(r"coupon|sale|deal|discount|offer|code", re.I)
_TIER = re.compile(r"-\s*(£\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*%)\s*Q(?:ty|uantity):?\s*(?:Only)?\s*(\d+)\s*(\d+)\s*%\s*Left", re.I)


def parse_campaign_text(text: str, keywords: list[str]) -> tuple[list[str], list[str], list[str]]:
    """From the campaign page's visible text, return:
    events   - timeline entries whose title mentions a keyword, e.g.
               "Sofa Tiered Coupon Sale (19.Nov 10:00 - 19.Nov 23:59)"
    sections - stable summary of matching coupon blocks, e.g.
               "Sofa Flash Coupon Sale: -50%, -30%, -10%, -8%"
    details  - the same blocks with how many coupons are left (changes all day,
               so it's shown in alerts but never used to decide whether to alert)
    """
    kws = [k.strip().lower() for k in keywords if k.strip()]
    lines = [" ".join(l.split()) for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    hit = lambda l: any(k in l.lower() for k in kws)

    events, timeline_titles = [], set()
    for i, l in enumerate(lines[:-1]):
        if _DATE_RANGE.match(l):
            timeline_titles.add(i + 1)
            if hit(lines[i + 1]):
                ev = f"{lines[i + 1]} ({l})"
                if ev not in events:
                    events.append(ev)

    sections, details, seen = [], [], set()
    for i, l in enumerate(lines):
        if i in timeline_titles or l in seen or len(l) > 80:
            continue
        if not (hit(l) and _SECTION_WORDS.search(l)):
            continue
        seen.add(l)
        window = lines[i + 1:i + 60]
        sub = next((w for w in window[:3] if w != l and len(w) > 20 and not _CLOCK.search(w)), "")
        tiers = _TIER.findall(" ".join(window))
        if not tiers and not sub:
            continue
        offs = [t[0].replace(" ", "") for t in tiers]
        sections.append(f"{l}: " + (", ".join("-" + o for o in offs) if offs else sub))
        left = ", ".join(f"-{t[0].replace(' ', '')} ({t[1]} coupons, {t[2]}% left)" for t in tiers)
        details.append(f"{l}" + (f" - {sub}" if sub else "") + (f": {left}" if left else ""))
    return events, sections, details


def apply_campaign(snap: "PriceSnapshot", text: str, keywords: list[str]) -> "PriceSnapshot":
    events, sections, details = parse_campaign_text(text, keywords)
    snap.campaign = " | ".join(sections + [f"event: {e}" for e in events])
    snap.campaign_detail = "\n".join(details + [f"event: {e}" for e in events])
    return snap


# Coupon JSON fields worth keeping (names vary by promotion type) and fields that
# change on every load (remaining stock, server time) which must not trigger alerts.
_COUPON_KEEP = re.compile(r"name|title|code|discount|amount|threshold|value|desc|wording|rule|"
                          r"condition|type|start|end|expire", re.I)
_COUPON_SKIP = re.compile(r"current|remain|stock|received|trace|image|icon|url|"
                          r"(count|num|qty|quantity|left)$|^id$|Id$", re.I)
_CLOCK = re.compile(r"\d{1,2}\s*:\s*\d{2}")


def summarize_coupon_json(payloads: list[dict]) -> list[str]:
    """Readable, stable one-line summaries of the coupons in the coupon API responses."""
    out = []

    def flat(obj, prefix=""):
        if isinstance(obj, dict) and "value" in obj and set(obj) <= {"type", "value", "currency"}:
            yield prefix.rstrip("."), obj["value"]  # money object, e.g. {"type": "global", "value": 33}
        elif isinstance(obj, dict):
            for k, v in obj.items():
                yield from flat(v, f"{prefix}{k}.")
        elif isinstance(obj, list):
            for v in obj[:5]:
                yield from flat(v, prefix)
        elif obj not in (None, "", [], {}):
            yield prefix.rstrip("."), obj

    for payload in payloads:
        data = (payload or {}).get("data")
        items = data if isinstance(data, list) else ([data] if isinstance(data, dict) and data else [])
        for item in items:
            parts = []
            for key, val in flat(item):
                leaf = key.split(".")[-1]
                if _COUPON_KEEP.search(leaf) and not _COUPON_SKIP.search(leaf):
                    parts.append(f"{leaf}={val}")
            if parts:
                out.append(", ".join(dict.fromkeys(parts)))
    return out


def clean_dom_coupon_texts(texts: list[str]) -> list[str]:
    """Keep the coupon wording shown on the page; drop countdown clocks and blanks."""
    keep = []
    for t in texts:
        t = " ".join((t or "").split())
        if t and len(t) <= 160 and not _CLOCK.search(t) and t not in keep:
            keep.append(t)
    return keep


def coupon_discount_from_text(texts: list[str], sale: float | None) -> float | None:
    """'Discount £33' -> 33.0; '-10%' -> 10% of the sale price."""
    for t in texts:
        if "£" in t:
            v = parse_money(t)
            if v:
                return round(v, 2)
    for t in texts:
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
        if m and sale:
            return round(sale * float(m.group(1)) / 100, 2)
    return None


def apply_coupons(snap: "PriceSnapshot", api_payloads: list[dict], dom_texts: list[str],
                  discount_texts: list[str] | None = None) -> "PriceSnapshot":
    """dom_texts: all coupon wording on the page; discount_texts: just the
    '.discount-price' line under the price, the only place a £ amount is read from."""
    disc = clean_dom_coupon_texts(discount_texts or [])
    dom = [t for t in clean_dom_coupon_texts(dom_texts) if t not in disc]
    api = summarize_coupon_json(api_payloads)
    snap.raw_coupon_json = [p for p in api_payloads if (p or {}).get("data")]
    snap.coupons = " | ".join(disc + dom + api)
    snap.coupon_discount = coupon_discount_from_text(disc, snap.sale_price)
    if snap.sale_price is not None:
        snap.effective_price = round(snap.sale_price - (snap.coupon_discount or 0), 2)
    return snap


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
    if not sale or sale <= 0:  # page still loading / placeholder: not a real price
        return None
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
    if not sale or sale <= 0:  # "£0.00" is the placeholder shown before the price loads
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
    if not new:  # upgrade a history file written before new columns were added
        with history.open(newline="") as f:
            rows = list(csv.DictReader(f))
            header = rows and list(rows[0].keys()) or None
        if header is not None and header != FIELDS:
            with history.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
    with history.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(asdict(snap))


# Columns that count as "something changed" when deciding whether to log a row.
CHANGE_FIELDS = ("sale_price", "original_price", "in_stock", "promo_text", "coupons",
                 "coupon_discount", "campaign")


def should_log(prev: dict | None, snap: PriceSnapshot) -> tuple[bool, str]:
    """Log a row when something changed, plus one heartbeat row per day so a quiet
    week still shows the tracker was running. Returns (log?, reason)."""
    if prev is None:
        return True, "first check"
    row = asdict(snap)
    changed = [f for f in CHANGE_FIELDS
               if ("" if row[f] is None else str(row[f])) != (prev.get(f) or "")]
    if changed:
        return True, "changed: " + ", ".join(changed)
    if (prev.get("checked_at") or "")[:10] != snap.checked_at[:10]:
        return True, "daily heartbeat"
    return False, "no change, not logged"


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
    # coupons: new, changed or gone (older history rows have no coupon column -> "")
    prev_coupons = (prev or {}).get("coupons") or ""
    if snap.coupons and snap.coupons != prev_coupons:
        pay = f" (pay £{snap.effective_price:.2f})" if snap.coupon_discount else ""
        reasons.append(f"{'Coupon changed' if prev_coupons else 'Coupon available'}: {snap.coupons}{pay}")
    elif prev is not None and prev_coupons and not snap.coupons:
        reasons.append("Coupon no longer shown")
    # campaign page: a new or changed sofa/recliner coupon event
    prev_campaign = (prev or {}).get("campaign") or ""
    if snap.campaign and snap.campaign != prev_campaign:
        reasons.append("Black Friday page: " + (snap.campaign_detail or snap.campaign).replace("\n", "\n  "))
    # target is checked against the price after any coupon
    now_price = snap.effective_price if snap.effective_price is not None else snap.sale_price
    if target is not None and now_price is not None and now_price <= target:
        prev_price = None
        if prev:
            prev_price = float(prev.get("effective_price") or prev["sale_price"])
        if prev_price is None or prev_price > target:
            reasons.append(f"At or below your target £{target:.2f}: now £{now_price:.2f}")
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
      NTFY_TOPIC (+ optional NTFY_SERVER,     -> ntfy.sh push notification
                  NTFY_PRIORITY=min|low|default|high|urgent, default high)
    A macOS desktop notification is also shown when running on a Mac.
    """
    import os
    import urllib.parse
    sent = []
    # strip() guards against a space or line break pasted into a secret
    tg_token = (os.environ.get("TELEGRAM_BOT_TOKEN") or TELEGRAM_BOT_TOKEN).strip().strip('"\'')
    tg_chat = (os.environ.get("TELEGRAM_CHAT_ID") or TELEGRAM_CHAT_ID).strip().strip('"\'')
    if tg_token and tg_chat:
        try:
            body = urllib.parse.urlencode({
                "chat_id": tg_chat, "text": f"{title}\n{message}\n{url}".strip(),
                "disable_web_page_preview": "true"}).encode()
            _post(f"https://api.telegram.org/bot{tg_token}/sendMessage", body,
                  {"Content-Type": "application/x-www-form-urlencoded"})
            sent.append("telegram")
        except Exception as e:
            detail = ""
            if hasattr(e, "read"):  # Telegram explains 4xx errors in the response body
                try:
                    detail = " - " + json.loads(e.read().decode()).get("description", "")
                except Exception:
                    pass
            print(f"  telegram alert failed: {e}{detail}", file=sys.stderr)
    topic = os.environ.get("NTFY_TOPIC") or NTFY_TOPIC
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        # Priority 4-5 makes the phone ring/vibrate straight away instead of a quiet
        # badge; 5 ("urgent") also breaks through Do Not Disturb if you allow it in the app.
        headers = {"Title": title.encode("ascii", "ignore").decode(), "Tags": "moneybag",
                   "Priority": os.environ.get("NTFY_PRIORITY", "high")}
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

def fetch_snapshot(url: str, timeout_ms: int = 45000, campaign_url: str = "",
                   keywords: list[str] | None = None) -> PriceSnapshot:
    from playwright.sync_api import sync_playwright

    now = datetime.now().isoformat(timespec="seconds")

    def is_detail(resp) -> bool:
        return DETAIL_API in resp.url and resp.request.method == "POST"

    coupon_payloads: list[dict] = []

    def on_response(resp):
        if any(api in resp.url for api in COUPON_APIS):
            try:
                coupon_payloads.append(resp.json())
            except Exception:
                pass

    def read_coupons(page) -> tuple[list[str], list[str]]:
        """Coupon JSON arrives after the price; give it a few seconds, then read the page."""
        try:
            page.wait_for_response(lambda r: COUPON_APIS[0] in r.url, timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(2500)  # discount call + render
        visible = "els => els.filter(e => e.offsetParent !== null && e.children.length < 6).map(e => e.innerText)"
        try:
            discount = page.eval_on_selector_all(".discount-price", visible)
            other = page.eval_on_selector_all("[class*='coupon'], [class*='Coupon']", visible)
            return other, discount
        except Exception:
            return [], []

    def read_product(page) -> PriceSnapshot:
        # Preferred source: the product JSON the page requests after load. It's
        # occasionally slow or missing, so reload and try again before falling back.
        for attempt in range(1, 4):
            try:
                coupon_payloads.clear()
                with page.expect_response(is_detail, timeout=timeout_ms // 2) as resp_info:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                snap = parse_detail_json(resp_info.value.json(), url, now)
                if snap:
                    dom_texts, discount_texts = read_coupons(page)
                    return apply_coupons(snap, coupon_payloads, dom_texts, discount_texts)
                why = "no valid price in it"
            except Exception as e:
                why = e.__class__.__name__
            print(f"  (product JSON attempt {attempt}/3 failed: {why})", file=sys.stderr)
        print("  (reading the price from the page instead)", file=sys.stderr)
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
        dom_texts, discount_texts = read_coupons(page)
        snap = parse_dom_prices(sale_text, orig_text, name, url, now)
        if snap is None:
            raise RuntimeError(f"Could not find a price on {url}")
        return apply_coupons(snap, coupon_payloads, dom_texts, discount_texts)

    def read_campaign(page, snap: PriceSnapshot) -> None:
        """Best effort: the campaign page may be gone after the event, which is fine."""
        try:
            resp = page.goto(campaign_url, wait_until="domcontentloaded", timeout=timeout_ms)
            if resp is not None and resp.status >= 400:
                print(f"  (campaign page returned {resp.status}; skipped)", file=sys.stderr)
                return
            page.wait_for_timeout(6000)  # sections render from several API calls
            apply_campaign(snap, page.inner_text("body"), keywords or [])
        except Exception as e:
            print(f"  (campaign page check failed: {e.__class__.__name__}; skipped)", file=sys.stderr)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="en-GB", user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"))
        page.on("response", on_response)
        try:
            snap = read_product(page)
            if campaign_url:
                read_campaign(page, snap)
        finally:
            browser.close()
    return snap


def save_raw_coupons(snap: PriceSnapshot, folder: Path = RAW_COUPON_DIR) -> Path | None:
    """Keep the coupon API's raw JSON the first time a coupon appears or changes,
    so the alert wording can be improved from real data later."""
    if not snap.raw_coupon_json:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{snap.checked_at.replace(':', '-')}.json"
    out.write_text(json.dumps(snap.raw_coupon_json, indent=2, ensure_ascii=False))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    ap.add_argument("--target", type=float,
                    help="notify when the price after any coupon is at or below this (GBP)")
    ap.add_argument("--campaign-url", default=DEFAULT_CAMPAIGN_URL,
                    help='campaign page to scan for event coupons ("" to skip)')
    ap.add_argument("--keywords", default=DEFAULT_KEYWORDS,
                    help="comma-separated words that mark a campaign event/coupon as relevant")
    ap.add_argument("--no-notify", action="store_true", help="don't send any alerts")
    ap.add_argument("--log-all", action="store_true",
                    help="add a history row on every check (default: only changes + one per day)")
    ap.add_argument("--every", type=float, metavar="MINUTES",
                    help="keep running and check every N minutes (e.g. --every 30)")
    ap.add_argument("--test-alert", action="store_true",
                    help="send a test alert to the configured channels and exit")
    args = ap.parse_args(argv)

    if args.test_alert:
        sent = notify("FlexiSpot tracker test", "Alerts are working.", args.url)
        print("sent via: " + (", ".join(sent) or "nothing (no channel configured, or every channel failed)"))
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


def fill_from_previous(snap: PriceSnapshot, prev: dict | None) -> PriceSnapshot:
    """A page-read (fallback) snapshot can't see the promo banner or stock status.
    Keep the last known values so they don't look like they vanished and came back."""
    if snap.source == "dom" and prev:
        if not snap.promo_text:
            snap.promo_text = prev.get("promo_text") or ""
        if snap.in_stock is None and prev.get("in_stock") in ("True", "False"):
            snap.in_stock = prev["in_stock"] == "True"
    return snap


def is_suspicious_drop(prev: dict | None, snap: PriceSnapshot) -> bool:
    """A fall of more than half in one check is far more likely a bad read than a real deal."""
    try:
        return bool(prev) and snap.sale_price < 0.5 * float(prev["sale_price"])
    except (TypeError, ValueError, KeyError):
        return False


def check_once(args) -> int:
    fetch = lambda: fetch_snapshot(args.url, campaign_url=args.campaign_url,
                                   keywords=args.keywords.split(","))
    snap = fetch()
    prev = last_snapshot(args.history, args.url)
    if is_suspicious_drop(prev, snap):
        print(f"  (price read as £{snap.sale_price:.2f}, under half the last £{prev['sale_price']}; "
              "checking again)", file=sys.stderr, flush=True)
        snap = fetch()  # a genuine price cut shows up again; a glitch usually doesn't
    snap = fill_from_previous(snap, prev)
    change, delta = describe_change(prev, snap)
    log, log_reason = (True, "logging every check") if args.log_all else should_log(prev, snap)
    if log:
        append_snapshot(args.history, snap)

    orig = f" (was £{snap.original_price:.2f}, -{snap.discount_pct}%)" if snap.original_price else ""
    stock = "" if snap.in_stock is None else (" in stock" if snap.in_stock else " OUT OF STOCK")
    print(f"[{snap.checked_at}] {snap.name}: £{snap.sale_price:.2f}{orig}{stock} — {change}", flush=True)
    if snap.promo_text:
        print(f"  promo: {snap.promo_text}", flush=True)
    if snap.coupons:
        pay = f" -> pay £{snap.effective_price:.2f}" if snap.coupon_discount else ""
        print(f"  coupon: {snap.coupons}{pay}", flush=True)
    else:
        print("  coupon: none shown", flush=True)
    print(f"  history: {log_reason}", flush=True)
    if args.campaign_url:
        print("  campaign: " + (snap.campaign_detail.replace("\n", "\n            ")
                                if snap.campaign else f"nothing matching '{args.keywords}'"), flush=True)

    reasons = alerts_for(prev, snap, delta, args.target)
    if snap.raw_coupon_json and snap.coupons != ((prev or {}).get("coupons") or ""):
        raw = json.dumps(snap.raw_coupon_json, ensure_ascii=False)
        print(f"  raw coupon data: {raw[:4000]}", flush=True)
        saved = save_raw_coupons(snap)
        print(f"  raw coupon data saved to {saved.relative_to(HERE) if saved else '-'}", flush=True)
    if reasons and not args.no_notify:
        sent = notify("FlexiSpot: " + reasons[0], "\n".join([snap.name, *reasons]), snap.url)
        print(f"  alert sent via: {', '.join(sent) or 'nothing configured'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
